# NFixer

**English** | [Русский](README.ru.md)

<img src="nfixer.png" width="96" align="right" alt="">

Repairs and compresses game recordings made with NVIDIA Instant Replay (ShadowPlay):

- finds clips whose head was destroyed by an Instant Replay buffer overflow and cuts the damaged part off;
- compresses recordings from H.264 to HEVC without visible quality loss, verifying the result with the VMAF metric.

Windows only. Requires Python 3.9+ and [FFmpeg](https://ffmpeg.org/) built with libvmaf (`ffmpeg.exe` and
`ffprobe.exe`). FFmpeg is looked up in an `ffmpeg` folder next to the program, then in `PATH`; another location
can be given with `--ffmpeg FOLDER` or the `NFIXER_FFMPEG` environment variable. Compression uses an NVIDIA GPU
with an HEVC encoder by default; without one, use `--encoder x265`.

## The window

Double-click `NFixer.pyw`. Add `.mp4` videos and folders with the buttons or by dragging them from Explorer into
the window (several at once is fine; they can also be dropped onto `NFixer.pyw` or its shortcut). Then pick an
action (check / estimate saving / compress / cut damaged head), where the result goes, and the quality.
There are Start and Stop buttons, a progress bar and a log. The language (English / Русский) is switched in the
top right corner; English is the default.

Stop interrupts the job at once: the unfinished file is removed, the original stays, and the next Start continues
where it left off. Do not run two copies on the same folders at the same time.

## Command line

```
nfixer check    PATHS... [--days N]         find damaged recordings (read-only)
nfixer estimate PATHS... [--files 12]       predict the saving on a random sample (read-only)
nfixer compress PATHS... --out FOLDER       compress, leave the originals alone
nfixer compress PATHS... --replace          compress and replace the originals (each one after verification)
nfixer repair   PATHS... --out FOLDER       cut the damaged head without re-encoding
```

`PATHS` is any number of folders and `.mp4` files; a long list can be passed as a file: `--list list.txt`.
Useful options: `--days N` (recent files only), `--match TEXT` (filter by path), `--limit N`, `--max-hours N`,
`--vmaf N`, `--dry-run`. A job can be interrupted with Ctrl+C and started again - it continues where it stopped.
Messages are in English by default; `--lang ru` (before the command) or `NFIXER_LANG=ru` switches to Russian.

Example: check the recordings of the last day.

```
nfixer check "D:\Videos" --days 1
```

## How quality is preserved

An already compressed video cannot be made smaller with no loss at all (the file would grow), so the goal is a
**visually indistinguishable** result, and that is measured rather than judged by eye:

1. Sample segments of every clip are encoded at several compression levels and compared with the original using
   VMAF. The strongest compression is chosen at which the mean VMAF is >= 95 and no individual frames drop out
   (5th percentile >= 91). The target is changed with `--vmaf`.
2. A clip that cannot be shrunk by at least 15% without quality loss is left as it is.
3. After encoding the tool checks: same frame count and duration, same audio tracks (audio is copied without
   re-encoding, track names are kept), no errors while reading the source, and a VMAF spot check on the finished
   file at a different place in the clip. Only then (with `--replace`) is the original replaced.
4. Resolution, frame rate, the timestamp of every frame, colour tags and file dates are preserved.

The result is HEVC (H.265, 8-bit) in MP4. `--encoder x265` compresses 10-20% further but is about 5 times slower.
`--encoder av1` is smaller still, but older video editors cannot open AV1.

What to expect (measured on short excerpts, NVENC encoder, VMAF around 96-98):

| Recordings                | Saving       |
|---------------------------|--------------|
| 1080p, 40-50 Mbit/s       | ~8x smaller  |
| 1440p, 30-40 Mbit/s       | ~4x smaller  |
| 1440p, ~20 Mbit/s         | 15-25%       |

1440p recordings at 20 Mbit/s are already heavily compressed when recorded, so there is little headroom.
With `--vmaf 93` they shrink by 35-45% at the cost of barely noticeable softening of fine detail.

## Why Instant Replay recordings break

Instant Replay keeps the last N seconds in a ring-buffer file. NVIDIA App sizes it as
`duration x bitrate x 1.2` (for example 270 s x 20 Mbit/s -> 810 MB), yet lets the encoder peak at twice the
configured bitrate (up to 40 Mbit/s). In fast-paced games the real bitrate stays near the peak for long stretches,
the buffer fills before the configured time has passed, and new frames overwrite the oldest ones. In the saved
clip the index still points at the old frames while foreign data sits in their place - hence the garbage at the
start of the clip. The final part of the clip that fits into the buffer is always intact.

Both numbers are visible in `%ProgramData%\NVIDIA Corporation\ShadowPlay\CaptureCore.log`
(the `GetVideoTempFileLimit` and `InitEncoderParams` lines). The behaviour was observed in NVIDIA App 11.0.8
and 11.0.9.

Workarounds:

- `replay duration x 0.6` is guaranteed to stay intact. To always get an intact 4.5 minutes, set the duration to
  7.5 minutes; the damaged head is then removed with the Cut action (`repair`).
- Or save the replay more often.
- After a gaming session, check the fresh clips with the Check action (`check --days 1`).

## How NFixer finds the damage boundary

An MP4 has an index: where every frame is stored and how large it is. In a damaged clip the index is correct, but
foreign data sits at the addresses of the first frames. A real H.264 frame starts with a header of a known form,
so NFixer reads the start of every keyframe and finds the last one that does not look like a header. The cut is
made at the next keyframe whose whole group of frames is intact. After the cut the result is checked frame by
frame and by a full decode.

## Misc

- The journal of processed files and the window settings live in the `logs` folder next to the program.
- The icon is drawn by `tools/make_icon.py` (requires Pillow).
- NFixer is not affiliated with NVIDIA. NVIDIA, ShadowPlay and Instant Replay are trademarks of their owner.
