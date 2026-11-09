"""
Runs the app's exact segment command for one file in several variations and
reports hardware-decode errors. Usage (inside the LXC; see pipeline_test.py for
loading the settings and running it as the service user):
  python file_test.py "Movies/Some Movie (2020)/Some Movie.mkv"
"""
import pathlib, subprocess, sys, tempfile, time
sys.path.insert(0, "/opt/movie-streamer")
import app as A

rel = sys.argv[1]
src = A.safe_path(rel)
info = subprocess.run([A.FFPROBE, "-v", "error", "-show_entries", "stream=index,codec_type,codec_name",
                       "-of", "csv=p=0", str(src)], capture_output=True, text=True).stdout
subs = [l for l in info.splitlines() if ",subtitle" in l]
first_sub = subs[0].split(",")[1] if subs else None

cases = [("start 0", {}), ("start 10:00", {"start": 600}), ("start 50:00", {"start": 3000})]
if first_sub:
    kind = "image" if first_sub in A.IMAGE_SUB_CODECS else "text"
    cases.append((f"start 10:00 + sub ({first_sub})",
                  {"start": 600, "subtitle": {"type": "internal", "index": 0, "kind": kind, "codec": first_sub}}))
cases.append(("start 10:00, CPU decode", {"start": 600, "hw_decode": False}))

for name, o in cases:
    opts = {"hw_decode": True, **o}
    wd = pathlib.Path(tempfile.mkdtemp())
    sub = opts.get("subtitle")
    ts = A.cached_text_sub(src, 0, first_sub) if A.needs_extract(sub) else None
    cmd = A.Streamer.build_command(src, opts, wd, ts, A.canvas_size(opts, src), 5.0)
    cmd.remove("-re")
    cmd[1:1] = ["-y"]
    cmd[-1:-1] = ["-t", "20"]
    cmd[-1] = "/dev/null"
    t = time.time()
    p = subprocess.run(cmd, capture_output=True, text=True)
    hw = sum(1 for l in p.stderr.splitlines() if "HW accel" in l or "Failed to upload" in l)
    other = [l for l in p.stderr.splitlines() if l.strip() and "HW accel" not in l
             and "Failed to upload" not in l and "Error submitting" not in l][-3:]
    print(f"{name:32s} exit={p.returncode} hw_errors={hw:4d} {time.time() - t:5.1f}s")
    for l in other:
        print("     ", l[:200])
