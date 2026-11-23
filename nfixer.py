#!/usr/bin/env python3
"""NFixer - repairs and compresses game recordings (NVIDIA Instant Replay / ShadowPlay) without visible quality loss.

Commands:
  check    - finds recordings damaged by an Instant Replay buffer overflow (the head of the clip is overwritten).
  estimate - searches settings on a few random clips and predicts the space saving.
  compress - re-encodes H.264 -> HEVC, picking for every file the strongest compression at which VMAF
             against the original stays above the target. Audio tracks are copied untouched.
             The original is replaced only after the result passes verification.
  repair   - cuts the damaged head off damaged recordings without re-encoding.

Requires Python 3.9+ and FFmpeg (ffmpeg.exe + ffprobe.exe) built with libvmaf.
Messages are in English by default; --lang ru or NFIXER_LANG=ru switches to Russian.
"""
import argparse
import bisect
import ctypes
import io
import json
import os
import random
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time

VERSION = "1.0"
TMP_SUFFIX = ".nfixer-tmp.mp4"
HERE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(HERE, "logs")
JOURNAL = os.path.join(LOG_DIR, "journal.jsonl")

FFMPEG = FFPROBE = None
_current_proc = None  # the ffmpeg process that is running now (so Ctrl+C can stop it cleanly)
LANG = "en"  # message language: en or ru (--lang option or the NFIXER_LANG variable)


def L(en, ru):
    """Return the text in the current language."""
    return ru if LANG == "ru" else en


def detect_lang(argv):
    """The language is needed before argument parsing (the help text depends on it), so --lang is looked up by hand."""
    lang = os.environ.get("NFIXER_LANG", "en")
    for i, x in enumerate(argv):
        if x == "--lang" and i + 1 < len(argv):
            lang = argv[i + 1]
        elif x.startswith("--lang="):
            lang = x[7:]
    return lang if lang in ("en", "ru") else "en"


# --------------------------------------------------------------------------------------
# Encoders. q is the quality parameter (higher = stronger compression), grid is the set of values to search.
# --------------------------------------------------------------------------------------
def _nvenc_hevc(q, gop, preset):
    # p5 measured: same size/quality as p7 (VMAF difference ~0.2) but 1.7x faster
    return ["-c:v", "hevc_nvenc", "-preset", preset or "p5", "-tune", "hq", "-rc", "vbr", "-cq", str(q), "-b:v", "0",
            "-maxrate", "250M", "-bufsize", "500M", "-bf", "3", "-b_ref_mode", "middle", "-spatial-aq", "1",
            "-temporal-aq", "1", "-rc-lookahead", "32", "-g", str(gop), "-tag:v", "hvc1"]


def _x265(q, gop, preset):
    return ["-c:v", "libx265", "-preset", preset or "medium", "-crf", str(q),
            "-x265-params", f"keyint={gop}:log-level=error", "-tag:v", "hvc1"]


def _nvenc_av1(q, gop, preset):
    return ["-c:v", "av1_nvenc", "-preset", preset or "p5", "-tune", "hq", "-rc", "vbr", "-cq", str(q), "-b:v", "0",
            "-maxrate", "250M", "-bufsize", "500M", "-spatial-aq", "1", "-temporal-aq", "1", "-rc-lookahead", "32",
            "-g", str(gop)]


ENCODERS = {
    # name: (argument builder, quality grid in order of growing compression, search start, output codec, description)
    # The descriptions go into argparse help, hence the escaped %%.
    "nvenc": (_nvenc_hevc, list(range(26, 39, 2)), 32, "hevc",
              ("HEVC on an NVIDIA GPU (fast)", "HEVC на видеокарте NVIDIA (быстро)")),
    "x265": (_x265, list(range(18, 31, 2)), 24, "hevc",
             ("HEVC on the CPU (~5x slower, files 10-20%% smaller)",
              "HEVC на процессоре (в ~5 раз медленнее, файлы на 10-20%% меньше)")),
    "av1": (_nvenc_av1, list(range(30, 45, 2)), 38, "av1",
            ("AV1 on an NVIDIA GPU (~12%% smaller still, but older video editors cannot open such files)",
             "AV1 на видеокарте NVIDIA (ещё ~12%% меньше, но старые версии видеоредакторов такие файлы не открывают)")),
}


# --------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------
def human(n):
    n = float(n)
    for i, unit in enumerate(L(("B", "KB", "MB", "GB", "TB"), ("Б", "КБ", "МБ", "ГБ", "ТБ"))):
        if abs(n) < 1024 or i == 4:
            return f"{n:.0f} {unit}" if i < 2 else f"{n:.1f} {unit}" if i == 2 else f"{n:.2f} {unit}"
        n /= 1024


def hms(sec):
    sec = int(max(0, sec))
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


def say(msg=""):
    print(msg, flush=True)


_status_len = 0


def status(msg):
    """Update the progress line in place (interactive console only)."""
    global _status_len
    if os.environ.get("NFIXER_GUI"):  # the window reads lines prefixed with @@ and shows them in its status line
        if msg.strip():
            print("@@" + msg.strip(), flush=True)
        return
    if not sys.stdout.isatty():
        return
    width = shutil.get_terminal_size((100, 20)).columns - 1
    msg = msg[:width]
    sys.stdout.write("\r" + msg + " " * max(0, _status_len - len(msg)) + "\r")
    sys.stdout.flush()
    _status_len = len(msg)


def find_ffmpeg(explicit=None):
    global FFMPEG, FFPROBE
    ext = ".exe" if os.name == "nt" else ""
    cands = []
    if explicit:
        cands.append(explicit)
    if os.environ.get("NFIXER_FFMPEG"):
        cands.append(os.environ["NFIXER_FFMPEG"])
    cands.append(os.path.join(HERE, "ffmpeg", "bin"))
    cands.append(os.path.join(HERE, "ffmpeg"))
    w = shutil.which("ffmpeg")
    if w:
        cands.append(os.path.dirname(w))
    for d in cands:
        a, b = os.path.join(d, "ffmpeg" + ext), os.path.join(d, "ffprobe" + ext)
        if os.path.isfile(a) and os.path.isfile(b):
            FFMPEG, FFPROBE = a, b
            return
    sys.exit(L("FFmpeg not found (ffmpeg.exe and ffprobe.exe). Point to its folder with --ffmpeg or the NFIXER_FFMPEG "
               "variable.",
               "Не найден FFmpeg (ffmpeg.exe и ffprobe.exe). Укажите папку через --ffmpeg или переменную NFIXER_FFMPEG."))


def work_dir():
    """Scratch folder for sample encodes. The window passes NFIXER_WORKDIR so it can delete it after Stop."""
    parent = os.environ.get("NFIXER_WORKDIR")
    return tempfile.mkdtemp(prefix="nfixer-", dir=parent if parent and os.path.isdir(parent) else None)


