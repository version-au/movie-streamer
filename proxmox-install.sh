#!/usr/bin/env bash
# =============================================================================
#  Movie Streamer: Proxmox installer
#
#  Creates an LXC container with your media folders and GPU passed through,
#  installs Movie Streamer in it and prints the address to open.
#
#  Run on a Proxmox VE node (8.2 or newer) as root, from the repo folder:
#      bash proxmox-install.sh            # asks questions, then installs
#      bash proxmox-install.sh --dry-run  # asks questions, shows what it would do
# =============================================================================
set -Eeuo pipefail

REPO_ARCHIVE="${REPO_ARCHIVE:-https://github.com/version-au/movie-streamer/archive/refs/heads/main.tar.gz}"
DRY=0
for a in "$@"; do
  case "$a" in
    --dry-run) DRY=1 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "Unknown option: $a" >&2; exit 1 ;;
  esac
done

# ----------------------------------------------------------------------------- UI helpers
if [ -t 1 ]; then
  B=$'\e[1m'; DIM=$'\e[2m'; G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; C=$'\e[36m'; N=$'\e[0m'
else
  B=; DIM=; G=; Y=; R=; C=; N=
fi
# Read answers from the terminal even when the script itself arrives on stdin (curl | bash).
if { exec 3</dev/tty; } 2>/dev/null; then :; else exec 3<&0; fi

step() { printf '\n%s%s== %s%s\n' "$B" "$C" "$*" "$N"; }
info() { printf '   %s\n' "$*"; }
ok()   { printf '   %s✔%s %s\n' "$G" "$N" "$*"; }
warn() { printf '   %s!%s %s\n' "$Y" "$N" "$*"; }
die()  { printf '\n%s✘ %s%s\n' "$R" "$*" "$N" >&2; exit 1; }

