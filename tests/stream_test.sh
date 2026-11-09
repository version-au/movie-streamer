#!/usr/bin/env bash
# Streams one movie through the running service into a local RTMP receiver,
# seeks once, and reports startup time, position, relay warnings and timestamp
# continuity. Saved stream settings are restored afterwards.
# Usage (inside the LXC as root): bash stream_test.sh "Movies/X (2020)/X.mkv" [seek_seconds]
set -u
REL="$1"; SEEK="${2:-4}"
F=/usr/lib/jellyfin-ffmpeg/ffmpeg; P=/usr/lib/jellyfin-ffmpeg/ffprobe
J="Content-Type: application/json"; API=http://127.0.0.1:8080
SAVED=$(curl -s $API/api/settings)
restore() {
  python3 - "$SAVED" <<'EOF'
import json, sys, urllib.request as u
s = json.loads(sys.argv[1])
u.urlopen(u.Request("http://127.0.0.1:8080/api/settings",
                    json.dumps({"server": s["server"], "key": s["key"]}).encode(),
                    {"Content-Type": "application/json"}))
EOF
}
trap restore EXIT
curl -s -X POST $API/api/settings -H "$J" -d '{"server":"rtmp://127.0.0.1:1935/live","key":"st"}' >/dev/null
$F -nostdin -v error -listen 1 -i rtmp://127.0.0.1:1935/live/st -c copy -f flv -y /tmp/st.flv 2>/dev/null </dev/null &
RX=$!
sleep 1
show() {
  curl -s $API/api/status | python3 -c "
import json,sys; s=json.load(sys.stdin)
print('  %-6s %-9s connected=%-5s pos=%7.1f speed=%s' % ('$1', s['state'], s['connected'], s['position'] or 0, s['speed']))"
}
DUR=$(curl -s -G $API/api/probe --data-urlencode "path=$REL" | python3 -c "import json,sys;print(json.load(sys.stdin)['duration'])")
python3 -c "import json;print(json.dumps({'path':'''$REL''','duration':$DUR}))" > /tmp/st.json
curl -s -X POST $API/api/play -H "$J" --data @/tmp/st.json >/dev/null
for i in 1 2 3 4 5; do sleep 2; show "t+$((2*i))s"; done
echo "  seek to ${SEEK}s"
curl -s -X POST $API/api/seek -H "$J" -d "{\"position\":$SEEK}" >/dev/null
for i in 1 2 3 4; do sleep 2; show "t+$((2*i))s"; done
curl -s $API/api/status | python3 -c "
import json,sys; s=json.load(sys.stdin)
print('  log:'); [print('    ', l[:150]) for l in s['log'] if not l.startswith('\$')]"
curl -s -X POST $API/api/stop >/dev/null
sleep 3; kill $RX 2>/dev/null; wait $RX 2>/dev/null
echo "  received: $($P -v error -show_entries stream=codec_name,width,height,sample_rate,channels -of compact=nk=1 /tmp/st.flv | tr '\n' ' ')"
$P -v error -select_streams v -show_entries packet=dts_time -of csv=p=0 /tmp/st.flv | python3 -c "
import sys; ts=[float(x) for x in sys.stdin.read().split() if x != 'N/A']
print('  video packets:', len(ts), ' backwards:', sum(b < a for a, b in zip(ts, ts[1:])),
      ' gaps>0.5s:', [round(b - a, 2) for a, b in zip(ts, ts[1:]) if b - a > 0.5])"
rm -f /tmp/st.flv /tmp/st.json