def set_file_times(path, st):
    """Give the file back its original dates (modified, accessed and - on Windows - created)."""
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    if os.name != "nt":
        return
    try:
        k32 = ctypes.windll.kernel32
        k32.CreateFileW.restype = ctypes.c_void_p
        k32.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
                                    ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
        k32.SetFileTime.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        h = k32.CreateFileW(path, 0x100, 7, None, 3, 0x02000000, None)  # FILE_WRITE_ATTRIBUTES, OPEN_EXISTING
        if h in (None, ctypes.c_void_p(-1).value):
            return
        ft = ctypes.c_uint64(int(st.st_ctime_ns // 100) + 116444736000000000)
        k32.SetFileTime(h, ctypes.byref(ft), None, None)
        k32.CloseHandle(h)
    except Exception:
        pass  # the creation date is cosmetic; it must not break the job


# --------------------------------------------------------------------------------------
# MP4 parsing and detection of Instant Replay ring-buffer damage
# --------------------------------------------------------------------------------------
def _atoms(f, start, end):
    pos = start
    while pos + 8 <= end:
        f.seek(pos)
        h = f.read(8)
        if len(h) < 8:
            return
        size, typ = struct.unpack(">I4s", h)
        hlen = 8
        if size == 1:
            size = struct.unpack(">Q", f.read(8))[0]
            hlen = 16
        elif size == 0:
            size = end - pos
        if size < hlen or pos + size > end:
            return
        yield typ, pos + hlen, pos + size
        pos += size


def _find(f, start, end, path):
    for typ, s, e in _atoms(f, start, end):
        if typ == path[0]:
            if len(path) == 1:
                yield s, e
            else:
                yield from _find(f, s, e, path[1:])


def _rd(f, s, n):
    f.seek(s)
    return f.read(n)


def _sample_ok(f, off, size, sync):
    """Does the sample look like a real H.264 frame (rather than foreign data that ended up in its place)?"""
    f.seek(off)
    d = f.read(6)
    if len(d) < 6:
        return False
    ln = int.from_bytes(d[:4], "big")
    if ln == 0 or ln + 4 > size:
        return False
    h = d[4]
    if h & 0x80:
        return False
    t = h & 31
    if t in (6, 7, 8, 9):
        return True
    if t == 5 or (t == 1 and not sync):
        return bool(d[5] & 0x80)  # first_mb_in_slice == 0
    return False


def inspect_mp4(path, deep=False, light=False):
    """Return facts about the MP4 video track and about damage to its head.

    damage: 'none' | 'head' (head overwritten, the rest intact) | 'total' | 'partial' | 'no-moov' | 'truncated' | 'n/a'
    deep=True - check every frame, not only keyframes (slower).
    light=True - read the index only (sizes, frame count) without checking frames: almost instant even on a slow disk.
    """
    r = {"damage": "n/a"}
    with open(path, "rb") as f:
        fsize = os.fstat(f.fileno()).st_size
        moov = next(((s, e) for t, s, e in _atoms(f, 0, fsize) if t == b"moov"), None)
        if not moov:
            r["damage"] = "no-moov"
            return r
        m = io.BytesIO(_rd(f, moov[0], moov[1] - moov[0]))
        ml = moov[1] - moov[0]
        for ts, te in _find(m, 0, ml, [b"trak"]):
            hd = next(_find(m, ts, te, [b"mdia", b"hdlr"]), None)
            stbl = next(_find(m, ts, te, [b"mdia", b"minf", b"stbl"]), None)
            if not hd or not stbl or _rd(m, hd[0] + 8, 4) != b"vide":
                continue
            sd = next(_find(m, *stbl, [b"stsd"]))
            codec = _rd(m, sd[0] + 12, 4).decode("latin1")
            width, height = struct.unpack(">HH", _rd(m, sd[0] + 40, 4))
            md = next(_find(m, ts, te, [b"mdia", b"mdhd"]))
            if _rd(m, md[0], 1)[0] == 1:
                tscale = struct.unpack(">I", _rd(m, md[0] + 20, 4))[0]
            else:
                tscale = struct.unpack(">I", _rd(m, md[0] + 12, 4))[0]
            sz = next(_find(m, *stbl, [b"stsz"]))
            uni, cnt = struct.unpack(">II", _rd(m, sz[0] + 4, 8))
            sizes = [uni] * cnt if uni else list(struct.unpack(f">{cnt}I", _rd(m, sz[0] + 12, cnt * 4)))
            co = next(_find(m, *stbl, [b"stco"]), None)
            if co:
                n = struct.unpack(">I", _rd(m, co[0] + 4, 4))[0]
                chunks = struct.unpack(f">{n}I", _rd(m, co[0] + 8, n * 4))
            else:
                co = next(_find(m, *stbl, [b"co64"]))
                n = struct.unpack(">I", _rd(m, co[0] + 4, 4))[0]
                chunks = struct.unpack(f">{n}Q", _rd(m, co[0] + 8, n * 8))
            sc = next(_find(m, *stbl, [b"stsc"]))
            n = struct.unpack(">I", _rd(m, sc[0] + 4, 4))[0]
            raw = _rd(m, sc[0] + 8, n * 12)
            stsc = [struct.unpack_from(">III", raw, i * 12) for i in range(n)]
            offsets = []
            si = 0
            for i, (first, per, _) in enumerate(stsc):
                last = stsc[i + 1][0] - 1 if i + 1 < len(stsc) else len(chunks)
                for c in range(first, last + 1):
                    off = chunks[c - 1]
                    for _ in range(per):
                        if si >= cnt:
                            break
                        offsets.append(off)
                        off += sizes[si]
                        si += 1
            st = next(_find(m, *stbl, [b"stts"]))
            n = struct.unpack(">I", _rd(m, st[0] + 4, 4))[0]
            raw = _rd(m, st[0] + 8, n * 8)
            times = []  # start time of every frame, s
            t = 0
            for i in range(n):
                c, d = struct.unpack_from(">II", raw, i * 8)
                for _ in range(c):
                    times.append(t / tscale)
                    t += d
            total = t / tscale if tscale else 0
            ss = next(_find(m, *stbl, [b"stss"]), None)
            if ss:
                kn = struct.unpack(">I", _rd(m, ss[0] + 4, 4))[0]
                keys = [k - 1 for k in struct.unpack(f">{kn}I", _rd(m, ss[0] + 8, kn * 4)) if k - 1 < cnt]
            else:
                keys = list(range(cnt))
            nfr = min(len(offsets), len(sizes), len(times))
            r.update(codec=codec, width=width, height=height, frames=nfr, vdur=total, vbytes=sum(sizes), sizes=sizes,
                     times=times, has_ctts=next(_find(m, *stbl, [b"ctts"]), None) is not None,
                     vmbps=sum(sizes) * 8 / total / 1e6 if total else 0)
            if light or codec not in ("avc1", "avc3") or not nfr or not keys:
                return r
            if offsets[nfr - 1] + sizes[nfr - 1] > fsize:
                r["damage"] = "truncated"
                return r
            ok = [_sample_ok(f, offsets[k], sizes[k], True) for k in keys]
            if all(ok):
                r["damage"] = "none"
                if deep:
                    keyset = set(keys)
                    if not all(_sample_ok(f, offsets[i], sizes[i], i in keyset) for i in range(nfr)):
                        r["damage"] = "partial"
                return r
            # Cut everything before the first keyframe that is no longer followed by bad keyframes.
            # Garbage passes the check with a probability of a few percent, so the candidate's whole group of
            # frames is checked.
            c = max(i for i, v in enumerate(ok) if not v) + 1
            while c < len(keys):
                a = keys[c]
                b = min(keys[c + 1] if c + 1 < len(keys) else nfr, a + 90)
                good = sum(_sample_ok(f, offsets[i], sizes[i], i == a) for i in range(a, b))
                if good >= 0.95 * (b - a):
                    break
                c += 1
            if c >= len(keys) or total - times[keys[c]] < 3:
                r["damage"] = "total"
                return r
            cut = keys[c]
            r.update(damage="head", cut_frame=cut, cut_time=times[cut], bad_bytes=offsets[cut] - offsets[0],
                     bad_keys=sum(1 for v in ok if not v), keys=len(keys))
            return r
    return r


# --------------------------------------------------------------------------------------
# ffprobe / ffmpeg
# --------------------------------------------------------------------------------------
def probe(path):
    cmd = [FFPROBE, "-v", "error", "-show_entries",
           "format=duration:format_tags:stream=index,codec_type,codec_name,pix_fmt,width,height,avg_frame_rate,"
           "nb_frames,duration,start_time,color_range,color_space,color_transfer,color_primaries:stream_tags:"
           "stream_disposition=attached_pic", "-of", "json", path]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise RuntimeError("ffprobe: " + (p.stderr.strip()[-300:] or L("cannot read the file", "не удалось прочитать файл")))
    return json.loads(p.stdout)


def video_start(path):
    """Presentation time of the first frame of the video track."""
    for s in probe(path).get("streams", []):
        if s.get("codec_type") == "video":
            return float(s.get("start_time") or 0)
    return 0.0


def _frac(s):
    try:
        a, b = s.split("/")
        return float(a) / float(b) if float(b) else 0.0
    except Exception:
        return 0.0


def run_ffmpeg(args, cwd=None, total_frames=0, label=""):
    """Run ffmpeg and show progress. Returns (exit code, error text)."""
    global _current_proc
    cmd = [FFMPEG, "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-nostats"]
    if total_frames:
        cmd += ["-progress", "pipe:1"]
    cmd += args
    flags = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
    proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=flags)
    _current_proc = proc
    err = []
    th = threading.Thread(target=lambda: err.append(proc.stderr.read()), daemon=True)
    th.start()
    frame = 0
    t0 = time.time()
    for raw in proc.stdout:
        line = raw.decode("ascii", "replace").strip()
        if line.startswith("frame="):
            try:
                frame = int(line[6:])
            except ValueError:
                pass
        elif line.startswith("progress=") and total_frames:
            el = time.time() - t0
            fps = frame / el if el > 0 else 0
            eta = (total_frames - frame) / fps if fps > 0 else 0
            pct = min(100, frame * 100 // total_frames)
            status(L(f"   {label} {pct:3}%  {fps:5.0f} fps  {hms(eta)} left",
                     f"   {label} {pct:3}%  {fps:5.0f} к/с  осталось {hms(eta)}"))
    proc.wait()
    th.join()
    proc.stdout.close()
    proc.stderr.close()
    _current_proc = None
    status("")
    return proc.returncode, (err[0] if err else b"").decode("utf-8", "replace")


class Source:
    """Everything that needs to be known about a source file."""

    def __init__(self, path):
        self.path = path
        self.st = os.stat(path)
        self.size = self.st.st_size
        self.reason = None  # why the file cannot be processed
        pr = probe(path)
        streams = pr.get("streams", [])
        vids = [s for s in streams
                if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")]
        self.audio = [s for s in streams if s.get("codec_type") == "audio"]
        other = [s for s in streams if s.get("codec_type") not in ("video", "audio")]
        if len(vids) != 1:
            self.reason = L("not exactly one video stream", "не одна видеодорожка")
            self.v = vids[0] if vids else {}
        else:
            self.v = vids[0]
        v = self.v
        self.codec = v.get("codec_name", "?")
        self.width, self.height = v.get("width", 0), v.get("height", 0)
        self.fps = _frac(v.get("avg_frame_rate", "0/1")) or 60.0
        self.duration = float(v.get("duration") or pr.get("format", {}).get("duration") or 0)
        self.nb_frames = int(v.get("nb_frames") or 0)
        self.start = float(v.get("start_time") or 0)  # presentation time of the first frame
        if not self.reason:
            if other:
                kinds = ",".join(sorted({s.get("codec_type", "?") for s in other}))
                self.reason = L("has streams other than video and audio (", "есть нестандартные дорожки (") + kinds + ")"
            elif v.get("pix_fmt") != "yuv420p":
                self.reason = L(f"pixel format {v.get('pix_fmt')} (only yuv420p is supported, to leave colours untouched)",
                                f"формат пикселей {v.get('pix_fmt')} (поддерживается только yuv420p, чтобы не трогать цвета)")
            elif self.duration <= 0 or self.nb_frames <= 0:
                self.reason = L("cannot determine the duration", "не удалось определить длительность")
        self.insp = inspect_mp4(path) if path.lower().endswith((".mp4", ".mov", ".m4v")) else {"damage": "n/a"}
        self.cut_frame = 0
        self.cut_time = 0.0

    def color_args(self):
        a = []
        for key, opt in (("color_primaries", "-color_primaries"), ("color_transfer", "-color_trc"),
                         ("color_space", "-colorspace"), ("color_range", "-color_range")):
            val = self.v.get(key)
            if val and val not in ("unknown", "unspecified"):
                a += [opt, val]
        return a

    @property
    def gop(self):
        return max(12, int(round(self.fps * 2)))

    @property
    def kept_frames(self):
        return self.nb_frames - self.cut_frame

    @property
    def kept_duration(self):
        return self.duration - self.cut_time

    def windows(self, fracs, length=2.0):
        """Windows (start, length) in the source used for quality spot checks."""
        nwin = int(round(length * self.fps)) / self.fps
        if self.duration < length * 2.5:
            fracs = [0.0]
            nwin = min(4.0, max(1, int((self.duration - 0.2) * self.fps)) / self.fps)
        times = [] if self.insp.get("has_ctts") else (self.insp.get("times") or [])
        out = []
        for fr in fracs:
            t = max(0.0, min(self.duration * fr - nwin / 2, self.duration - nwin - 0.1))
            # The window edge is placed halfway between two frames. A small timestamp shift between the source and
            # the result then does not change which frames fall into the window, so frames are compared one to one.
            i = bisect.bisect_left(times, t)
            if 0 < i < len(times):
                t = self.start + (times[i - 1] + times[i]) / 2
            out.append((t, nwin))
        return out

    def window_bytes(self, wins):
        """How many bytes the source video takes in these windows."""
        times, sizes = self.insp.get("times"), self.insp.get("sizes")
        total = 0
        for t, d in wins:
            if times and sizes:
                total += sum(sz for tt, sz in zip(times, sizes) if t <= tt < t + d)
            else:
                total += int(self.video_bytes * d / self.duration)
        return total

    @property
    def video_bytes(self):
        if self.insp.get("vbytes"):
            return self.insp["vbytes"]
        return self.size - self.audio_bytes

    @property
    def audio_bytes(self):
        return int(len(self.audio) * 24000 * self.duration)  # ~190 kbit/s per track, used for the forecast only


def _inputs(path, wins):
    a = []
    for t, d in wins:
        a += ["-ss", f"{t:.6f}", "-t", f"{d:.6f}", "-i", path]
    return a


def encode_samples(src, wins, enc_args, out_path):
    args = _inputs(src.path, wins)
    if len(wins) > 1:
        streams = "".join(f"[{i}:v:0]" for i in range(len(wins)))
        args += ["-filter_complex", f"{streams}concat=n={len(wins)}:v=1:a=0[v]", "-map", "[v]"]
    else:
        args += ["-map", "0:v:0"]
    args += ["-an", "-fps_mode", "passthrough"] + enc_args + src.color_args() + ["-f", "mp4", out_path]
    rc, err = run_ffmpeg(args)
    if rc != 0 or not os.path.isfile(out_path):
        raise RuntimeError(L("sample encode failed: ", "пробное кодирование не удалось: ") + err.strip()[-300:])
    return os.path.getsize(out_path)


def measure_vmaf(dist_path, dist_wins, src, src_wins, workdir):
    """VMAF of the compressed video against the original. dist_wins=None - the compressed file is taken whole."""
    if dist_wins is None:
        args = ["-i", dist_path]
        nd = 1
    else:
        args = _inputs(dist_path, dist_wins)
        nd = len(dist_wins)
    args += _inputs(src.path, src_wins)
    ns = len(src_wins)

    def chain(first, count, label):
        ins = "".join(f"[{i}:v:0]" for i in range(first, first + count))
        pre = f"{ins}concat=n={count}:v=1:a=0," if count > 1 else ins
        return f"{pre}settb=1/1000,setpts=N[{label}]"

    log = "vmaf.json"  # relative to workdir: Windows paths would need escaping inside the filter graph
    fc = (f"{chain(0, nd, 'd')};{chain(nd, ns, 'r')};"
          f"[d][r]libvmaf=n_threads={os.cpu_count() or 8}:shortest=1:log_fmt=json:log_path={log}")
    args += ["-filter_complex", fc, "-f", "null", "-"]
    rc, err = run_ffmpeg(args, cwd=workdir)
    lp = os.path.join(workdir, log)
    if rc != 0 or not os.path.isfile(lp):
        raise RuntimeError(L("VMAF measurement failed: ", "не удалось измерить VMAF: ") + err.strip()[-300:])
    with open(lp, encoding="utf-8") as fh:
        j = json.load(fh)
    os.remove(lp)
    scores = sorted(fr["metrics"]["vmaf"] for fr in j["frames"])
    if not scores:
        raise RuntimeError(L("VMAF: no frames to compare", "VMAF: нет кадров для сравнения"))
    n = len(scores)
    return {"mean": sum(scores) / n, "p5": scores[n // 20], "min": scores[0], "frames": n}


def quality_ok(m, target):
    """The mean VMAF meets the target and no individual frames drop noticeably."""
    return m["mean"] >= target and m["p5"] >= target - 4


def search_quality(src, enc_name, preset, target, min_saving, workdir, fixed_q=None, orig_size=None):
    """Find the strongest compression at which quality on the sample segments stays at the target.

    Returns dict(q, vmaf, p5, ratio, pred_size, tries) or dict(q=None, why=...).
    """
    fn, grid, start, _, _ = ENCODERS[enc_name]
    nwin = min(8, 3 + int(src.duration // 600))  # longer recordings get more samples
    wins = src.windows([(i + 1) / (nwin + 1) for i in range(nwin)])
    src_bytes = max(1, src.window_bytes(wins))
    tries = []
    sample = os.path.join(workdir, "sample.mp4")

    def evaluate(q):
        size = encode_samples(src, wins, fn(q, src.gop, preset), sample)
        m = measure_vmaf(sample, None, src, wins, workdir)
        ratio = size / src_bytes
        pred = int(src.video_bytes * ratio) + src.audio_bytes
        res = {"q": q, "vmaf": round(m["mean"], 2), "p5": round(m["p5"], 2), "ratio": round(ratio, 4),
               "pred_size": pred, "ok": quality_ok(m, target)}
        tries.append(res)
        status(L(f"   search: q={q} -> VMAF {res['vmaf']:.1f} (5%: {res['p5']:.1f}), size {ratio * 100:.0f}% of the source",
                 f"   подбор: q={q} -> VMAF {res['vmaf']:.1f} (5%: {res['p5']:.1f}), размер {ratio * 100:.0f}% от исходного"))
        return res

    limit = (orig_size or src.size) * (1 - min_saving)
    if fixed_q is not None:
        res = evaluate(fixed_q)
        res["tries"] = tries
        if not res["ok"]:
            return {"q": None, "tries": tries,
                    "why": L(f"quality below the target at q={fixed_q} (VMAF {res['vmaf']})",
                             f"при q={fixed_q} качество ниже планки (VMAF {res['vmaf']})")}
        if res["pred_size"] > limit:
            return {"q": None, "why": L("the file would barely shrink", "файл почти не уменьшится"), "tries": tries}
        return res
    lo, hi = 0, len(grid) - 1
    idx = grid.index(start) if start in grid else len(grid) // 2
    best = None
    while lo <= hi:
        res = evaluate(grid[idx])
        if res["ok"]:
            best = res
            lo = idx + 1
        else:
            if res["pred_size"] > limit:
                break  # quality is still too low and the file is already no smaller than the original - it only gets worse
            hi = idx - 1
        idx = (lo + hi + 1) // 2
    status("")
    if best is None:
        return {"q": None, "incompressible": True, "tries": tries,
                "why": L("cannot be compressed without visible quality loss, even at the highest quality setting",
                         "не сжимается без заметной потери качества даже на самой качественной настройке")}
    if best["pred_size"] > limit:
        return {"q": None, "tries": tries,
                "why": L(f"saving is below {min_saving * 100:.0f}%", f"выигрыш меньше {min_saving * 100:.0f}%")}
    best = dict(best)
    best["tries"] = tries
    return best


DECODE_ERROR_MARKS = ("error while decoding", "concealing", "Invalid NAL", "corrupt", "non-existing PPS", "no frame!",
                      "decode_slice_header", "Invalid data found", "missing picture", "Frame num change")


def _audio_title_args(src):
    """Keep the audio track names (NVIDIA writes them as 'name', most players read 'title')."""
    args = []
    for i, a in enumerate(src.audio):
        name = a.get("tags", {}).get("name") or a.get("tags", {}).get("title")
        if name:
            args += [f"-metadata:s:a:{i}", f"title={name}"]
    return args


def full_encode(src, enc_name, preset, q, tmp_path, note):
    fn = ENCODERS[enc_name][0]
    # passthrough + enc_time_base demux: frames and their timestamps are carried over one to one
    # (no rounding to a 1/fps grid)
    args = ["-i", src.path, "-map", "0:v:0", "-map", "0:a?", "-map_metadata", "0", "-c:a", "copy",
            "-fps_mode", "passthrough", "-enc_time_base", "demux"]
    args += fn(q, src.gop, preset) + src.color_args() + _audio_title_args(src)
    args += ["-metadata", f"comment={note}", "-f", "mp4", tmp_path]
    return run_ffmpeg(args, total_frames=src.kept_frames, label=L("encoding", "кодирование"))


def copy_trim(src, tmp_path):
    """Cut the damaged head off without re-encoding."""
    # make_zero shifts the timestamps instead of writing an edit list - otherwise the first keyframe lands
    # "before the start" and players skip it
    args = ["-noaccurate_seek", "-ss", f"{src.cut_time + 0.002:.6f}", "-i", src.path, "-map", "0:v:0", "-map", "0:a?",
            "-map_metadata", "0", "-c", "copy", "-avoid_negative_ts", "make_zero"]
    args += _audio_title_args(src)
    args += ["-metadata", f"comment=NFixer {VERSION}: damaged head cut ({src.cut_time:.1f} s)", "-f", "mp4", tmp_path]
    return run_ffmpeg(args, total_frames=src.kept_frames, label=L("trimming", "обрезка"))


def verify_output(src, out_path, want_codec, stderr_text, trimmed=False):
    """Verify the finished file. Returns a list of problems (empty - all is well).

    trimmed=True - this is a trimmed damaged file: ffmpeg stumbles over the garbage at the head of the source when
    opening it, so its messages are not treated as errors; the result is checked frame by frame instead.
    """
    issues = []
    low = stderr_text.lower()
    hits = [mk for mk in DECODE_ERROR_MARKS if mk.lower() in low]
    if trimmed:
        chk = inspect_mp4(out_path, deep=True)
        if chk["damage"] != "none":
            issues.append(L("the trimmed file still has damaged frames", "в обрезанном файле остались повреждённые кадры"))
        # full decode: every frame in the index must actually be shown
        p = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0", "-count_frames", "-show_entries",
                            "stream=nb_read_frames", "-of", "csv=p=0", out_path],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        shown = p.stdout.strip().split(",")[0]
        if shown != str(src.kept_frames):
            issues.append(L(f"decoding yields {shown or '?'} frames instead of {src.kept_frames}",
                            f"при декодировании получается кадров: {shown or '?'} вместо {src.kept_frames}"))
        elif p.stderr.strip():
            issues.append(L("the trimmed file decodes with errors", "обрезанный файл декодируется с ошибками"))
    elif hits:
        issues.append(L("decode errors while reading the source (", "при чтении исходника были ошибки декодирования (")
                      + hits[0] + ")")
    elif stderr_text.strip():
        issues.append(L("ffmpeg reported errors: ", "ffmpeg сообщил об ошибках: ")
                      + stderr_text.strip().splitlines()[-1][:200])
    try:
        pr = probe(out_path)
    except RuntimeError as ex:
        return issues + [str(ex)]
    st = pr.get("streams", [])
    v = [s for s in st if s.get("codec_type") == "video"]
    a = [s for s in st if s.get("codec_type") == "audio"]
    if len(v) != 1:
        return issues + [L("the result has no video stream", "в результате нет видеодорожки")]
    v = v[0]
    if want_codec and v.get("codec_name") != want_codec:
        issues.append(L(f"codec {v.get('codec_name')} instead of {want_codec}",
                        f"кодек {v.get('codec_name')} вместо {want_codec}"))
    if (v.get("width"), v.get("height")) != (src.width, src.height):
        issues.append(L("resolution changed", "изменилось разрешение"))
    if int(v.get("nb_frames") or 0) != src.kept_frames:
        issues.append(L(f"{v.get('nb_frames')} frames instead of {src.kept_frames}",
                        f"кадров {v.get('nb_frames')} вместо {src.kept_frames}"))
    odur = float(v.get("duration") or 0)
    if abs(odur - src.kept_duration) > 0.05 + 2 / src.fps:
        issues.append(L(f"duration {odur:.2f} s instead of {src.kept_duration:.2f} s",
                        f"длительность {odur:.2f} с вместо {src.kept_duration:.2f} с"))
    if len(a) != len(src.audio):
        issues.append(L(f"{len(a)} audio tracks instead of {len(src.audio)}",
                        f"звуковых дорожек {len(a)} вместо {len(src.audio)}"))
    else:
        for sa, oa in zip(src.audio, a):
            if oa.get("codec_name") != sa.get("codec_name"):
                issues.append(L("audio codec changed", "изменился кодек звука"))
            if not src.cut_frame and sa.get("nb_frames") and oa.get("nb_frames") != sa.get("nb_frames"):
                issues.append(L(f"audio: {oa.get('nb_frames')} packets instead of {sa.get('nb_frames')}",
                                f"звук: пакетов {oa.get('nb_frames')} вместо {sa.get('nb_frames')}"))
            if src.cut_frame and abs(float(oa.get("duration") or 0) - src.kept_duration) > 0.6:
                issues.append(L("audio: wrong duration after trimming", "звук: неверная длительность после обрезки"))
    return issues


# --------------------------------------------------------------------------------------
# Journal and file list
# --------------------------------------------------------------------------------------
def load_journal():
    done = {}
    if os.path.isfile(JOURNAL):
        with open(JOURNAL, encoding="utf-8") as fh:
            for line in fh:
                try:
                    e = json.loads(line)
                    done[e["path"].lower()] = e
                except Exception:
                    continue
    return done


def journal(entry):
    os.makedirs(LOG_DIR, exist_ok=True)
    entry["when"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(JOURNAL, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def collect(paths, days=None, match=None, rejects=None):
    """Collect .mp4 files from the list of paths. Pairs (path, reason) for what cannot be taken go into rejects."""
    files = []
    for p in paths:
        p = os.path.abspath(p)
        why = None
        if os.path.isfile(p):
            if p.lower().endswith(".mp4") and not p.endswith(TMP_SUFFIX):
                files.append(p)
            else:
                why = L("not an .mp4 file (only .mp4 recordings are supported)",
                        "это не файл .mp4 (поддерживаются только записи .mp4)")
        elif os.path.isdir(p):
            for dp, dn, fn in os.walk(p):
                for x in fn:
                    if x.lower().endswith(".mp4") and not x.endswith(TMP_SUFFIX):
                        files.append(os.path.join(dp, x))
        else:
            why = L("file or folder not found", "файл или папка не найдены")
        if why:
            say(L(f"Skipped: {p} - {why}", f"Пропуск: {p} - {why}"))
            if rejects is not None:
                rejects.append((p, why))
    if match:
        files = [f for f in files if match.lower() in f.lower()]
    if days:
        limit = time.time() - days * 86400
        files = [f for f in files if os.path.getmtime(f) >= limit]
    return sorted(set(files))


def out_folder_error(out, paths):
    """The output folder must not be a source folder or lie inside one, or the next run would pick up the results."""
    a = os.path.normcase(os.path.abspath(out))
    for p in paths:
        b = os.path.normcase(os.path.abspath(p if os.path.isdir(p) else os.path.dirname(os.path.abspath(p))))
        if a == b or (os.path.isdir(p) and a.startswith(b.rstrip(os.sep) + os.sep)):
            return L("The output folder must not be the folder of the source recordings or lie inside it.",
                     "Папка для результатов не должна совпадать с папкой исходных записей или лежать внутри неё.")
    return None


def cleanup_tmp(paths):
    """Delete unfinished temporary files of earlier runs (only our own, with the .nfixer-tmp.mp4 suffix)."""
    for p in paths:
        if os.path.isdir(p):
            for dp, dn, fn in os.walk(p):
                for x in fn:
                    if x.endswith(TMP_SUFFIX):
                        try:
                            os.remove(os.path.join(dp, x))
                        except OSError:
                            pass


def rel_dest(path, roots, out_root):
    for r in roots:
        r = os.path.abspath(r)
        if os.path.isdir(r) and os.path.commonpath([r, path]) == r:
            return os.path.join(out_root, os.path.relpath(path, r))
    return os.path.join(out_root, os.path.basename(path))


# --------------------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------------------
def cmd_check(a):
    files = collect(a.paths, a.days, a.match)
    say(L(f"Checking {len(files)} files...", f"Проверяю {len(files)} файлов..."))
    bad = []
    lost = 0.0
    for i, p in enumerate(files):
        status(f"   {i + 1}/{len(files)} {os.path.basename(p)}")
        try:
            r = inspect_mp4(p)
        except Exception as ex:
            r = {"damage": "error", "err": str(ex)}
        d = r["damage"]
        if d == "head":
            bad.append(p)
            lost += r["cut_time"]
            status("")
            say(L(f"DAMAGED    {p}\n           first {r['cut_time']:.1f} s of {r['vdur']:.0f} s overwritten, "
                  f"bitrate {r['vmbps']:.0f} Mbit/s",
                  f"ПОВРЕЖДЁН  {p}\n           первые {r['cut_time']:.1f} с из {r['vdur']:.0f} с перезаписаны, "
                  f"битрейт {r['vmbps']:.0f} Мбит/с"))
        elif d in ("total", "no-moov", "truncated", "error"):
            bad.append(p)
            status("")
            what = {"total": L("the whole video is damaged", "видео испорчено целиком"),
                    "no-moov": L("the file was not finalised (no index)", "файл не дописан (нет индекса)"),
                    "truncated": L("the file is truncated", "файл обрезан"),
                    "error": L("unreadable: ", "не читается: ") + r.get("err", "")}[d]
            say(L(f"DAMAGED    {p}\n           {what}", f"ПОВРЕЖДЁН  {p}\n           {what}"))
        elif a.all:
            status("")
            rate = L(f"  ({r['vmbps']:.0f} Mbit/s)", f"  ({r['vmbps']:.0f} Мбит/с)") if r.get("vmbps") else ""
            say(f"ok         {p}{rate}")
    status("")
    if bad:
        say(L(f"\n{len(bad)} of {len(files)} damaged; total length of the lost heads: {hms(lost)}.",
              f"\nПовреждено {len(bad)} из {len(files)}; потеряно начало общей длительностью {hms(lost)}."))
        say(L("Trimming (repair) removes the damaged head without re-encoding; compressing (compress) removes it and "
              "compresses.",
              "Обрезка (repair) уберёт испорченное начало без перекодирования, сжатие (compress) - уберёт и сожмёт."))
    else:
        say(L(f"No damaged recordings ({len(files)} files checked).",
              f"Повреждённых записей нет ({len(files)} файлов проверено)."))
    return 1 if bad else 0


def cmd_estimate(a):
    files = collect(a.paths, a.days, a.match)
    rnd = random.Random(a.seed)
    rnd.shuffle(files)
    work = work_dir()
    rows = []
    try:
        for p in files:
            if len(rows) >= a.files:
                break
            try:
                src = Source(p)
            except Exception as ex:
                say(L(f"skipped {os.path.basename(p)}: {ex}", f"пропуск {os.path.basename(p)}: {ex}"))
                continue
            if src.codec != "h264" or src.reason or src.insp["damage"] not in ("none", "n/a"):
                continue
            t0 = time.time()
            try:
                res = search_quality(src, a.encoder, a.preset, a.vmaf, a.min_saving, work, a.quality)
            except RuntimeError as ex:
                say(L(f"skipped {os.path.basename(p)}: {ex}", f"пропуск {os.path.basename(p)}: {ex}"))
                continue
            pred = res["pred_size"] if res.get("q") is not None else src.size
            rows.append((src, res, pred))
            tag = f"q={res['q']} VMAF {res['vmaf']:.1f}" if res.get("q") is not None else res["why"]
            mbps = src.video_bytes * 8 / src.duration / 1e6
            say(f"{len(rows):3}. {src.height}p {mbps:4.0f} {L('Mbit/s', 'Мбит/с')}  {human(src.size):>9} -> "
                f"{human(pred):>9} ({100 - pred * 100 / src.size:3.0f}%)  {tag}  [{time.time() - t0:.0f} {L('s', 'с')}]  "
                f"{os.path.basename(p)[:50]}")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if rows:
        s0 = sum(r[0].size for r in rows)
        s1 = sum(r[2] for r in rows)
        say(L(f"\nSample of {len(rows)} files: {human(s0)} -> {human(s1)}, saving {100 - s1 * 100 / s0:.0f}%.",
              f"\nПо выборке из {len(rows)} файлов: {human(s0)} -> {human(s1)}, экономия {100 - s1 * 100 / s0:.0f}%."))
    return 0


# --------------------------------------------------------------------------------------
# Time forecast. Work is measured in "megapixel-frames": frame count x width x height / 1e6.
# The rates come from real runs (1440p60, hevc_nvenc p5) and are refined from the journal and as the job goes.
# --------------------------------------------------------------------------------------
T_SKIP = 3.0  # a file that is only inspected and skipped
T_FIXED = 60.0  # quality search and checks: almost independent of the clip length
T_HEAD = (60.0, 2.0e-3)  # cutting a damaged head: copy + frame-by-frame check
DEFAULT_RATE = {"nvenc": 1.15e-3, "av1": 1.3e-3, "x265": 6.0e-3}  # seconds per megapixel-frame


def work_of(insp):
    """Amount of work from inspect_mp4 facts (without the head that will be cut, if known)."""
    frames = insp.get("frames", 0) - (insp.get("cut_frame", 0) if insp.get("damage") == "head" else 0)
    return max(0, frames) * insp.get("width", 0) * insp.get("height", 0) / 1e6


def learned_rate(done, encoder):
    """Speed of this machine from earlier runs in the journal (median); a typical value if there is too little data."""
    vals = sorted((e["seconds"] - T_FIXED) / e["work"] for e in done.values()
                  if e.get("status") == "compressed" and e.get("encoder") == encoder and e.get("work")
                  and e.get("seconds") and not e.get("cut_seconds"))
    vals = [v for v in vals if v > 0]
    if len(vals) < 3:
        return DEFAULT_RATE.get(encoder, 1.2e-3)
    return min(30e-3, max(0.2e-3, vals[len(vals) // 2]))


def predict_seconds(insp, repair, rate):
    """Roughly how many seconds a file will take."""
    dmg, work = insp.get("damage"), work_of(insp)
    if not work or dmg in ("total", "no-moov", "truncated"):
        return T_SKIP
    head = T_HEAD[0] + T_HEAD[1] * work if dmg == "head" else 0.0
    if repair:
        return head or T_SKIP
    if insp.get("codec") not in ("avc1", "avc3"):
        return T_SKIP
    return T_FIXED + rate * work + head


def unreadable_reason(path, ex):
    """A plain explanation of why the file could not be opened as video."""
    try:
        if os.path.getsize(path) == 0:
            return L("the file is empty (0 bytes)", "файл пустой (0 байт)")
        if inspect_mp4(path, light=True).get("damage") == "no-moov":
            return L("not a finished MP4 recording: the index is missing (the recording was cut off, or this is not a "
                     "video at all) - nothing can be recovered",
                     "это не завершённая запись MP4: в файле нет индекса (запись оборвалась либо это вообще не видео) - "
                     "восстановить нечего")
    except Exception:
        pass
    text = str(ex).strip().splitlines()[-1][:160] if str(ex).strip() else ""
    return (L("the file cannot be read as video - it is broken or is not a video",
              "файл не читается как видео - он сломан или это не видео") + (f" ({text})" if text else ""))


BROKEN_WHY = {
    "total": ("the video is damaged from start to end - there is nothing left to save",
              "видео испорчено от начала до конца - спасать нечего"),
    "no-moov": ("the recording was never finalised (no index) - nothing can be recovered",
                "запись не была завершена (нет индекса) - восстановить нечего"),
    "truncated": ("the file is cut short (part of the video is missing) - left as is",
                  "файл обрезан (части видео нет) - оставлен как есть"),
}

UNCHANGED_STATUSES = ("kept", "skip-codec", "skip-unsupported")  # healthy videos that stayed as they were


def verdict(a):
    """Settings the "leave as is" decision depends on: with other settings the file has to be looked at again."""
    return {"target": a.vmaf, "encoder": a.encoder, "min_saving": a.min_saving, "fixed_q": a.quality}


def same_verdict(e, a, repair):
    """Is the earlier decision about the file still valid for this run?"""
    if e.get("status") not in ("kept", "skip-codec"):
        return True  # a broken file or an unsupported format stays so with any settings
    if repair:
        return False  # a compression decision says nothing about whether there is anything to cut
    if e.get("status") == "skip-codec":
        return True
    # entries from older versions have no settings - those were the defaults
    return (e.get("target", 95.0) == a.vmaf and e.get("encoder", "nvenc") == a.encoder
            and abs(e.get("min_saving", 0.15) - a.min_saving) < 1e-9 and e.get("fixed_q") == a.quality)


def cmd_compress(a):
    return BatchRun(a, repair=False).run()


def cmd_repair(a):
    return BatchRun(a, repair=True).run()


class Halt(Exception):
    """Stop the whole batch (for example, the output drive is full)."""


class BatchRun:
    """One pass of compress or repair over the file list.

    only - process just these paths (retry of problem files); the folder layout still comes from a.paths.
    """

    def __init__(self, a, repair, only=None):
        self.a = a
        self.repair = repair
        self.only = only
        self.problems = []  # every file to report at the end: dict(path, kind, detail, ...)
        self.stats = {"done": 0, "in": 0, "out": 0, "kept": 0, "failed": 0, "copied": 0, "copied_bytes": 0}
        self.gui = bool(os.environ.get("NFIXER_GUI"))
        self.tmp = None  # the temporary file being written now (removed on errors and Ctrl+C)
        self.done = {}
        self.preds = []
        self.rate = 0.0
        self.scale = 1.0
        self.copy_kept = False
        self.work = None

    # ------------------------------------------------------------------ helpers
    def problem(self, path, kind, detail, **extra):
        self.problems.append(dict(extra, path=path, kind=kind, detail=detail))

    def _files(self):
        """Files to look at, plus problems found while collecting them."""
        a = self.a
        rejects = []
        files = collect(a.paths, a.days, a.match, rejects)
        only = self.only
        if only is None and getattr(a, "only", None):
            with open(a.only, encoding="utf-8-sig") as fh:
                only = [ln.strip() for ln in fh if ln.strip()]
        if only is not None:
            keep = {os.path.normcase(os.path.abspath(x)) for x in only}
            files = [f for f in files if os.path.normcase(f) in keep]
            rejects = []
        for p, why in rejects:
            self.problem(p, "unsupported", why)
        return files

    def _announce(self, n, t0, insp=None):
        """Report the forecast for this file and for the rest of the job (more accurate once the file is inspected)."""
        if insp is not None:
            try:
                self.prev[1] = self.preds[n - 1] = predict_seconds(insp, self.repair, self.rate)
            except Exception:
                pass
        one, rest = self.preds[n - 1] * self.scale, sum(self.preds[n - 1:]) * self.scale
        if self.gui:
            print("@@#" + json.dumps({"n": n, "total": len(self.preds), "file": round(one), "all": round(rest),
                                      "spent": round(time.time() - t0)}), flush=True)
        return one, rest

    def _check_space(self, folder, need):
        free = shutil.disk_usage(folder).free
        return free if free < need + 3 * 2**30 else None

    def place_unchanged(self, p, src=None, trimmed=None):
        """--copy-kept: put a file that was not compressed into the output folder, so it holds the full set.

        A recording with a damaged head is copied trimmed (trimmed - an already trimmed copy, if there is one):
        nobody needs the damaged part. A copy error does not stop the job; it goes into the problem list.
        """
        a = self.a
        if not self.copy_kept:
            return
        dest = rel_dest(p, a.paths, a.out)
        if os.path.exists(dest):
            return
        try:
            orig_st = os.stat(p)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            free = self._check_space(os.path.dirname(dest), orig_st.st_size)
            if free is not None:
                raise OSError(L(f"only {human(free)} left on the output drive",
                                f"на диске назначения осталось только {human(free)}"))
            self.tmp = dest + TMP_SUFFIX
            if trimmed and os.path.exists(trimmed):
                os.replace(trimmed, self.tmp)
                how = L("with the damaged head cut off", "с отрезанным испорченным началом")
            else:
                if src is None:
                    src = Source(p)
                if src.insp.get("damage") == "head" and a.damaged == "trim":
                    src.cut_frame, src.cut_time = src.insp["cut_frame"], src.insp["cut_time"]
                    rc, err = copy_trim(src, self.tmp)
                    bad = [err.strip()[-300:]] if rc != 0 else verify_output(src, self.tmp, None, err, trimmed=True)
                    if bad:
                        raise OSError(L("could not cut the damaged head: ", "не удалось отрезать испорченное начало: ")
                                      + "; ".join(bad))
                    how = L("with the damaged head cut off", "с отрезанным испорченным началом")
                else:
                    status(L(f"   copying {os.path.basename(p)}", f"   копирую {os.path.basename(p)}"))
                    shutil.copyfile(p, self.tmp)
                    status("")
                    how = L("as is", "как есть")
            os.replace(self.tmp, dest)
            self.tmp = None
            set_file_times(dest, orig_st)
            self.stats["copied"] += 1
            self.stats["copied_bytes"] += os.path.getsize(dest)
            say(L(f"   copied to the output folder {how}", f"   скопирован в папку результатов {how}"))
        except (OSError, RuntimeError) as ex:
            if self.tmp and os.path.exists(self.tmp):
                os.remove(self.tmp)
            self.tmp = None
            say(L(f"   ERROR while copying to the output folder: {ex}", f"   ОШИБКА при копировании в папку результатов: {ex}"))
            self.problem(p, "error", L("not copied to the output folder: ", "не скопирован в папку результатов: ")
                         + str(ex)[:300])

    # ------------------------------------------------------------------ the whole batch
    def run(self):
        a = self.a
        files = self._files()
        if a.out:
            a.out = os.path.abspath(a.out)
        if not a.replace and not a.out and not a.dry_run:
            sys.exit(L("Say where to put the result: --out FOLDER (originals are left alone) or --replace (the original "
                       "is replaced after verification).",
                       "Укажите, куда класть результат: --out ПАПКА (оригиналы не трогаются) или --replace (оригинал "
                       "заменяется после проверки)."))
        if a.out and not a.replace:
            err = out_folder_error(a.out, a.paths)
            if err:
                sys.exit(err)
        cleanup_tmp(a.paths + ([a.out] if a.out else []))
        self.done = load_journal()
        self.copy_kept = bool(getattr(a, "copy_kept", False) and a.out and not a.replace)
        todo = []
        copy_only = []  # looked at earlier and left unchanged - these only need copying
        for p in files:
            e = self.done.get(p.lower())
            try:
                st = os.stat(p)
            except OSError:
                continue
            if (e and not a.retry and e.get("size") == st.st_size and e.get("mtime_ns") == st.st_mtime_ns
                    and e.get("final") and same_verdict(e, a, self.repair)):
                if self.copy_kept and e.get("status") in UNCHANGED_STATUSES:
                    copy_only.append(p)
                continue  # this file in this state and with these settings has been looked at already
            todo.append((p, st.st_size))
        todo.sort(key=lambda x: -x[1])
        if a.limit:
            todo = todo[: a.limit]
        say(L(f"To process: {len(todo)} files, {human(sum(s for _, s in todo))} "
              f"(already handled earlier: {len(files) - len(todo)}).",
              f"К обработке: {len(todo)} файлов, {human(sum(s for _, s in todo))} "
              f"(уже рассмотрено ранее: {len(files) - len(todo)})."))
        if copy_only:
            say(L(f"Left unchanged earlier and to be copied to the output folder as is: {len(copy_only)}.",
                  f"Оставлены без изменений раньше и будут скопированы в папку результатов как есть: {len(copy_only)}."))
        if a.dry_run:
            for p, s in todo[:30]:
                say(f"   {human(s):>10}  {p}")
            if len(todo) > 30:
                say(L(f"   ... and {len(todo) - 30} more", f"   ... и ещё {len(todo) - 30}"))
            return 0

        # Time forecast: a quick look at every file's index (frames are not read), then refined as the job goes.
        self.rate = learned_rate(self.done, a.encoder) if not self.repair else 0.0
        for i, (p, _) in enumerate(todo, 1):
            status(L(f"   estimating the time: file {i} of {len(todo)}", f"   оцениваю время: файл {i} из {len(todo)}"))
            try:
                self.preds.append(predict_seconds(inspect_mp4(p, light=True), self.repair, self.rate))
            except Exception:
                self.preds.append(T_SKIP)
        status("")
        if todo:
            say(L(f"Estimated time: about {hms(sum(self.preds))} (rough; it is refined as the work goes).",
                  f"Ожидаемое время: около {hms(sum(self.preds))} (грубо; уточняется по ходу работы)."))

        self.work = work_dir()
        t_start = time.time()
        spent = planned = 0.0  # actual and forecast time of the files done so far - to correct the forecast
        self.prev = None
        halted = interrupted = False
        try:
            for n, (p, size) in enumerate(todo, 1):
                if a.max_hours and time.time() - t_start > a.max_hours * 3600:
                    say(L("Time limit reached (--max-hours), stopping.", "Достигнут лимит времени (--max-hours), останавливаюсь."))
                    halted = True
                    break
                if self.prev:
                    spent += time.time() - self.prev[0]
                    planned += self.prev[1]
                self.scale = min(4.0, max(0.25, spent / planned)) if planned >= 30 else 1.0
                try:
                    self.process_file(n, len(todo), p, size)
                except Halt:
                    halted = True
                    break
            pending = [p for p in copy_only if not os.path.exists(rel_dest(p, a.paths, a.out))]
            if pending and not halted:
                say(L(f"\nCopying files left unchanged in earlier runs: {len(pending)}",
                      f"\nКопирую файлы, оставленные без изменений в прошлых запусках: {len(pending)}"))
                for i, p in enumerate(pending, 1):
                    say(f"({i}/{len(pending)}) {p}")
                    self.place_unchanged(p)
        except KeyboardInterrupt:
            interrupted = True
            status("")
            say(L("\nStopped by the user. The unfinished file was removed, the original is intact; the next run will "
                  "continue.",
                  "\nОстановлено пользователем. Незавершённый файл удалён, оригинал цел; при следующем запуске работа "
                  "продолжится."))
            if _current_proc:
                try:
                    _current_proc.wait(timeout=15)
                except Exception:
                    _current_proc.kill()
            if self.tmp and os.path.exists(self.tmp):
                try:
                    os.remove(self.tmp)
                except OSError:
                    pass
        finally:
            shutil.rmtree(self.work, ignore_errors=True)
        self.report(t_start)
        return self.finish(interrupted)

    # ------------------------------------------------------------------ one file
    def process_file(self, n, total, p, size):
        """Process one file. Raises Halt when the whole batch has to stop."""
        a, repair, stats = self.a, self.repair, self.stats
        say(f"[{n}/{total}] {p}  ({human(size)})")
        t0 = time.time()
        self.prev = [t0, self.preds[n - 1]]
        prev_entry = self.done.get(p.lower())
        entry = {"path": p, "size": size, "mtime_ns": os.stat(p).st_mtime_ns, "final": True}
        self._announce(n, t0)
        try:
            src = Source(p)
        except Exception as ex:
            why = unreadable_reason(p, ex)
            say(L(f"   skipped: {why}", f"   пропуск: {why}"))
            journal(dict(entry, status="skip-broken", detail=why[:300], final=False))
            self.problem(p, "broken", why)
            stats["failed"] += 1
            return
        orig_st = src.st
        dmg = src.insp["damage"]
        one, rest = self._announce(n, t0, dict(src.insp, width=src.width, height=src.height,
                                               frames=src.insp.get("frames") or src.nb_frames,
                                               codec=src.insp.get("codec") or ("avc1" if src.codec == "h264" else src.codec)))
        if not repair and src.codec != "h264":
            say(L(f"   skipped: the video is already {src.codec.upper()}", f"   пропуск: видео уже в {src.codec.upper()}"))
            journal(dict(entry, status="skip-codec", detail=src.codec))
            self.place_unchanged(p, src)
            return
        if src.reason:
            say(L(f"   skipped: {src.reason}", f"   пропуск: {src.reason}"))
            journal(dict(entry, status="skip-unsupported", detail=src.reason))
            self.problem(p, "unsupported", src.reason)
            self.place_unchanged(p, src)
            return
        if dmg in ("total", "no-moov", "truncated"):
            why = L(*BROKEN_WHY[dmg])
            say(L(f"   skipped: {why}", f"   пропуск: {why}"))
            journal(dict(entry, status="skip-broken", detail=dmg))
            self.problem(p, "broken", why)
            return
        if dmg == "head" or not repair:
            say(L(f"   expected time: this file about {hms(one)}, everything left about {hms(rest)}",
                  f"   ожидаемое время: этот файл около {hms(one)}, весь остаток около {hms(rest)}"))
        if dmg == "head":
            if a.damaged == "skip" and not repair:
                say(L(f"   skipped: the head ({src.insp['cut_time']:.1f} s) was overwritten by the replay buffer; "
                      "use --damaged trim",
                      f"   пропуск: начало ({src.insp['cut_time']:.1f} с) испорчено переполнением буфера; "
                      "используйте --damaged trim"))
                journal(dict(entry, status="skip-damaged", final=False))
                self.place_unchanged(p, src)
                return
            src.cut_frame, src.cut_time = src.insp["cut_frame"], src.insp["cut_time"]
            say(L(f"   damaged head: cutting the first {src.cut_time:.1f} s (no video exists there), "
                  f"{src.kept_duration:.1f} s remain",
                  f"   начало испорчено: отрезаю первые {src.cut_time:.1f} с (видео там не существует), "
                  f"остаётся {src.kept_duration:.1f} с"))
        elif repair:
            say(L("   intact - nothing to cut", "   запись цела - обрезать нечего"))
            self.place_unchanged(p, src)
            return
        dest = p if a.replace else rel_dest(p, a.paths, a.out)
        if not a.replace and os.path.exists(dest):
            say(L("   skipped: the result already exists in the output folder",
                  "   пропуск: результат уже есть в папке назначения"))
            return
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        free = self._check_space(os.path.dirname(dest), size)
        if free is not None:
            say(L(f"   Only {human(free)} left on the output drive - not enough, stopping.",
                  f"   На диске назначения осталось {human(free)} - этого мало, останавливаюсь."))
            raise Halt()
        self.tmp = tmp = dest + TMP_SUFFIX
        rep = dest + ".rep" + TMP_SUFFIX  # trimmed copy of a damaged file - the source for compression
        cut_time = src.cut_time
        trimmed = rep if cut_time else None
        try:
            if repair:
                rc, err = copy_trim(src, tmp)
                want, info = None, {"q": None}
            else:
                if src.cut_frame:
                    rc, err = copy_trim(src, rep)
                    issues = ([L("ffmpeg failed: ", "ffmpeg завершился с ошибкой: ") + err.strip()[-300:]] if rc != 0
                              else verify_output(src, rep, None, err, trimmed=True))
                    if issues:
                        raise RuntimeError(L("could not cut the damaged head: ", "не удалось отрезать испорченное начало: ")
                                           + "; ".join(issues))
                    src = Source(rep)  # from here on work with the clean copy: any decode errors are now real
                    if src.reason or src.insp["damage"] != "none":
                        raise RuntimeError(L("the trimmed copy failed verification", "обрезанная копия не прошла проверку"))
                grid = ENCODERS[a.encoder][1]
                fixed_q = a.quality
                if (getattr(a, "better", False) and prev_entry and prev_entry.get("status") == "quality-failed"
                        and prev_entry.get("encoder") == a.encoder and prev_entry.get("q") in grid[1:]):
                    fixed_q = grid[grid.index(prev_entry["q"]) - 1]
                    say(L(f"   one step higher quality than last time: q={fixed_q} instead of q={prev_entry['q']}",
                          f"   на ступень качественнее, чем в прошлый раз: q={fixed_q} вместо q={prev_entry['q']}"))
                info = search_quality(src, a.encoder, a.preset, a.vmaf, a.min_saving, self.work, fixed_q, orig_size=size)
                if info.get("q") is None:
                    say(L(f"   left as is: {info['why']}", f"   оставлен как есть: {info['why']}"))
                    journal(dict(entry, **verdict(a), status="kept", detail=info["why"], tries=info.get("tries")))
                    stats["kept"] += 1
                    if info.get("incompressible"):
                        self.problem(p, "incompressible", info["why"])
                    self.place_unchanged(p, trimmed=trimmed)
                    return
                say(L(f"   quality q={info['q']}: VMAF {info['vmaf']:.1f} on samples, predicted size {human(info['pred_size'])}",
                      f"   качество q={info['q']}: VMAF {info['vmaf']:.1f} на пробах, прогноз {human(info['pred_size'])}"))
                note = f"NFixer {VERSION}: {a.encoder} q={info['q']} vmaf={info['vmaf']} src=h264 {size} bytes"
                if cut_time:
                    note += f", cut {cut_time:.1f}s damaged head"
                rc, err = full_encode(src, a.encoder, a.preset, info["q"], tmp, note)
                want = ENCODERS[a.encoder][3]
            if rc != 0:
                raise RuntimeError(L("ffmpeg failed: ", "ffmpeg завершился с ошибкой: ") + err.strip()[-300:])
            issues = verify_output(src, tmp, want, err, trimmed=repair)
            out_size = os.path.getsize(tmp)
            if not repair and not issues and out_size > size * (1 - a.min_saving):
                os.remove(tmp)
                say(L(f"   left as is: the result is {human(out_size)} - saving below {a.min_saving * 100:.0f}%",
                      f"   оставлен как есть: результат {human(out_size)} - выигрыш меньше {a.min_saving * 100:.0f}%"))
                journal(dict(entry, **verdict(a), status="kept", detail="not smaller", out_size=out_size, q=info["q"]))
                stats["kept"] += 1
                self.place_unchanged(p, trimmed=trimmed)
                return
            spot = spot_fail = None
            if not repair and not issues:
                # control measurement on the finished file at another place in the clip
                sw = src.windows([0.37] if src.duration < 600 else [0.12, 0.63, 0.91])
                shift = video_start(tmp) - src.start  # the result's first frame may sit at another timestamp
                spot = measure_vmaf(tmp, [(max(0.0, t + shift), d) for t, d in sw], src, sw, self.work)
                # A guard against gross defects, not a second quality search: the sample search holds the target,
                # the tolerance here is wider, or a clip with one hard scene would be rejected over and over.
                if not (spot["mean"] >= a.vmaf - 2 and spot["p5"] >= a.vmaf - 12):
                    spot_fail = L(f"VMAF {spot['mean']:.1f} (worst 5%: {spot['p5']:.1f}) at q={info['q']}, "
                                  f"required {a.vmaf - 2:.0f} ({a.vmaf - 12:.0f})",
                                  f"VMAF {spot['mean']:.1f} (худшие 5%: {spot['p5']:.1f}) при q={info['q']}, "
                                  f"нужно {a.vmaf - 2:.0f} ({a.vmaf - 12:.0f})")
            if issues:
                os.remove(tmp)
                say(L("   VERIFICATION ERROR, original untouched: ", "   ОШИБКА ПРОВЕРКИ, оригинал не тронут: ")
                    + "; ".join(issues))
                journal(dict(entry, status="verify-failed", detail="; ".join(issues)[:500], final=False))
                self.problem(p, "error", "; ".join(issues))
                stats["failed"] += 1
                return
            if spot_fail:
                # The finished file is worse than the samples promised. The user decides: remember it, move on and
                # offer a retry at the end.
                os.remove(tmp)
                if info["q"] in grid[1:]:
                    nq = grid[grid.index(info["q"]) - 1]
                    say(L(f"   QUALITY CHECK FAILED, original untouched: {spot_fail}. Skipped; it can be retried one "
                          f"step higher quality (q={nq}).",
                          f"   ПРОВЕРКА КАЧЕСТВА НЕ ПРОЙДЕНА, оригинал не тронут: {spot_fail}. Пропущен; можно повторить "
                          f"на ступень качественнее (q={nq})."))
                    journal(dict(entry, status="quality-failed", detail=spot_fail, q=info["q"], encoder=a.encoder,
                                 final=False))
                    self.problem(p, "retry", spot_fail, q=info["q"], next_q=nq)
                    stats["failed"] += 1
                else:
                    why = L(f"fails the quality check even at the highest quality setting ({spot_fail})",
                            f"не проходит проверку качества даже на самой качественной настройке ({spot_fail})")
                    say(L(f"   left as is: {why}", f"   оставлен как есть: {why}"))
                    journal(dict(entry, **verdict(a), status="kept", detail=why))
                    self.problem(p, "incompressible", why)
                    stats["kept"] += 1
                    self.place_unchanged(p, trimmed=trimmed)
                return
            os.replace(tmp, dest)
            self.tmp = None
            set_file_times(dest, orig_st)
            nst = os.stat(dest)
            journal({"path": dest, "size": nst.st_size, "mtime_ns": nst.st_mtime_ns, "final": True,
                     "status": "repaired" if repair else "compressed", "src_path": p, "src_size": size,
                     "q": info.get("q"), "vmaf": info.get("vmaf"), "spot_vmaf": round(spot["mean"], 2) if spot else None,
                     "cut_seconds": round(cut_time, 2), "encoder": None if repair else a.encoder,
                     "seconds": round(time.time() - t0), "work": round(src.kept_frames * src.width * src.height / 1e6)})
            stats["done"] += 1
            stats["in"] += size
            stats["out"] += out_size
            extra = L(f", spot-check VMAF {spot['mean']:.1f}", f", контроль VMAF {spot['mean']:.1f}") if spot else ""
            saved = 100 - out_size * 100 / size
            say(L(f"   done: {human(size)} -> {human(out_size)} (-{saved:.0f}%){extra}, {hms(time.time() - t0)}",
                  f"   готово: {human(size)} -> {human(out_size)} (-{saved:.0f}%){extra}, {hms(time.time() - t0)}"))
        except RuntimeError as ex:
            if os.path.exists(tmp):
                os.remove(tmp)
            say(L(f"   ERROR, original untouched: {ex}", f"   ОШИБКА, оригинал не тронут: {ex}"))
            journal(dict(entry, status="error", detail=str(ex)[:500], final=False))
            self.problem(p, "error", str(ex)[:300])
            stats["failed"] += 1
        finally:
            if self.tmp == tmp and not os.path.exists(tmp):
                self.tmp = None
            try:
                if os.path.exists(rep):
                    os.remove(rep)
            except OSError:
                pass

    # ------------------------------------------------------------------ summary
    def report(self, t_start):
        stats = self.stats
        say(L(f"\nSummary: {stats['done']} processed, {stats['kept']} left as is, {stats['failed']} errors, "
              f"time {hms(time.time() - t_start)}.",
              f"\nИтог: обработано {stats['done']}, оставлено как есть {stats['kept']}, ошибок {stats['failed']}, "
              f"время {hms(time.time() - t_start)}."))
        if stats["done"]:
            freed, pct = human(stats["in"] - stats["out"]), 100 - stats["out"] * 100 / stats["in"]
            say(L(f"Was {human(stats['in'])}, now {human(stats['out'])} - freed {freed} ({pct:.0f}%).",
                  f"Было {human(stats['in'])}, стало {human(stats['out'])} - освобождено {freed} ({pct:.0f}%)."))
        if stats["copied"]:
            say(L(f"Copied to the output folder without compression: {stats['copied']} files, "
                  f"{human(stats['copied_bytes'])}.",
                  f"Скопировано в папку результатов без сжатия: {stats['copied']} файлов, "
                  f"{human(stats['copied_bytes'])}."))
        say(L(f"Journal: {JOURNAL}", f"Журнал: {JOURNAL}"))
        if not self.problems:
            return
        groups = (("retry", L("Failed the quality check after compression - can be retried one step higher quality:",
                              "Не прошли проверку качества после сжатия - можно повторить на ступень качественнее:")),
                  ("incompressible", L("Cannot be compressed without visible loss - left as is:",
                                       "Не сжимаются без заметных потерь - оставлены как есть:")),
                  ("broken", L("Broken or not a video - nothing was done:", "Сломаны или не являются видео - ничего не сделано:")),
                  ("unsupported", L("Not supported - nothing was done:", "Не поддерживаются - ничего не сделано:")),
                  ("error", L("Errors during processing - the originals are untouched:",
                              "Ошибки при обработке - оригиналы не тронуты:")))
        say(L(f"\nPROBLEM FILES: {len(self.problems)}", f"\nПРОБЛЕМНЫЕ ФАЙЛЫ: {len(self.problems)}"))
        for kind, title in groups:
            rows = [x for x in self.problems if x["kind"] == kind]
            if rows:
                say(" " + title)
                for x in rows:
                    say(f"   ! {x['path']}\n       {x['detail']}")

    def finish(self, interrupted):
        """Exit code, plus the offer to retry files that failed the final quality check."""
        a = self.a
        code = 2 if self.stats["failed"] else 0
        retry = [x["path"] for x in self.problems if x["kind"] == "retry"]
        if self.gui:
            print("@@!" + json.dumps({"retry": retry, "problems": len(self.problems)}), flush=True)
        elif retry and not interrupted:
            ask = L(f"\nCompress these {len(retry)} file(s) again one step higher quality (the files come out larger)? [y/N] ",
                    f"\nСжать эти файлы ({len(retry)}) ещё раз на ступень качественнее (файлы выйдут крупнее)? [y/N] ")
            if sys.stdin and sys.stdin.isatty() and sys.stdout.isatty():
                try:
                    yes = input(ask).strip().lower() in ("y", "yes", "д", "да")
                except EOFError:
                    yes = False
                if yes:
                    a.better = True
                    return BatchRun(a, self.repair, only=retry).run()
            else:
                say(L("To retry them one step higher quality, run the same command again with --better.",
                      "Чтобы повторить их на ступень качественнее, запустите ту же команду ещё раз с параметром --better."))
        return code


def main():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(errors="replace")
        except Exception:
            pass
    global LANG
    LANG = detect_lang(sys.argv[1:])
    ap = argparse.ArgumentParser(prog="nfixer", description=L(
        "Compress game recordings without visible quality loss and find recordings damaged by Instant Replay.",
        "Сжатие игровых записей без видимой потери качества и поиск записей, испорченных Instant Replay."))
    ap.add_argument("--lang", choices=["en", "ru"], help=L(
        "message language: en (default) or ru; the NFIXER_LANG variable works too",
        "язык сообщений: en (по умолчанию) или ru; можно задать и переменной NFIXER_LANG"))
    ap.add_argument("--ffmpeg", help=L("folder with ffmpeg.exe and ffprobe.exe", "папка с ffmpeg.exe и ffprobe.exe"))
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("paths", nargs="*", help=L("folders and/or files", "папки и/или файлы"))
        p.add_argument("--list", help=L("text file (UTF-8) listing folders and files, one path per line",
                                        "текстовый файл (UTF-8) со списком папок и файлов, по одному пути в строке"))
        p.add_argument("--days", type=float, help=L("only files modified within the last N days",
                                                    "только файлы, изменённые за последние N дней"))
        p.add_argument("--match", help=L("only files whose path contains this string",
                                         "только файлы, в пути которых есть эта строка"))

    def quality(p):
        p.add_argument("--encoder", choices=list(ENCODERS), default="nvenc",
                       help="; ".join(f"{k}: {L(*v[4])}" for k, v in ENCODERS.items()))
        p.add_argument("--preset", help=L("encoder preset (nvenc: p1..p7, x265: fast/medium/slow)",
                                          "пресет кодировщика (nvenc: p1..p7, x265: fast/medium/slow)"))
        p.add_argument("--vmaf", type=float, default=95.0, help=L(
            "VMAF quality target against the original (default 95 - visually indistinguishable)",
            "планка качества VMAF относительно оригинала (по умолчанию 95 - визуально неотличимо)"))
        p.add_argument("--quality", type=int, help=L("set the quality parameter by hand instead of searching for it",
                                                     "задать параметр качества вручную вместо автоподбора"))
        p.add_argument("--min-saving", type=float, default=0.15, help=L(
            "minimum saving that justifies touching a file (0.15 = 15%%)",
            "минимальный выигрыш, ради которого стоит трогать файл (0.15 = 15%%)"))

    def batch(p):
        p.add_argument("--replace", action="store_true", help=L(
            "after successful verification replace the original with the result (the original is deleted)",
            "после успешной проверки заменить оригинал результатом (оригинал удаляется)"))
        p.add_argument("--out", help=L("output folder; originals stay in place",
                                       "папка для результатов; оригиналы остаются на месте"))
        p.add_argument("--limit", type=int, help=L("process at most N files", "обработать не больше N файлов"))
        p.add_argument("--max-hours", type=float, help=L("stop after N hours", "остановиться через N часов"))
        p.add_argument("--retry", action="store_true", help=L("retry files that were left as is before",
                                                              "заново попробовать файлы, которые раньше были оставлены как есть"))
        p.add_argument("--copy-kept", action="store_true", help=L(
            "with --out: also copy files left unchanged into the output folder, so it holds the full set (a recording "
            "with a damaged head is copied with the head cut off)",
            "вместе с --out: копировать в папку результатов и файлы, оставленные без изменений, чтобы там был полный "
            "набор (у записи с испорченным началом копируется обрезанная версия)"))
        p.add_argument("--better", action="store_true", help=L(
            "files that failed the quality check last time are compressed one step higher quality",
            "файлы, не прошедшие проверку качества в прошлый раз, сжать на ступень качественнее"))
        p.add_argument("--only", help=L(
            "text file (UTF-8): process only the files listed in it (the main list still defines the folder layout)",
            "текстовый файл (UTF-8): обработать только перечисленные в нём файлы (раскладку по папкам задаёт основной "
            "список)"))
        p.add_argument("--dry-run", action="store_true", help=L("only show what would be processed",
                                                                "только показать, что будет обработано"))

    p = sub.add_parser("check", help=L("find damaged recordings", "найти повреждённые записи"))
    common(p)
    p.add_argument("--all", action="store_true", help=L("also list intact files", "показывать и исправные файлы"))
    p.set_defaults(fn=cmd_check)

    p = sub.add_parser("estimate", help=L("estimate the saving on a random sample", "оценить экономию на случайной выборке"))
    common(p)
    quality(p)
    p.add_argument("--files", type=int, default=12, help=L("how many files to sample", "сколько файлов взять"))
    p.add_argument("--seed", type=int, default=1)
    p.set_defaults(fn=cmd_estimate)

    p = sub.add_parser("compress", help=L("compress recordings", "сжать записи"))
    common(p)
    quality(p)
    batch(p)
    p.add_argument("--damaged", choices=["trim", "skip"], default="trim", help=L(
        "what to do with recordings whose head is damaged: cut it (trim) or leave the file alone (skip)",
        "что делать с записями, у которых испорчено начало: отрезать его (trim) или не трогать файл (skip)"))
    p.set_defaults(fn=cmd_compress)

    p = sub.add_parser("repair", help=L("cut the damaged head without re-encoding",
                                        "отрезать испорченное начало без перекодирования"))
    common(p)
    batch(p)
    p.set_defaults(fn=cmd_repair, encoder=None, preset=None, vmaf=0, quality=None, min_saving=0, damaged="trim")

    a = ap.parse_args()
    if a.list:
        with open(a.list, encoding="utf-8-sig") as fh:
            a.paths += [ln.strip() for ln in fh if ln.strip()]
    if not a.paths:
        ap.error(L("no folders or files given", "не указаны папки или файлы"))
    if a.cmd != "check":
        find_ffmpeg(a.ffmpeg)
    try:
        sys.exit(a.fn(a))
    except KeyboardInterrupt:
        status("")
        say(L("\nStopped.", "\nОстановлено."))
        sys.exit(130)


if __name__ == "__main__":
    main()
