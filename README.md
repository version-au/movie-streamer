# Movie Streamer

A small web UI that runs in a Proxmox LXC. You browse your movie library, pick an
audio track and a subtitle track, and it pushes the movie to an RTMP server (e.g.
`tiangolo/nginx-rtmp` on a VPS) with ffmpeg, using the GPU to decode, tone-map and
encode. It replaces a remote-desktop + OBS setup.

```
[LXC] web UI → ffmpeg (GPU H.264) ──RTMP──► [VPS] nginx-rtmp ──► VLC
```

## Quick install on Proxmox

On the Proxmox node (VE 8.2 or newer), as root:

```bash
git clone https://github.com/version-au/movie-streamer.git
cd movie-streamer
bash proxmox-install.sh            # or: bash proxmox-install.sh --dry-run
```

Or in one line, without cloning (the script downloads the rest of the app itself):

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/version-au/movie-streamer/main/proxmox-install.sh)
```

It asks a few questions, with suggested answers pre-filled:

- container ID and hostname
- **GPU**: lists the GPUs it finds (Intel, AMD, NVIDIA), says which are usable
  and why not, or choose CPU only
- CPU cores, RAM and disk (suggested from your host and GPU), and the storage
- network bridge, optional VLAN, DHCP or a static IP
- **media folders** on the host (local disks, or NFS/SMB shares already mounted on
  the host). Each shows up in the app under `/media/<name>`, read-only by default.
- optionally: RTMP server and stream key, a web page password, a root password

It then creates the container, passes the folders and GPU through, installs
everything, checks that GPU encoding works and the folders are readable, and
prints the address to open. `--dry-run` asks the same questions and shows the
commands without changing anything.

| GPU | What runs where | Status |
|---|---|---|
| Intel (Arc, iGPU 7th gen+) | all on the GPU (VA-API) | tested (Arc A380) |
| AMD (amdgpu driver) | decode, scale and encode on the GPU; HDR tone-mapping on the CPU | pipeline tested on Intel; untested on AMD hardware |
| NVIDIA | all on the GPU (CUDA/NVENC). The host needs the NVIDIA driver; the container gets the matching user-space driver. | untested |
| None | CPU only | works; 4K/HDR may be too slow |

## Requirements

- Proxmox LXC running Debian 12/13 (unprivileged is fine) with a GPU passed
  through (see the table above).
- ffmpeg: `install.sh` installs `jellyfin-ffmpeg` from Jellyfin's repo. It bundles
  current Intel and AMD VA-API drivers, the CUDA filters and the tone-mapping filters.
- Tests (copy them into the container, e.g. `/opt/movie-streamer/tests/`; see each file's docstring):
  - `pipeline_test.py`: every decode/subtitle/HDR combination
  - `e2e_queue_test.py`: queue, seek, skip and reconnect against a local RTMP receiver
  - `stream_test.sh "<path>" <seek>`: one movie through the service, with timing and timestamp checks
  - `late_audio_test.py`: exact A/V sync using synthetic beep files
  - `file_test.py "<path>"`: the app's command on one file in several variations (for "this file won't play")
  - `av1_hw_scan.sh`: tries GPU decoding on every AV1 file in the library

## Manual install

### 1. Create the LXC (Proxmox host)

Create a Debian 12 container (unprivileged is fine; 2 cores and 1–2 GB RAM is
enough), then add the following to `/etc/pve/lxc/<CTID>.conf`:

```
# iGPU (Proxmox 8.2+). gid must match the "render" group INSIDE the container (Debian 12 = 104 usually)
dev0: /dev/dri/renderD128,gid=104
# Your movies, read-only
mp0: /path/on/host/to/movies,mp=/media,ro=1
```

To check the gid, run `getent group render` inside the container. If your host
has more than one GPU, the Intel one may be `renderD129`; `ls -l /dev/dri/by-path`
on the host tells you which.

### 2. Install (inside the LXC)

Copy this folder into the container, then:

```bash
GPU=intel bash install.sh         # intel | amd | none, or: GPU=nvidia NVIDIA_VERSION=<host driver version>
nano /etc/movie-streamer.env      # check MEDIA_ROOT and VAAPI_DEVICE
systemctl restart movie-streamer
```

Open `http://<lxc-ip>:8080`.

## Watch

Open **⚙ Stream settings** in the web UI and enter the destination the same
way you would in OBS:

