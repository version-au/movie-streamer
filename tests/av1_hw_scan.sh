#!/usr/bin/env bash
# Lists AV1 videos in the library and tries 2s of VAAPI (GPU) decoding on each,
# reporting the ones the GPU rejects. Run inside the LXC: bash av1_hw_scan.sh
P=/usr/lib/jellyfin-ffmpeg/ffprobe
F=/usr/lib/jellyfin-ffmpeg/ffmpeg
LIST=/tmp/av1list.txt

probe() {
  c=$($P -v error -select_streams v:0 -show_entries stream=codec_name,profile,pix_fmt,width,height \
        -of csv=p=0 "$1" 2>/dev/null | head -1)
  case "$c" in av1*) printf '%s|%s\n' "$c" "$1" ;; esac
}
export -f probe; export P

find /media/Movies "/media/TV Series" -type f \( -name '*.mkv' -o -name '*.mp4' \) -print0 2>/dev/null |
  xargs -0 -P 8 -I{} bash -c 'probe "$1"' _ {} > "$LIST"
echo "AV1 files: $(wc -l < "$LIST")"
cut -d'|' -f1 "$LIST" | sort | uniq -c

echo "== GPU decode test (2s at 10 min in)"
fail=0
while IFS='|' read -r info path; do
  err=$($F -hide_banner -v error -hwaccel vaapi -hwaccel_device /dev/dri/renderD128 \
         -hwaccel_output_format vaapi -ss 600 -t 2 -i "$path" -map 0:v:0 -f null - 2>&1 |
        grep -cE 'HW accel|Failed to upload|Error submitting')
  if [ "$err" -gt 0 ]; then
    fail=$((fail+1))
    echo "FAIL ($err errors) $info  $path"
  fi
done < "$LIST"
echo "GPU decode failures: $fail of $(wc -l < "$LIST")"