# ask VAR "Question" [default]
ask() {
  local __var=$1 __q=$2 __def=${3-} __ans
  if [ -n "$__def" ]; then printf '%s %s[%s]%s: ' "$__q" "$DIM" "$__def" "$N"
  else printf '%s: ' "$__q"; fi
  # read fails at end of input; stop instead of re-asking forever
  if ! IFS= read -r __ans <&3 && [ -z "$__ans" ]; then echo; die "Input ended before all questions were answered."; fi
  __ans="${__ans#"${__ans%%[![:space:]]*}"}"; __ans="${__ans%"${__ans##*[![:space:]]}"}"
  printf -v "$__var" '%s' "${__ans:-$__def}"
}
# ask_secret VAR "Question"   (input not shown)
ask_secret() {
  local __var=$1 __q=$2 __ans
  printf '%s %s[blank = none]%s: ' "$__q" "$DIM" "$N"
  local __s=""; [ -t 3 ] && __s=s
  if ! IFS= read -r${__s} __ans <&3 && [ -z "$__ans" ]; then echo; die "Input ended before all questions were answered."; fi
  if [ -n "$__s" ]; then echo; fi
  printf -v "$__var" '%s' "$__ans"
}
# ask_yn "Question" y|n   -> exit status 0 = yes
ask_yn() {
  local __a
  while :; do
    ask __a "$1 (y/n)" "$2"
    case "${__a,,}" in y|yes) return 0 ;; n|no) return 1 ;; esac
    warn "Please answer y or n."
  done
}
# ask_int VAR "Question" default min max
ask_int() {
  local __v
  while :; do
    ask __v "$2" "$3"
    if [[ "$__v" =~ ^[0-9]+$ ]] && [ "$__v" -ge "$4" ] && [ "$__v" -le "$5" ]; then
      printf -v "$1" '%s' "$__v"; return
    fi
    warn "Enter a whole number from $4 to $5."
  done
}
# choose VAR "Question" default_number option...   (VAR gets the 1-based number)
choose() {
  local __var=$1 __q=$2 __def=$3; shift 3
  local i=1 o __n
  for o in "$@"; do printf '   %s%2d)%s %s\n' "$B" "$i" "$N" "$o"; i=$((i + 1)); done
  while :; do
    ask __n "$__q" "$__def"
    if [[ "$__n" =~ ^[0-9]+$ ]] && [ "$__n" -ge 1 ] && [ "$__n" -le $# ]; then
      printf -v "$__var" '%s' "$__n"; return
    fi
    warn "Pick a number from 1 to $#."
  done
}
# run CMD...   (prints instead of running in --dry-run)
run() {
  if [ "$DRY" = 1 ]; then printf '   %s+%s' "$DIM" "$N"; printf ' %q' "$@"; printf '\n'
  else "$@"; fi
}
ct() { run pct exec "$CTID" -- env LC_ALL=C.UTF-8 "$@"; }

CREATED=0
on_error() {
  local code=$? line=$1
  printf '\n%s✘ Failed (line %s, exit %s).%s\n' "$R" "$line" "$code" "$N" >&2
  if [ "$CREATED" = 1 ]; then
    printf '   Container %s was created. Inspect it with %spct enter %s%s,\n' "$CTID" "$B" "$CTID" "$N" >&2
    printf '   or remove it and start over with %spct stop %s; pct destroy %s%s\n' "$B" "$CTID" "$CTID" "$N" >&2
  fi
}
trap 'on_error $LINENO' ERR

# ----------------------------------------------------------------------------- preflight
printf '%s\nMovie Streamer: Proxmox installer%s%s\n' "$B" "$N" "$([ "$DRY" = 1 ] && printf '   %s(dry run: nothing will be changed)%s' "$Y" "$N")"
[ "$(id -u)" = 0 ] || die "Run this as root on the Proxmox node."
command -v pct >/dev/null && command -v pvesm >/dev/null || die "This doesn't look like a Proxmox VE node (pct not found)."
PVE_VER=$(pveversion | sed -E 's#^pve-manager/([0-9]+)\.([0-9]+).*#\1 \2#')
read -r PVE_MAJ PVE_MIN <<<"$PVE_VER"
if [ "$PVE_MAJ" -lt 8 ] || { [ "$PVE_MAJ" -eq 8 ] && [ "$PVE_MIN" -lt 2 ]; }; then
  die "Proxmox VE 8.2 or newer is needed for GPU passthrough to containers (this is $(pveversion | cut -d/ -f2))."
fi

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo /nonexistent)"
TMP=$(mktemp -d /tmp/movie-streamer-install.XXXXXX)
trap 'rm -rf "$TMP"' EXIT
if [ ! -f "$SRC_DIR/app.py" ] || [ ! -f "$SRC_DIR/install.sh" ]; then
  info "App files not found next to this script; downloading them..."
  curl -fsSL "$REPO_ARCHIVE" | tar -xz -C "$TMP" || die "Couldn't download $REPO_ARCHIVE"
  SRC_DIR=$(dirname "$(find "$TMP" -maxdepth 3 -name app.py | head -1)")
  [ -f "$SRC_DIR/app.py" ] || die "Downloaded archive doesn't contain the app."
fi

HOST_CPUS=$(nproc)
HOST_MEM=$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo)

