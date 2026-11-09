"""
Checks late-starting audio is placed at the right moment: compares when sound
first appears (relative to the picture) in the app's output vs. a reference
encode where ffmpeg keeps the audio at its true timestamps.
Run inside the LXC as the service user with the service env loaded (see pipeline_test.py).
"""
import pathlib, re, subprocess, sys, tempfile
sys.path.insert(0, "/opt/movie-streamer")
import app as A

F, P = A.FFMPEG, A.FFPROBE
REL = sys.argv[1] if len(sys.argv) > 1 else "Movies/Onslaught (2026)/Onslaught (2026) WEBDL-1080p EAC3 Atmos.mkv"
src = A.safe_path(REL)


def start_time(path, sel):
    out = subprocess.run([P, "-v", "error", "-select_streams", sel, "-show_entries", "stream=start_time",
                          "-of", "csv=p=0", path], capture_output=True, text=True).stdout.split()
    return float(out[0])


def sound_start(path):
    """Seconds after the first video frame at which audio is first audible."""
    r = subprocess.run([F, "-hide_banner", "-copyts", "-i", path, "-map", "0:a",
                        "-af", "silencedetect=n=-50dB:d=0.3", "-f", "null", "-"],
                       capture_output=True, text=True).stderr
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.-]+)", r)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", r)]
    a0, v0 = start_time(path, "a"), start_time(path, "v")
    first = ends[0] if ends and starts and starts[0] <= a0 + 0.05 else a0
    return first - v0


for start in (0, 5, 9, 600):
    lead = A.audio_lead(src, 0, start)
    o = {"hw_decode": True, "start": start, "_audio_lead": lead}
    cmd = A.Streamer.build_command(src, o, pathlib.Path(tempfile.mkdtemp()), None, (1920, 1080), 0.0)
    cmd.remove("-re")
    cmd[1:1] = ["-y"]
    cmd[-1:-1] = ["-t", "20"]
    cmd[-1] = "/tmp/ours.ts"
    subprocess.run(cmd, capture_output=True)
    ss = ["-ss", str(start)] if start else []
    subprocess.run([F, "-y", "-v", "error"] + ss + ["-i", str(src), "-t", "20", "-map", "0:v:0", "-map", "0:a:0",
                    "-c:v", "libx264", "-preset", "ultrafast", "-s", "640x266", "-c:a", "aac", "-ac", "2",
                    "-f", "mpegts", "/tmp/ref.ts"])
    ours, ref = sound_start("/tmp/ours.ts"), sound_start("/tmp/ref.ts")
    verdict = "OK" if abs(ours - ref) < 0.15 else "MISMATCH"
    print(f"start={start:<4} lead={lead:5.2f}  first sound after picture: ours={ours:6.2f}s  "
          f"reference={ref:6.2f}s  {verdict}")

for f in ("/tmp/ours.ts", "/tmp/ref.ts"):
    pathlib.Path(f).unlink(missing_ok=True)
