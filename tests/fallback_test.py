"""
Checks the GPU->CPU decode fallback: plays a movie into a local RTMP receiver
using a private Streamer instance (not the running service), triggers the
fallback as if GPU decode errors had been seen, and checks playback continues
from the same spot with CPU decoding. Run inside the LXC as root:
  STATE_DIR=/tmp/fbstate RTMP_URL=rtmp://127.0.0.1:1936/live/fb \
    FFMPEG=/usr/lib/jellyfin-ffmpeg/ffmpeg FFPROBE=/usr/lib/jellyfin-ffmpeg/ffprobe \
    /opt/movie-streamer/venv/bin/python fallback_test.py
"""
import subprocess, sys, time
sys.path.insert(0, "/opt/movie-streamer")
import app as A

rx = subprocess.Popen([A.FFMPEG, "-nostdin", "-v", "error", "-listen", "1", "-i", "rtmp://127.0.0.1:1936/live/fb",
                       "-c", "copy", "-f", "null", "-"], stdin=subprocess.DEVNULL)
time.sleep(1)
s = A.Streamer()
item = A.Streamer.make_item({"path": "Movies/Onslaught (2026)/Onslaught (2026) WEBDL-1080p EAC3 Atmos.mkv",
                             "duration": 5539})
s.play(item)
time.sleep(8)
st = s.status()
print("before:", st["state"], round(st["position"], 1), "hw_decode =", item["opts"].get("hw_decode", True))
if st["position"] < 2:
    print("NOT STREAMING. log:\n  " + "\n  ".join(l[:250] for l in st["log"]))
    s.stop(); rx.kill(); sys.exit(1)
s._fallback_to_cpu(s.seg_gen, s.current)
time.sleep(6)
st = s.status()
print("after: ", st["state"], round(st["position"], 1), "hw_decode =", item["opts"].get("hw_decode"),
      "connected =", st["connected"])
print("log:", [l for l in st["log"] if "CPU" in l or "exit" in l])
ok = st["state"] == "playing" and item["opts"].get("hw_decode") is False and st["position"] > 4
s.stop()
try:
    rx.wait(timeout=10)
except subprocess.TimeoutExpired:
    rx.kill()
print("PASS" if ok else "FAIL")