# ----------------------------------------------------------------------------- GPU detection
# Fills GPU_* arrays: vendor (intel|amd|nvidia), label, usable (1/0), note, device node(s)
GPU_VENDOR=(); GPU_LABEL=(); GPU_OK=(); GPU_NOTE=(); GPU_NODE=(); GPU_NVIDX=()
NV_LIST=""
command -v nvidia-smi >/dev/null && NV_LIST=$(nvidia-smi --query-gpu=index,pci.bus_id,driver_version --format=csv,noheader 2>/dev/null || true)
for dev in /sys/bus/pci/devices/*; do
  class=$(cat "$dev/class")
  case "$class" in 0x03*) ;; *) continue ;; esac
  addr=$(basename "$dev"); vid=$(cat "$dev/vendor")
  drv=none; [ -e "$dev/driver" ] && drv=$(basename "$(readlink -f "$dev/driver")")
  name=$(lspci -s "$addr" 2>/dev/null | sed -E 's/^[^ ]+ [^:]+: //' || echo "$addr")
  node=""; nvidx=""; usable=0; note=""
  case "$vid" in
    0x8086|0x1002)
      v=intel; [ "$vid" = 0x1002 ] && v=amd
      if [ -e "/dev/dri/by-path/pci-$addr-render" ]; then
        node=$(readlink -f "/dev/dri/by-path/pci-$addr-render"); usable=1; note="$node"
      elif [ "$drv" = none ]; then
        note="no driver loaded on the host"
      else
        note="driver '$drv' has no render node"
      fi ;;
    0x10de)
      v=nvidia
      short=${addr#0000:}
      line=$(printf '%s\n' "$NV_LIST" | grep -i "$short" | head -1 || true)
      if [ "$drv" != nvidia ]; then
        note="needs the NVIDIA driver on the host (driver now: $drv)"
      elif [ -z "$line" ]; then
        note="nvidia-smi doesn't list it"
      else
        nvidx=$(echo "$line" | cut -d, -f1 | tr -d ' ')
        node="/dev/nvidia$nvidx"; usable=1
        note="$node, driver $(echo "$line" | cut -d, -f3 | tr -d ' ')"
      fi ;;
    *) continue ;;
  esac
  GPU_VENDOR+=("$v"); GPU_LABEL+=("$name"); GPU_OK+=("$usable"); GPU_NOTE+=("$note")
  GPU_NODE+=("$node"); GPU_NVIDX+=("$nvidx")
done

# ============================================================================= questions
step "Container"
NEXT=$(pvesh get /cluster/nextid 2>/dev/null || echo 200)
while :; do
  ask_int CTID "Container ID" "$NEXT" 100 999999999
  if ls /etc/pve/nodes/*/lxc/"$CTID".conf /etc/pve/nodes/*/qemu-server/"$CTID".conf >/dev/null 2>&1; then
    warn "ID $CTID is already in use."; continue
  fi
  break
done
while :; do
  ask CT_HOST "Hostname" "movie-streamer"
  [[ "$CT_HOST" =~ ^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$ ]] && break
  warn "Letters, numbers and dashes only."
done

step "GPU"
GPU_CHOICES=(); GPU_MAP=()
for i in "${!GPU_VENDOR[@]}"; do
  tag=$(printf '%s' "${GPU_VENDOR[$i]}" | tr a-z A-Z)
  if [ "${GPU_OK[$i]}" = 1 ]; then
    GPU_CHOICES+=("$tag: ${GPU_LABEL[$i]}  ${DIM}(${GPU_NOTE[$i]})${N}"); GPU_MAP+=("$i")
  else
    info "${DIM}Not usable: $tag ${GPU_LABEL[$i]} (${GPU_NOTE[$i]})${N}"
  fi
done
GPU_CHOICES+=("None: CPU only ${DIM}(works, but slow; 4K and HDR may not keep up)${N}"); GPU_MAP+=("-1")
choose GPU_PICK "Which GPU should the container use?" 1 "${GPU_CHOICES[@]}"
GI=${GPU_MAP[$((GPU_PICK - 1))]}
if [ "$GI" = -1 ]; then
  GPU=none; GPU_DESC="none (CPU only)"
else
  GPU=${GPU_VENDOR[$GI]}; GPU_DESC="${GPU_LABEL[$GI]} (${GPU_NOTE[$GI]})"
fi
NV_VER=""; NV_BOOT=0
if [ "$GPU" = nvidia ]; then
  NV_VER=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1 | tr -d ' ')
  info "The container gets the same NVIDIA user-space driver as the host ($NV_VER)."
  info "If you update the host driver later, reinstall it in the container to match."
  if ask_yn "Add a small boot service on the host so /dev/nvidia-uvm always exists (needed after reboots)?" y; then
    NV_BOOT=1
  fi
fi

step "Resources"
if [ "$GPU" = none ]; then DEF_CORES=$(( HOST_CPUS < 8 ? HOST_CPUS : 8 )); else DEF_CORES=$(( HOST_CPUS < 4 ? HOST_CPUS : 4 )); fi
DEF_MEM=2048; [ "$HOST_MEM" -lt 4096 ] && DEF_MEM=1024
DEF_DISK=8; [ "$GPU" = nvidia ] && DEF_DISK=12
info "Host has $HOST_CPUS CPU threads and $((HOST_MEM / 1024)) GB RAM. Suggested values are pre-filled."
ask_int CORES "CPU cores" "$DEF_CORES" 1 "$HOST_CPUS"
ask_int MEM "RAM (MB)" "$DEF_MEM" 512 "$HOST_MEM"
ask_int DISK "Disk size (GB)" "$DEF_DISK" 4 2048

mapfile -t STORES < <(pvesm status -content rootdir 2>/dev/null | awk 'NR>1 && $3=="active"{print $1"|"$2"|"int($6/1048576)}')
[ ${#STORES[@]} -gt 0 ] || die "No active storage can hold containers (content 'rootdir')."
# Default: the storage most existing containers use, else local-lvm, else the first
USED=$(grep -h '^rootfs:' /etc/pve/nodes/*/lxc/*.conf 2>/dev/null | sed -E 's/^rootfs: *([^:]+):.*/\1/' |
       sort | uniq -c | sort -rn | awk 'NR==1{print $2}')
