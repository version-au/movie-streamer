"""
End-to-end test of the queue / seek / reconnect behaviour through the web API.

Run inside the LXC while the service is up. It runs its own local RTMP receiver
(ffmpeg -listen) in place of the VPS, so nothing is sent anywhere:
  python3 e2e_queue_test.py
Afterwards it restores the saved stream settings and the queue.
"""
import json, subprocess, sys, time, urllib.request, urllib.parse

BASE = "http://127.0.0.1:8080"
FF = "/usr/lib/jellyfin-ffmpeg/ffmpeg"
FP = "/usr/lib/jellyfin-ffmpeg/ffprobe"
A = "Movies/A Working Man (2025)/A Working Man (2025) 1080p EAC3.mkv"
B = "Movies/Seven Pounds (2008)/Seven Pounds (2008) Bluray-1080p AC3.mkv"
C = "Movies/Back to the Future (1985)/Back to the Future (1985) WEBDL-2160p TrueHD Atmos.mkv"


def call(path, body=None, method=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method or ("POST" if data else "GET"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def dur(p):
    return call("/api/probe?path=" + urllib.parse.quote(p))["duration"]


def st(tag=""):
    s = call("/api/status")
    cur = s["current"]["name"][:28] if s["current"] else "-"
    pos = f"{s['position']:.1f}" if s["position"] is not None else "-"
    print(f"  {tag:22s} state={s['state']:9s} conn={s['connected']!s:5s} cur={cur:28s} pos={pos:>8s} q={len(s['queue'])}")
    return s


def listener(name):
    return subprocess.Popen([FF, "-nostdin", "-v", "error", "-listen", "1", "-i", "rtmp://127.0.0.1:1935/live/e2e",
                             "-c", "copy", "-f", "flv", "-y", f"/tmp/{name}.flv"], stdin=subprocess.DEVNULL)


saved_settings = call("/api/settings")
saved_queue = call("/api/status")["queue"]
call("/api/queue/clear", {})
call("/api/settings", {"server": "rtmp://127.0.0.1:1935/live", "key": "e2e"})
rx = listener("rx1")
time.sleep(1)
def dump_log():
    print("  ---- app log ----")
    for l in call("/api/status")["log"]:
        print("   ", l[:220])


try:
    da = dur(A)
    print("1) play A 8s before its end, queue B (PGS subs) and C (4K HDR tonemap)")
    call("/api/play", {"path": A, "start": da - 8, "duration": da, "label": "A"})
    call("/api/queue", {"path": B, "start": 900, "duration": dur(B), "label": "B",
                        "subtitle": {"type": "internal", "index": 0, "kind": "image"}})
    call("/api/queue", {"path": C, "start": 1500, "duration": dur(C), "label": "C", "tonemap": True})
    prev = None
    for i in range(10):
        time.sleep(2); s = st(f"t+{2*(i+1)}s")
        if s["current"]["label"] == "B" and prev is not None:
            assert s["position"] >= prev, f"position went backwards {prev} -> {s['position']}"
        prev = s["position"] if s["current"]["label"] == "B" else None
    assert s["current"] and s["current"]["label"] == "B", "did not auto-advance to B"

    print("2) seek B to 1:10:00")
    call("/api/seek", {"position": 4200})
    time.sleep(5); s = st("after seek")
    assert 4200 <= s["position"] < 4215, s["position"]

    print("3) skip to C")
    call("/api/skip", {})
    time.sleep(6); s = st("after skip")
    assert s["current"]["label"] == "C"

    print("4) kill the receiver (simulated connection drop) and bring it back")
    pos_before = s["position"]
    rx.kill(); rx.wait()
    rx2 = listener("rx2")
    for i in range(6):
        time.sleep(2); s = st(f"t+{2*(i+1)}s")
    assert s["state"] == "playing" and s["connected"], "did not reconnect"
    assert s["position"] >= pos_before - 3, (pos_before, s["position"])
    print("  log:", [l for l in s["log"] if "stopped (exit" in l or "Connecting" in l])

    print("5) stop")
    call("/api/stop", {})
    time.sleep(2); st("after stop")
    rx2.wait(timeout=10)
except AssertionError:
    dump_log()
    raise
finally:
    call("/api/stop", {})
    call("/api/settings", {"server": saved_settings["server"], "key": saved_settings["key"]})
    call("/api/queue/clear", {})
    for it in saved_queue:
        call("/api/queue", {"path": it["path"], **it["opts"], "duration": it["duration"],
                            "label": it["label"], "start": it.get("start", 0)})

print("6) what the receivers got")
for name in ("rx1", "rx2"):
    out = subprocess.run([FP, "-v", "error", "-show_entries", "format=duration:stream=codec_name,width,height",
                          "-of", "compact", f"/tmp/{name}.flv"], capture_output=True, text=True).stdout
    print(f"  {name}: " + " | ".join(out.split()))
    # timestamps must never go backwards across movie changes / seeks
    pk = subprocess.run([FP, "-v", "error", "-select_streams", "v", "-show_entries", "packet=dts_time",
                         "-of", "csv=p=0", f"/tmp/{name}.flv"], capture_output=True, text=True).stdout.split()
    ts = [float(x) for x in pk if x not in ("N/A", "")]
    back = sum(1 for a, b in zip(ts, ts[1:]) if b < a)
    gaps = [round(b - a, 2) for a, b in zip(ts, ts[1:]) if b - a > 0.5]
    print(f"  {name}: {len(ts)} video packets, backwards={back}, gaps>0.5s={gaps}")
