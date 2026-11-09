"""
Runs the real ffmpeg pipelines the app builds against files in the library,
writing to local files instead of RTMP. The cases use movies from the author's
library; edit CASES for yours. Usage (inside the LXC, as root; the settings file
is root-only, so load it first and pass it on to the service user):
  set -a; . /etc/movie-streamer.env; set +a
  runuser -u streamer -p -- /opt/movie-streamer/venv/bin/python pipeline_test.py /tmp/pt [case numbers]
The other Python tests in this folder are run the same way.
"""
import os, sys, subprocess, tempfile, time, pathlib, re
sys.path.insert(0, "/opt/movie-streamer")
import app as A

M = "Movies/"
CASES = [
    ("SDR 1080p, no subs",       "A Working Man (2025)/A Working Man (2025) 1080p EAC3.mkv", {}),
    ("SDR, SRT sub + seek 10m",  "A Working Man (2025)/A Working Man (2025) 1080p EAC3.mkv",
        {"subtitle": {"type": "internal", "index": 0, "kind": "text", "codec": "subrip"}, "start": 600}),
    ("SDR, ASS sub",             "They Will Kill You (2026)/They Will Kill You (2026) Bluray-1080p EAC3 Atmos.mkv",
        {"subtitle": {"type": "internal", "index": 0, "kind": "text", "codec": "ass"}, "start": 300}),
    ("SDR, PGS sub",             "You Only Live Twice (1967)/You Only Live Twice (1967) Bluray-1080p EAC3.mkv",
        {"subtitle": {"type": "internal", "index": 0, "kind": "image"}, "start": 900}),
    ("SDR, DVD sub",             "Seven Pounds (2008)/Seven Pounds (2008) Bluray-1080p AC3.mkv",
        {"subtitle": {"type": "internal", "index": 0, "kind": "image"}, "start": 900}),
    ("4K 10bit, external SRT",   "The Dark Knight (2008)/The Dark Knight (2008) 2160p AAC.mkv",
        {"subtitle": {"type": "external", "file": "The Dark Knight (2008) 2160p AAC.eng.srt"}, "start": 900}),
    ("HDR10 AV1 4K, NO tonemap", "Back to the Future (1985)/Back to the Future (1985) WEBDL-2160p TrueHD Atmos.mkv",
        {"start": 1500, "audio": 1}),
    ("HDR10 AV1 4K, tonemap",    "Back to the Future (1985)/Back to the Future (1985) WEBDL-2160p TrueHD Atmos.mkv",
        {"start": 1500, "tonemap": True}),
    ("HDR10 tonemap + PGS sub",  "Back to the Future (1985)/Back to the Future (1985) WEBDL-2160p TrueHD Atmos.mkv",
        {"start": 1500, "tonemap": True, "subtitle": {"type": "internal", "index": 2, "kind": "image"}}),
    ("HDR10 tonemap, CPU decode", "Back to the Future (1985)/Back to the Future (1985) WEBDL-2160p TrueHD Atmos.mkv",
        {"start": 1500, "tonemap": True, "hw_decode": False}),
    ("HDR10 tonemap + SRT sub",  "Back to the Future (1985)/Back to the Future (1985) WEBDL-2160p TrueHD Atmos.mkv",
        {"start": 1500, "tonemap": True, "subtitle": {"type": "internal", "index": 0, "kind": "text", "codec": "subrip"}}),
    ("HDR10 tonemap, original 4K", "Back to the Future (1985)/Back to the Future (1985) WEBDL-2160p TrueHD Atmos.mkv",
        {"start": 1500, "tonemap": True, "max_height": 0}),
    ("PGS sub, CPU decode",      "You Only Live Twice (1967)/You Only Live Twice (1967) Bluray-1080p EAC3.mkv",
        {"subtitle": {"type": "internal", "index": 0, "kind": "image"}, "start": 900, "hw_decode": False}),
]

out_dir = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/pipetest")
out_dir.mkdir(exist_ok=True)
only = sys.argv[2:] if len(sys.argv) > 2 else None

for i, (name, rel, opts) in enumerate(CASES):
    if only and str(i) not in only:
        continue
    src = A.safe_path(M + rel)
    opts = {"hw_decode": True, **opts}
    wd = pathlib.Path(tempfile.mkdtemp())
    text_sub, prep = None, ""
    sub = opts.get("subtitle")
    if A.needs_extract(sub):
        t = time.time()
        text_sub = A.cached_text_sub(src, sub["index"], sub.get("codec", ""))
        prep = f"(subs ready in {time.time() - t:.1f}s) "
    canvas = A.canvas_size(opts, src)
    cmd = A.Streamer.build_command(src, opts, wd, text_sub, canvas)
    cmd.remove("-re")
    out = out_dir / f"{i}.ts"
    cmd[-1] = str(out)
    cmd[1:1] = ["-y"]
    cmd[-1:-1] = ["-t", "15"]
    t = time.time()
    p = subprocess.run(cmd, capture_output=True, text=True)
    el = time.time() - t
    errs = [l for l in p.stderr.splitlines() if l.strip()][-4:]
    speed = 15 / el
    print(f"[{i}] {name:28s} exit={p.returncode} {el:5.1f}s  ~{speed:4.1f}x realtime {prep}")
    if p.returncode:
        print("     CMD:", " ".join(A._quote(c) for c in cmd))
        for l in errs:
            print("     ", l)
    else:
        # grab a frame for visual check
        subprocess.run([A.FFMPEG, "-y", "-v", "error", "-ss", "8", "-i", str(out), "-frames:v", "1",
                        "-vf", "scale=640:-2", str(out_dir / f"{i}.jpg")])
