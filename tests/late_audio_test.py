"""
A/V sync test with exact markers. Builds synthetic movies (video + a 0.1s beep at
every whole second of the audio track), one with audio starting together with the
video and one whose audio track starts 5s late. Runs the app's segment command
from several start points and checks every beep lands where it should relative
to the picture (beep k of the track = file time audio_offset + k).

Run inside the LXC as the service user (it makes its own test files, so it
doesn't need the settings file):
  MEDIA_ROOT=/tmp/synth FFMPEG=/usr/lib/jellyfin-ffmpeg/ffmpeg \
    FFPROBE=/usr/lib/jellyfin-ffmpeg/ffprobe /opt/movie-streamer/venv/bin/python late_audio_test.py
"""
import os, pathlib, re, subprocess, sys, tempfile
ROOT = pathlib.Path(os.environ.get("MEDIA_ROOT", "/tmp/synth"))
ROOT.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, "/opt/movie-streamer")
import app as A

F, P = A.FFMPEG, A.FFPROBE


def make(name, audio_offset):
    out = ROOT / name
    if not out.exists():
        subprocess.run([F, "-y", "-v", "error",
                        "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=24:d=40",
                        "-itsoffset", str(audio_offset),
                        "-f", "lavfi", "-i", "sine=f=1000:sample_rate=48000:d=34,"
                                             "volume='if(lt(mod(t\\,1)\\,0.1)\\,1\\,0)':eval=frame",
                        "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "ultrafast",
                        "-g", "48", "-c:a", "ac3", "-ac", "2", str(out)], check=True)
    return out


def onsets(path):
    """Beep start times, relative to the first video frame."""
    r = subprocess.run([F, "-hide_banner", "-copyts", "-i", str(path), "-map", "0:a",
                        "-af", "silencedetect=n=-30dB:d=0.3", "-f", "null", "-"],
                       capture_output=True, text=True).stderr
    v0 = float(subprocess.run([P, "-v", "error", "-select_streams", "v", "-show_entries",
                               "stream=start_time", "-of", "csv=p=0", str(path)],
                              capture_output=True, text=True).stdout.split()[0])
    return [round(float(x) - v0, 3) for x in re.findall(r"silence_end: ([\d.]+)", r)]


fails = 0
for name, off in (("in_sync.mkv", 0), ("late_audio_5s.mkv", 5)):
    src = make(name, off)
    for start in (0, 2, 7, 12):
        lead = A.audio_lead(src, 0, start)
        opts = {"hw_decode": True, "start": start, "_audio_lead": lead}
        cmd = A.Streamer.build_command(src, opts, pathlib.Path(tempfile.mkdtemp()), None, (1280, 720), 0.0)
        cmd.remove("-re")
        cmd[1:1] = ["-y"]
        cmd[-1:-1] = ["-t", "12"]
        cmd[-1] = "/tmp/la.ts"
        p = subprocess.run(cmd, capture_output=True, text=True)
        got = onsets("/tmp/la.ts")[:4]
        # beeps are at file times off+k; relative to the picture starting at `start`
        exp = [round(off + k - start, 3) for k in range(40) if off + k - start > 0.05][:len(got) or 4]
        err = max((abs(g - e) for g, e in zip(got, exp)), default=99)
        ok = p.returncode == 0 and got and err < 0.06
        fails += not ok
        print(f"{name:18s} start={start:<3} lead={lead:5.2f} beeps at {got}  expected {exp}  "
              f"max error {err * 1000:.0f}ms  {'OK' if ok else 'FAIL'}")
pathlib.Path("/tmp/la.ts").unlink(missing_ok=True)
print("ALL OK" if not fails else f"{fails} FAILED")
