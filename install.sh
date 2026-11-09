#!/usr/bin/env bash
# Installs Movie Streamer inside a Debian 12/13 container. Run as root from the repo folder.
# (proxmox-install.sh runs this for you; you only need it for a manual install.)
#
# Options (environment variables):
#   GPU=intel|amd|nvidia|none   which GPU the container has (default: intel)
#   NVIDIA_VERSION=580.95.05    NVIDIA only: must be exactly the host's driver version
#
# Installs jellyfin-ffmpeg (bundles current Intel/AMD VA-API drivers, CUDA filters,
# tone-mapping, libass), the app in /opt/movie-streamer and a systemd service.
set -euo pipefail
cd "$(dirname "$0")"
export DEBIAN_FRONTEND=noninteractive LC_ALL=C.UTF-8
GPU="${GPU:-intel}"
case "$GPU" in
  intel) ENC=vaapi ;; amd) ENC=vaapi-amd ;; nvidia) ENC=nvenc ;; none) ENC=x264 ;;
  *) echo "GPU must be intel, amd, nvidia or none (got '$GPU')" >&2; exit 1 ;;
esac
FF=/usr/lib/jellyfin-ffmpeg/ffmpeg

. /etc/os-release
echo "== Packages"
apt-get update -qq
apt-get install -y -qq curl gpg ca-certificates python3 python3-venv \
  fonts-dejavu-core fonts-noto-core >/dev/null

# Jellyfin repo (for jellyfin-ffmpeg)
install -d /etc/apt/keyrings
curl -fsSL https://repo.jellyfin.org/jellyfin_team.gpg.key | gpg --dearmor --yes -o /etc/apt/keyrings/jellyfin.gpg
cat > /etc/apt/sources.list.d/jellyfin.sources <<EOF
Types: deb
URIs: https://repo.jellyfin.org/${ID}
Suites: ${VERSION_CODENAME}
Components: main
Architectures: amd64
Signed-By: /etc/apt/keyrings/jellyfin.gpg
EOF
apt-get update -qq
PKG=$(apt-cache search --names-only '^jellyfin-ffmpeg[0-9]+$' | awk '{print $1}' | sort -V | tail -1)
[ -n "$PKG" ] || { echo "jellyfin-ffmpeg not found for ${ID} ${VERSION_CODENAME}" >&2; exit 1; }
echo "== Installing $PKG"
apt-get install -y -qq "$PKG" >/dev/null

if [ "$GPU" = nvidia ]; then
  echo "== NVIDIA user-space driver"
  [ -n "${NVIDIA_VERSION:-}" ] || { echo "NVIDIA_VERSION is required for GPU=nvidia" >&2; exit 1; }
  have=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1 || true)
  if [ "$have" = "$NVIDIA_VERSION" ]; then
    echo "already installed ($have)"
  else
    apt-get install -y -qq kmod pkg-config xz-utils >/dev/null
    url="https://us.download.nvidia.com/XFree86/Linux-x86_64/${NVIDIA_VERSION}/NVIDIA-Linux-x86_64-${NVIDIA_VERSION}.run"
    echo "downloading $url"
    curl -fL --progress-bar -o /tmp/nvidia.run "$url"
    # No kernel module: the host's driver provides it. (Flag was renamed in newer drivers.)
    opts="--silent --no-questions --ui=none --no-x-check --no-nouveau-check --no-systemd"
    sh /tmp/nvidia.run --no-kernel-modules $opts 2>/dev/null || sh /tmp/nvidia.run --no-kernel-module $opts
    rm -f /tmp/nvidia.run
  fi
  nvidia-smi -L
fi

echo "== App"
id streamer &>/dev/null || useradd --system --home /opt/movie-streamer --shell /usr/sbin/nologin streamer
getent group render &>/dev/null && usermod -aG render streamer
usermod -aG video streamer

mkdir -p /opt/movie-streamer
cp -r app.py static requirements.txt /opt/movie-streamer/
[ -d tests ] && cp -r tests /opt/movie-streamer/
python3 -m venv /opt/movie-streamer/venv
/opt/movie-streamer/venv/bin/pip install -q -r /opt/movie-streamer/requirements.txt
chown -R streamer:streamer /opt/movie-streamer

if [ ! -f /etc/movie-streamer.env ]; then
  cp movie-streamer.env /etc/movie-streamer.env
  sed -i "s/^ENCODER=.*/ENCODER=$ENC/" /etc/movie-streamer.env
fi
chmod 600 /etc/movie-streamer.env
cp movie-streamer.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable -q movie-streamer
systemctl restart movie-streamer

# ---- GPU encode test (as the service user, with the service's settings)
set -a; . /etc/movie-streamer.env; set +a
ENCODER="${ENCODER:-$ENC}"
echo "== GPU check ($ENCODER)"
src=(-f lavfi -i testsrc2=s=1280x720:r=30:d=2)
case "$ENCODER" in
  vaapi|vaapi-amd)
    runuser -u streamer -- /usr/lib/jellyfin-ffmpeg/vainfo --display drm --device "$VAAPI_DEVICE" 2>/dev/null \
      | grep -E 'Driver version' || true
    test_cmd=("$FF" -hide_banner -v error -init_hw_device "vaapi=va:$VAAPI_DEVICE" -filter_hw_device va
              "${src[@]}" -vf format=nv12,hwupload -c:v h264_vaapi -f null -) ;;
  nvenc)
    test_cmd=("$FF" -hide_banner -v error "${src[@]}" -c:v h264_nvenc -f null -) ;;
  *) test_cmd=() ;;
esac
if [ ${#test_cmd[@]} -eq 0 ]; then
  echo "ENCODE_TEST=skipped (CPU only)"
elif out=$(runuser -u streamer -- "${test_cmd[@]}" 2>&1); then
  echo "ENCODE_TEST=ok"
else
  echo "$out" | tail -5
  echo "ENCODE_TEST=failed"
fi
systemctl is-active --quiet movie-streamer && echo "SERVICE=active" || echo "SERVICE=failed"