OPTS=(); DEF_S=1; i=1
for s in "${STORES[@]}"; do
  IFS='|' read -r sn st sf <<<"$s"; OPTS+=("$sn ${DIM}($st, ${sf} GB free)${N}")
  if [ "$sn" = local-lvm ] && [ -z "$USED" ]; then DEF_S=$i; fi
  if [ "$sn" = "$USED" ]; then DEF_S=$i; fi
  i=$((i + 1))
done
choose SP "Storage for the container disk" "$DEF_S" "${OPTS[@]}"
ROOT_STORE=$(cut -d'|' -f1 <<<"${STORES[$((SP - 1))]}")

UNPRIV=1
if ! ask_yn "Unprivileged container (recommended)?" y; then UNPRIV=0; fi

step "Network"
mapfile -t ALLBR < <(ip -o link show type bridge 2>/dev/null | awk -F': ' '{print $2}' | cut -d@ -f1 | sort -V)
# Only Proxmox bridges (vmbrN); others such as docker0 aren't meant for guests
BRIDGES=(); for b in "${ALLBR[@]}"; do [[ "$b" == vmbr* ]] && BRIDGES+=("$b"); done
[ ${#BRIDGES[@]} -gt 0 ] || BRIDGES=("${ALLBR[@]}")
[ ${#BRIDGES[@]} -gt 0 ] || die "No network bridges found (vmbr0...)."
OPTS=(); DEF_B=1; i=1
for b in "${BRIDGES[@]}"; do
  ipb=$(ip -4 -o addr show dev "$b" 2>/dev/null | awk '{print $4}' | head -1)
  OPTS+=("$b ${DIM}${ipb:+(host IP $ipb)}${N}")
  [ "$b" = vmbr0 ] && DEF_B=$i
  i=$((i + 1))
done
choose BP "Network bridge" "$DEF_B" "${OPTS[@]}"
BRIDGE=${BRIDGES[$((BP - 1))]}
ask VLAN "VLAN tag (blank for none)" ""
[ -z "$VLAN" ] || [[ "$VLAN" =~ ^[0-9]+$ && "$VLAN" -ge 1 && "$VLAN" -le 4094 ]] || die "VLAN tag must be 1-4094."
if ask_yn "Use DHCP for the container's IP address?" y; then
  NET_IP=dhcp; NET_GW=""
else
  while :; do
    ask NET_IP "Static IP with prefix, e.g. 192.168.1.50/24" ""
    [[ "$NET_IP" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}/[0-9]{1,2}$ ]] && break
    warn "Use the form 192.168.1.50/24."
  done
  while :; do
    ask NET_GW "Gateway" "$(echo "$NET_IP" | cut -d/ -f1 | awk -F. '{print $1"."$2"."$3".1"}')"
    [[ "$NET_GW" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] && break
    warn "Enter an IP address like 192.168.1.1."
  done
fi

step "Media folders"
info "Add the folders on THIS Proxmox host that hold your movies/TV shows."
info "Network shares (NFS/SMB) must already be mounted on the host, e.g. under /mnt/..."
info "Each folder appears in the app under /media/<name>."
SHARE_HOST=(); SHARE_NAME=(); SHARE_RO=()
while :; do
  n=${#SHARE_HOST[@]}
  if [ "$n" -eq 0 ]; then q="Host folder path"; else q="Another folder (blank to finish)"; fi
  ask HP "$q" ""
  if [ -z "$HP" ]; then
    [ "$n" -gt 0 ] && break
    if ask_yn "No media folders added. Continue without any?" n; then break; else continue; fi
  fi
  HP="${HP%/}"
  [ -d "$HP" ] || { warn "'$HP' isn't a folder on this host."; continue; }
  [[ "$HP" == *,* ]] && { warn "Paths with commas can't be mounted into containers."; continue; }
  nm=${HP##*/}; nm=${nm//[^A-Za-z0-9._ -]/_}; [ -n "$nm" ] || nm="media$n"
  while :; do
    ask nm "  Name inside the app (/media/<name>)" "$nm"
    [[ "$nm" =~ ^[A-Za-z0-9._\ -]+$ ]] || { warn "Letters, numbers, spaces, dots and dashes only."; continue; }
    dup=0; for x in "${SHARE_NAME[@]}"; do [ "$x" = "$nm" ] && dup=1; done
    [ "$dup" = 0 ] && break; warn "'$nm' is already used."
  done
  ro=1; ask_yn "  Read-only (recommended: the app never changes files)?" y || ro=0
  SHARE_HOST+=("$HP"); SHARE_NAME+=("$nm"); SHARE_RO+=("$ro")
  ok "$HP → /media/$nm$([ "$ro" = 1 ] && echo ' (read-only)')"
done

step "Streaming (optional: you can also set these later in the web page)"
RTMP_KEY=""
while :; do
  ask RTMP_SERVER "RTMP server, e.g. rtmp://your-vps:1935/live (blank = later)" ""
  [ -z "$RTMP_SERVER" ] || [[ "$RTMP_SERVER" =~ ^rtmps?://[^[:space:]/]+ ]] && break
  warn "The server must start with rtmp:// or rtmps://"
done
if [ -n "$RTMP_SERVER" ]; then
  while :; do
    ask RTMP_KEY "Stream key" "movie"
    [[ "$RTMP_KEY" =~ ^[^[:space:]\"\'\\]+$ ]] && break
    warn "No spaces or quotes in the stream key."
  done
fi
while :; do
  ask_secret WEB_PW "Password for the web page (user 'admin')"
  [[ "$WEB_PW" != *"'"* ]] && break
  warn "Please don't use a single quote (') in this password."
done
ask_secret ROOT_PW "Root password for the container (for the Proxmox console)"

# ----------------------------------------------------------------------------- template
mapfile -t TSTORES < <(pvesm status -content vztmpl 2>/dev/null | awk 'NR>1 && $3=="active"{print $1}')
[ ${#TSTORES[@]} -gt 0 ] || die "No storage can hold container templates (content 'vztmpl')."
TSTORE=${TSTORES[0]}; for t in "${TSTORES[@]}"; do [ "$t" = local ] && TSTORE=local; done
TEMPLATE=""
for ver in 13 12; do
  TEMPLATE=$(pveam list "$TSTORE" 2>/dev/null | awk '{print $1}' | grep -E "debian-${ver}-standard_.*_amd64" | sort -V | tail -1 || true)
  [ -n "$TEMPLATE" ] && break
done
TEMPLATE_DL=""
if [ -z "$TEMPLATE" ]; then
  [ "$DRY" = 1 ] || pveam update >/dev/null 2>&1 || true
  for ver in 13 12; do
    TEMPLATE_DL=$(pveam available --section system 2>/dev/null | awk '{print $2}' | grep -E "debian-${ver}-standard_.*_amd64" | sort -V | tail -1 || true)
    [ -n "$TEMPLATE_DL" ] && break
  done
  [ -n "$TEMPLATE_DL" ] || die "No Debian 12/13 container template found or downloadable."
  TEMPLATE="$TSTORE:vztmpl/$TEMPLATE_DL"
fi

# ----------------------------------------------------------------------------- summary
step "Summary"
info "Container:  $CTID ($CT_HOST), $([ "$UNPRIV" = 1 ] && echo unprivileged || echo privileged), $(basename "$TEMPLATE")$([ -n "$TEMPLATE_DL" ] && echo ' (will download)')"
info "Resources:  $CORES cores, $MEM MB RAM, $DISK GB on $ROOT_STORE"
info "Network:    $BRIDGE${VLAN:+ VLAN $VLAN}, $([ "$NET_IP" = dhcp ] && echo DHCP || echo "$NET_IP via $NET_GW")"
info "GPU:        $GPU_DESC"
if [ ${#SHARE_HOST[@]} -eq 0 ]; then info "Media:      (none)"; fi
for i in "${!SHARE_HOST[@]}"; do
  info "Media:      ${SHARE_HOST[$i]} → /media/${SHARE_NAME[$i]}$([ "${SHARE_RO[$i]}" = 1 ] && echo ' (read-only)')"
done
info "Streaming:  ${RTMP_SERVER:-(set later in the web page)}${RTMP_SERVER:+ / key ${RTMP_KEY//?/•}}"
info "Web login:  $([ -n "$WEB_PW" ] && echo 'admin + password' || echo 'none (open on your LAN)')"
echo
ask_yn "Create the container and install?" y || die "Cancelled. Nothing was changed."

# ============================================================================= install
if [ -n "$TEMPLATE_DL" ]; then
  step "Downloading template $TEMPLATE_DL"
  run pveam download "$TSTORE" "$TEMPLATE_DL"
fi

step "Creating container $CTID"
NET="name=eth0,bridge=$BRIDGE,firewall=1,ip=$NET_IP${NET_GW:+,gw=$NET_GW}${VLAN:+,tag=$VLAN}"
[ "$NET_IP" = dhcp ] && NET="$NET,ip6=auto"
TAGS="movie-streamer"
if [ "$GPU" != none ]; then TAGS="$TAGS;$GPU"; fi
ARGS=(--hostname "$CT_HOST" --ostype debian --arch amd64 --unprivileged "$UNPRIV"
      --cores "$CORES" --memory "$MEM" --swap 512 --rootfs "$ROOT_STORE:$DISK"
      --net0 "$NET" --features nesting=1 --onboot 1 --timezone host --tags "$TAGS")
for i in "${!SHARE_HOST[@]}"; do
  MP="${SHARE_HOST[$i]},mp=/media/${SHARE_NAME[$i]}"
  if [ "${SHARE_RO[$i]}" = 1 ]; then MP="$MP,ro=1"; fi
  ARGS+=("--mp$i" "$MP")
done
run pct create "$CTID" "$TEMPLATE" "${ARGS[@]}"
CREATED=1
ok "created"

wait_net() {
  [ "$DRY" = 1 ] && return
  local t=0
  until pct exec "$CTID" -- getent hosts deb.debian.org >/dev/null 2>&1; do
    t=$((t + 3)); [ $t -ge 120 ] && die "The container has no internet access after 2 minutes (check bridge/DHCP/VLAN)."
    sleep 3
  done
}

run pct start "$CTID"
info "waiting for the network..."
wait_net
ok "container is up"

if [ "$GPU" != none ]; then
  step "Passing the GPU through"
  if [ "$GPU" = nvidia ]; then
    NVMP=$(command -v nvidia-modprobe || true)
    if [ ! -e /dev/nvidia-uvm ] && [ -n "$NVMP" ]; then run "$NVMP" -u -c=0; fi
    if [ "$NV_BOOT" = 1 ] && [ -z "$NVMP" ]; then
      warn "nvidia-modprobe isn't installed on the host, so the boot service can't be added."
    elif [ "$NV_BOOT" = 1 ]; then
      if [ "$DRY" = 1 ]; then info "+ write /etc/systemd/system/nvidia-uvm-init.service and enable it"
      else
        cat > /etc/systemd/system/nvidia-uvm-init.service <<EOF
[Unit]
Description=Create /dev/nvidia-uvm device nodes (for LXC GPU passthrough)
After=systemd-modules-load.service
Before=pve-guests.service

[Service]
Type=oneshot
ExecStart=$NVMP -u -c=0
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
        systemctl daemon-reload; systemctl enable -q nvidia-uvm-init.service
      fi
    fi
    NV_NODES=("/dev/nvidia${GPU_NVIDX[$GI]}" /dev/nvidiactl /dev/nvidia-uvm /dev/nvidia-uvm-tools)
    for c in /dev/nvidia-caps/nvidia-cap1 /dev/nvidia-caps/nvidia-cap2; do [ -e "$c" ] && NV_NODES+=("$c"); done
    d=0
    for nd in "${NV_NODES[@]}"; do
      [ -e "$nd" ] || [ "$DRY" = 1 ] || die "$nd doesn't exist on the host (is the NVIDIA driver loaded?)"
      run pct set "$CTID" "--dev$d" "$nd,mode=0666"; d=$((d + 1))
    done
  else
    # The render group's id differs between distro versions, so read it from the container.
    if [ "$DRY" = 1 ]; then RGID="<render gid>"
    else
      RGID=$(pct exec "$CTID" -- getent group render 2>/dev/null | cut -d: -f3 || true)
      [ -n "$RGID" ] || RGID=$(pct exec "$CTID" -- getent group video 2>/dev/null | cut -d: -f3 || echo 44)
    fi
    run pct set "$CTID" --dev0 "${GPU_NODE[$GI]},gid=$RGID,mode=0660"
  fi
  info "restarting the container to apply..."
  run pct reboot "$CTID"
  wait_net
  ok "GPU passed through"
fi

if [ -n "$ROOT_PW" ]; then
  if [ "$DRY" = 1 ]; then info "+ set the container's root password"
  else printf 'root:%s\n' "$ROOT_PW" | pct exec "$CTID" -- chpasswd; fi
fi

step "Installing Movie Streamer (takes a few minutes)"
info "updating the container..."
ct bash -c 'export DEBIAN_FRONTEND=noninteractive; apt-get update -qq && apt-get -y -qq full-upgrade >/dev/null'
tar -C "$SRC_DIR" --exclude=.git --exclude=__pycache__ -czf "$TMP/app.tgz" .
run pct push "$CTID" "$TMP/app.tgz" /root/movie-streamer-src.tgz
ct bash -c 'rm -rf /root/movie-streamer-src && mkdir /root/movie-streamer-src && tar -xzf /root/movie-streamer-src.tgz -C /root/movie-streamer-src && rm /root/movie-streamer-src.tgz'

# Settings file (the app's install.sh keeps an existing one)
case "$GPU" in intel) ENC=vaapi ;; amd) ENC=vaapi-amd ;; nvidia) ENC=nvenc ;; *) ENC=x264 ;; esac
VAAPI_NODE=/dev/dri/renderD128
if [ "$GPU" = intel ] || [ "$GPU" = amd ]; then VAAPI_NODE=${GPU_NODE[$GI]}; fi
{
  sed -e "s#^MEDIA_ROOT=.*#MEDIA_ROOT=/media#" \
      -e "s#^ENCODER=.*#ENCODER=$ENC#" \
      -e "s#^VAAPI_DEVICE=.*#VAAPI_DEVICE=$VAAPI_NODE#" \
      -e "s#^CUDA_DEVICE=.*#CUDA_DEVICE=0#" \
      -e "/^APP_PASSWORD=/d" "$SRC_DIR/movie-streamer.env"
  # single quotes: read literally by both systemd and bash (no $ or \ surprises)
  printf "APP_PASSWORD='%s'\n" "$WEB_PW"
} > "$TMP/movie-streamer.env"
chmod 600 "$TMP/movie-streamer.env"
ct mkdir -p /media
run pct push "$CTID" "$TMP/movie-streamer.env" /etc/movie-streamer.env --perms 0600

INSTALL_LOG="$TMP/install.log"
if [ "$DRY" = 1 ]; then
  ct env GPU="$GPU" NVIDIA_VERSION="$NV_VER" bash /root/movie-streamer-src/install.sh
else
  pct exec "$CTID" -- env LC_ALL=C.UTF-8 GPU="$GPU" NVIDIA_VERSION="$NV_VER" \
      bash /root/movie-streamer-src/install.sh 2>&1 | tee "$INSTALL_LOG" | sed 's/^/   | /'
fi

# Stream destination: saved through the app's own API. The key and password go in a
# root-only file, not on the command line (where other users could see them in ps).
if [ -n "$RTMP_SERVER" ] && [ "$DRY" = 0 ]; then
  RTMP_SERVER="$RTMP_SERVER" RTMP_KEY="$RTMP_KEY" python3 -c \
    'import json, os; print(json.dumps({"server": os.environ["RTMP_SERVER"], "key": os.environ["RTMP_KEY"]}))' \
    > "$TMP/settings.json"
  pw_esc=${WEB_PW//\\/\\\\}; pw_esc=${pw_esc//\"/\\\"}
  printf 'user = "admin:%s"\ndata = "@/root/.ms-settings.json"\n' "$pw_esc" > "$TMP/curl.cfg"
  pct push "$CTID" "$TMP/settings.json" /root/.ms-settings.json --perms 0600
  pct push "$CTID" "$TMP/curl.cfg" /root/.ms-curl.cfg --perms 0600
  saved=0
  for _ in 1 2 3 4 5; do
    if pct exec "$CTID" -- curl -fsS -o /dev/null -K /root/.ms-curl.cfg -H 'Content-Type: application/json' \
         http://127.0.0.1:8080/api/settings 2>/dev/null; then saved=1; break; fi
    sleep 2
  done
  pct exec "$CTID" -- rm -f /root/.ms-settings.json /root/.ms-curl.cfg
  [ "$saved" = 1 ] && ok "stream settings saved" || warn "couldn't save the stream settings; enter them in the web page"
fi

# ----------------------------------------------------------------------------- checks
step "Checks"
if [ "$DRY" = 1 ]; then
  info "(dry run: checks skipped)"
  IP="<container IP>"
else
  grep -q '^SERVICE=active' "$INSTALL_LOG" && ok "app service running" || warn "the app service isn't running: pct exec $CTID -- journalctl -u movie-streamer"
  if grep -q '^ENCODE_TEST=ok' "$INSTALL_LOG"; then ok "GPU video encoding works"
  elif grep -q '^ENCODE_TEST=skipped' "$INSTALL_LOG"; then info "CPU-only: no GPU test"
  else
    warn "GPU encode test FAILED (see the install output above). Streaming won't work until"
    warn "  that's fixed. To use the CPU instead: set ENCODER=x264 in /etc/movie-streamer.env"
    warn "  inside the container, then: systemctl restart movie-streamer"
  fi
  for i in "${!SHARE_HOST[@]}"; do
    p="/media/${SHARE_NAME[$i]}"
    if cnt=$(pct exec "$CTID" -- runuser -u streamer -- sh -c 'ls -A "$1" >/dev/null && ls -A "$1" | wc -l' _ "$p" 2>/dev/null); then
      ok "$p readable ($cnt entries)"
    else
      warn "$p is NOT readable by the app."
      [ "$UNPRIV" = 1 ] && warn "  Unprivileged containers see host files as 'nobody'; make them world-readable (chmod -R o+rX '${SHARE_HOST[$i]}') or use a privileged container."
    fi
  done
  IP=$(pct exec "$CTID" -- hostname -I 2>/dev/null | awk '{print $1}')
  [ -n "$IP" ] || IP=$(echo "$NET_IP" | cut -d/ -f1)
fi

printf '\n%s%s✔ Done!%s\n\n' "$B" "$G" "$N"
printf '   Open:        %shttp://%s:8080%s\n' "$B" "$IP" "$N"
[ -n "$WEB_PW" ] && printf '   Login:       admin / (the password you chose)\n'
if [ -n "$RTMP_SERVER" ]; then
  printf '   Watch (VLC): %s/%s\n' "${RTMP_SERVER%/}" "$RTMP_KEY"
else
  printf '   Then:        open %s⚙ Stream settings%s and enter your RTMP server + stream key\n' "$B" "$N"
fi
printf '   Shell:       pct enter %s\n\n' "$CTID"
