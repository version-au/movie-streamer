"""
A/V start alignment check: encodes 6s segments with the app's command and
compares where the video and audio streams start in the output.
Run inside the LXC as the service user (see pipeline_test.py for the env).
"""
import pathlib, subprocess, sys, tempfile
sys.path.insert(0, "/opt/movie-streamer")
import app as A

ONS = "Movies/Onslaught (2026)/Onslaught (2026) WEBDL-1080p EAC3 Atmos.mkv"
AWM = "Movies/A Working Man (2025)/A Working Man (2025) 1080p EAC3.mkv"
CASES = [(ONS, 0), (ONS, 5), (ONS, 600), (AWM, 0), (AWM, 600),
         (ONS, 600, {"subtitle": {"type": "internal", "index": 0, "kind": "text", "codec": "subrip"}})]

for case in CASES:
    rel, start, extra = (case + ({},))[:3]
    src = A.safe_path(rel)
    opts = {"hw_decode": True, "start": start, **extra}
    opts["_audio_lead"] = A.audio_lead(src, 0, start)
    ts = A.cached_text_sub(src, 0, "subrip") if A.needs_extract(opts.get("subtitle")) else None
    cmd = A.Streamer.build_command(src, opts, pathlib.Path(tempfile.mkdtemp()), ts, (1920, 1080), 5.0)
    cmd.remove("-re")
    cmd[1:1] = ["-y"]
    cmd[-1:-1] = ["-t", "6"]
    cmd[-1] = "/tmp/sync.ts"
    p = subprocess.run(cmd, capture_output=True, text=True)
    out = subprocess.run([A.FFPROBE, "-v", "error", "-show_entries", "stream=codec_type,start_time,duration",
                          "-of", "csv=p=0", "/tmp/sync.ts"], capture_output=True, text=True).stdout.split()
    st = {l.split(",")[0]: float(l.split(",")[1]) for l in out if l.count(",") >= 1 and l.split(",")[1] not in ("N/A", "")}
    v, a = st.get("video"), st.get("audio")
    diff = f"{(a - v) * 1000:+.0f}ms" if v is not None and a is not None else "n/a"
    hw = sum(1 for l in p.stderr.splitlines() if "HW accel" in l)
    tag = " +sub" if extra else ""
    print(f"{rel.split('/')[1][:22]:22s} start={start:<5}{tag:5s} lead={opts['_audio_lead']:5.2f} exit={p.returncode} "
          f"video_start={v} audio_start={a} a-v={diff} hw_errors={hw}")
    if p.returncode:
        print("   ", p.stderr.strip().splitlines()[-3:])
