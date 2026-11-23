"""Tests for nfixer.py. Run from the project folder: python -m unittest

Tests marked "needs FFmpeg" generate small clips with ffmpeg (libx264, libx265) and are skipped without it.
VMAF is mocked there, so an FFmpeg build without libvmaf is enough.
"""
import contextlib
import io
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import nfixer as N  # noqa: E402


def has_ffmpeg():
    try:
        N.find_ffmpeg()
    except SystemExit:
        return False
    enc = subprocess.run([N.FFMPEG, "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    return "libx264" in enc and "libx265" in enc


FFMPEG_OK = has_ffmpeg()
needs_ffmpeg = unittest.skipUnless(FFMPEG_OK, "needs FFmpeg with libx264 and libx265")


def make_clip(path, seconds=10, crf=12):
    """A 320x240 30 fps H.264 clip with an AAC track, a keyframe every second."""
    subprocess.run([N.FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", f"testsrc2=size=320x240:rate=30:duration={seconds}",
                    "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
                    "-c:v", "libx264", "-crf", str(crf), "-g", "30", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", path], check=True)


def damage_head(path, fraction=0.3, seed=1):
    """Imitate an Instant Replay overflow: overwrite the start of the media data with garbage."""
    with open(path, "r+b") as f:
        size = os.fstat(f.fileno()).st_size
        mdat = next((s, e) for t, s, e in N._atoms(f, 0, size) if t == b"mdat")
        n = int((mdat[1] - mdat[0]) * fraction)
        f.seek(mdat[0])
        f.write(random.Random(seed).randbytes(n))


def batch_args(paths, out, **kw):
    a = dict(paths=paths, days=None, match=None, only=None, better=False, out=out, replace=False, dry_run=False,
             copy_kept=False, retry=False, limit=None, max_hours=None, encoder="x265", preset="ultrafast", vmaf=95.0,
             min_saving=0.15, quality=None, damaged="trim")
    a.update(kw)
    return types.SimpleNamespace(**a)


class TempDirTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="nfixer-test-")
        self.src = os.path.join(self.dir, "in")
        self.out = os.path.join(self.dir, "out")
        os.makedirs(self.src)
        patches = [mock.patch.object(N, "LOG_DIR", os.path.join(self.dir, "logs")),
                   mock.patch.object(N, "JOURNAL", os.path.join(self.dir, "logs", "journal.jsonl")),
                   mock.patch.dict(os.environ, {"NFIXER_GUI": ""})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def run_batch(self, a, repair=False):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = N.BatchRun(a, repair=repair).run()
        return code, buf.getvalue()


class OutFolderTest(TempDirTest):
    def test_inside_source_folder_is_refused(self):
        self.assertIsNotNone(N.out_folder_error(self.src, [self.src]))
        self.assertIsNotNone(N.out_folder_error(os.path.join(self.src, "out"), [self.src]))

    def test_folder_of_a_source_file_is_refused(self):
        self.assertIsNotNone(N.out_folder_error(self.src, [os.path.join(self.src, "a.mp4")]))

    def test_separate_folder_is_fine(self):
        self.assertIsNone(N.out_folder_error(self.out, [self.src]))
        self.assertIsNone(N.out_folder_error(self.src + "-out", [self.src]))  # a sibling with a common prefix

    def test_cli_refuses_output_inside_source(self):
        a = batch_args([self.src], os.path.join(self.src, "out"))
        with self.assertRaises(SystemExit):
            self.run_batch(a)


class FakeSource:
    """Stands in for Source so the batch logic can be tested without real videos."""
    reasons = {}

    def __init__(self, path):
        self.path = path
        self.st = os.stat(path)
        self.size = self.st.st_size
        self.codec, self.width, self.height, self.fps = "h264", 320, 240, 30.0
        self.duration, self.nb_frames, self.start = 10.0, 300, 0.0
        self.audio, self.cut_frame, self.cut_time = [], 0, 0.0
        self.insp = {"damage": "none"}
        self.reason = self.reasons.get(os.path.basename(path))

    kept_frames = 300
    kept_duration = 10.0

    def windows(self, fracs):
        return [(1.0, 2.0)]


class ProblemReportTest(TempDirTest):
    """Regression: the list of problem files used to be overwritten by the verification result of the next file."""

    def setUp(self):
        super().setUp()
        for name, size in (("a.mp4", 3000), ("b.mp4", 2000), ("c.mp4", 1000)):  # processed largest first
            with open(os.path.join(self.src, name), "wb") as fh:
                fh.write(b"\0" * size)

        def encode(src, enc, preset, q, tmp, note):
            with open(tmp, "wb") as fh:
                fh.write(b"\0" * 100)
            return 0, ""

        self.verify_fail = set()
        self.spot = {"mean": 97.0, "p5": 95.0}
        patches = [
            mock.patch.object(N, "Source", FakeSource),
            mock.patch.object(N, "inspect_mp4", lambda p, **k: {"damage": "none"}),
            mock.patch.object(N, "search_quality", lambda *a, **k: {"q": 32, "vmaf": 96.0, "pred_size": 100}),
            mock.patch.object(N, "full_encode", encode),
            mock.patch.object(N, "verify_output", lambda src, *a, **k:
                              ["frame count mismatch"] if os.path.basename(src.path) in self.verify_fail else []),
            mock.patch.object(N, "measure_vmaf", lambda *a, **k: self.spot),
            mock.patch.object(N, "video_start", lambda p: 0.0),
            mock.patch.object(FakeSource, "reasons", {}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_problems_survive_later_successes(self):
        FakeSource.reasons["a.mp4"] = "pixel format yuv444p"
        code, log = self.run_batch(batch_args([self.src], self.out, encoder="nvenc"))
        self.assertIn("PROBLEM FILES: 1", log)
        self.assertIn("yuv444p", log)
        self.assertEqual(code, 0)

    def test_failed_verification_on_last_file_does_not_crash(self):
        self.verify_fail.add("c.mp4")
        code, log = self.run_batch(batch_args([self.src], self.out, encoder="nvenc"))
        self.assertIn("PROBLEM FILES: 1", log)
        self.assertIn("frame count mismatch", log)
        self.assertEqual(code, 2)

    def test_quality_retry_is_offered_to_the_window(self):
        self.spot = {"mean": 80.0, "p5": 70.0}  # every finished file fails the spot check
        with mock.patch.dict(os.environ, {"NFIXER_GUI": "1"}):
            code, log = self.run_batch(batch_args([self.src], self.out, encoder="nvenc"))
        msg = json.loads(next(ln[3:] for ln in log.splitlines() if ln.startswith("@@!")))
        self.assertEqual(sorted(os.path.basename(p) for p in msg["retry"]), ["a.mp4", "b.mp4", "c.mp4"])
        self.assertEqual(msg["problems"], 3)


class IndexTest(TempDirTest):
    def test_missing_index(self):
        p = os.path.join(self.src, "cut.mp4")
        with open(p, "wb") as fh:
            fh.write(b"\0\0\0\x08mdat" + b"\0" * 100)
        self.assertEqual(N.inspect_mp4(p)["damage"], "no-moov")


@needs_ffmpeg
class InspectTest(TempDirTest):
    def test_intact_clip(self):
        p = os.path.join(self.src, "ok.mp4")
        make_clip(p)
        r = N.inspect_mp4(p)
        self.assertEqual(r["damage"], "none")
        self.assertEqual(r["frames"], 300)

    def test_overwritten_head_is_found(self):
        p = os.path.join(self.src, "bad.mp4")
        make_clip(p)
        damage_head(p)
        r = N.inspect_mp4(p)
        self.assertEqual(r["damage"], "head")
        self.assertEqual(r["cut_frame"] % 30, 0)  # the cut lands on a keyframe
        self.assertGreater(r["cut_time"], 1.0)
        self.assertLess(r["cut_time"], 6.0)


@needs_ffmpeg
class EndToEndTest(TempDirTest):
    """Real ffmpeg runs; only VMAF is mocked."""

    def test_repair_cuts_the_damaged_head(self):
        p = os.path.join(self.src, "bad.mp4")
        make_clip(p)
        damage_head(p)
        cut = N.inspect_mp4(p)["cut_frame"]
        code, log = self.run_batch(batch_args([self.src], self.out), repair=True)
        self.assertEqual(code, 0, log)
        res = os.path.join(self.out, "bad.mp4")
        r = N.inspect_mp4(res)
        self.assertEqual(r["damage"], "none")
        self.assertEqual(r["frames"], 300 - cut)

    def test_compress_makes_a_verified_hevc_file(self):
        p = os.path.join(self.src, "big.mp4")
        make_clip(p, crf=4)  # a high bitrate leaves room for compression
        with mock.patch.object(N, "measure_vmaf", lambda *a, **k: {"mean": 97.0, "p5": 95.0, "min": 94.0, "frames": 60}):
            code, log = self.run_batch(batch_args([self.src], self.out))
        self.assertEqual(code, 0, log)
        res = os.path.join(self.out, "big.mp4")
        streams = N.probe(res)["streams"]
        self.assertEqual([s["codec_name"] for s in streams], ["hevc", "aac"])
        self.assertLess(os.path.getsize(res), os.path.getsize(p) * 0.85)
        self.assertEqual(os.path.getmtime(res), os.path.getmtime(p))  # file dates are kept

    def test_compress_trims_damaged_head_first(self):
        p = os.path.join(self.src, "bad.mp4")
        make_clip(p, crf=4)
        damage_head(p)
        cut = N.inspect_mp4(p)["cut_frame"]
        with mock.patch.object(N, "measure_vmaf", lambda *a, **k: {"mean": 97.0, "p5": 95.0, "min": 94.0, "frames": 60}):
            code, log = self.run_batch(batch_args([self.src], self.out))
        self.assertEqual(code, 0, log)
        v = next(s for s in N.probe(os.path.join(self.out, "bad.mp4"))["streams"] if s["codec_type"] == "video")
        self.assertEqual(int(v["nb_frames"]), 300 - cut)
        self.assertEqual(os.listdir(self.out), ["bad.mp4"])  # no temporary files left behind


if __name__ == "__main__":
    unittest.main()