- **Server:** `rtmp://YOUR_VPS_IP/live` (for tiangolo/nginx-rtmp the app is `live`, port 1935)
- **Stream key:** anything, e.g. `movie`

These are saved in `/var/lib/movie-streamer/settings.json` (readable only by
the service user). The panel shows the matching VLC URL: in VLC use
**Media → Open Network Stream** → `rtmp://YOUR_VPS_IP/live/movie`.
`RTMP_URL` in the env file is only the default used before you save anything.

## Features

- Browse folders or search the whole library
- Choose an **audio track** (downmixed to stereo AAC)
- Choose a **subtitle track**, burned into the picture. This works with
  embedded text subs (SRT/ASS), embedded image subs (PGS/VobSub), and external
  files next to the movie (`Movie.srt`, `Movie.en.srt`, `Movie.en.forced.srt`).
  RTMP can't carry separate subtitle tracks, so burning them in is the only
  option.
- Start partway through (`1:02:30`), with subtitles kept in sync
- Max resolution and bitrate per stream
- **Queue:** add movies with their own track choices, reorder or remove them,
  and they play one after another automatically. The queue survives restarts
  (`/var/lib/movie-streamer/queue.json`).
- **Seek:** click anywhere on the progress bar, or focus it and use ←/→ for ±30s
- **Sort** the library by name or by date added (newest video in each folder)
- **One continuous stream:** a long-lived relay keeps the RTMP connection open,
  so VLC keeps playing across seeks and queue changes. Every movie is
  letterboxed to the same 16:9 frame. If the connection to the VPS drops, it
  reconnects and resumes where it was (gives up after 5 drops in 3 minutes).
- Live position, speed and the ffmpeg log in the page; Next and Stop buttons
- Runs under systemd, so it starts on boot and restarts if it crashes

## Settings (`/etc/movie-streamer.env`)

| Variable | Default | |
|---|---|---|
| `MEDIA_ROOT` | `/media` | Library folder |
| `RTMP_URL` | — | `rtmp://VPS/live/<key>` |
| `ENCODER` | `vaapi` | `vaapi` (Intel), `vaapi-amd` (AMD), `nvenc` (NVIDIA) or `x264` (CPU) |
| `VAAPI_DEVICE` | `/dev/dri/renderD128` | Intel/AMD render node |
| `CUDA_DEVICE` | `0` | NVIDIA GPU index |
| `VIDEO_BITRATE` | `6M` | Default in the UI |
| `MAX_HEIGHT` | `1080` | Default in the UI (0 = original) |
| `AUDIO_BITRATE` | `192k` | |
| `PORT` | `8080` | |
| `APP_PASSWORD` | empty | Set this to require a login (user `APP_USER`, default `admin`) |

## Troubleshooting

- **The stream fails right away.** Open "ffmpeg log" in the page. If the GPU
  can't decode a file ("HW accel end frame fail" etc.), the app switches that
  movie to CPU decoding by itself and resumes at the same spot. Tone mapping,
  scaling and encoding stay on the GPU. You can also untick **Hardware
  decoding** before starting.
- **Movies whose audio starts late.** Some files (e.g. *Onslaught (2026)*, where
  the audio starts at 0:11) are handled by padding silence until the audio
  starts. The log says "Audio starts N s after the picture; padding with
  silence". A/V sync is checked by `tests/late_audio_test.py`.
- **`vainfo` fails.** The `gid=` in the LXC config doesn't match the
  container's render group, or the device path is wrong.
- **HDR looks washed out.** Make sure "Tone-map HDR → SDR" is ticked. It only
  appears for files detected as HDR10/HLG/Dolby Vision. Tone mapping runs on
  the GPU (`tonemap_vaapi`). Dolby Vision profile 5 files have no HDR10 base
  layer, so their colours can still be off.
- **Embedded text subtitles take a while to start the first time.** The track
  is extracted once (the whole file has to be read) and cached in
  `/var/cache/movie-streamer`. It starts extracting as soon as you pick the
  subtitle, and later plays are instant.
- **Subtitles show as boxes.** Install more fonts: `apt install fonts-noto-core`.
- **VLC stutters.** Lower the bitrate (your VPS upload and the viewer's
  download both have to keep up), or raise VLC's network caching
  (Show more options → Caching 3000 ms).
