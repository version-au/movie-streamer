"""
Movie Streamer - browse a media library and push a movie to an RTMP server
(e.g. tiangolo/nginx-rtmp) using ffmpeg with Intel VAAPI hardware encoding.

Configuration is via environment variables (see README.md).
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from collections import deque
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
MEDIA_ROOT = Path(os.environ.get("MEDIA_ROOT", "/media")).resolve()
RTMP_URL = os.environ.get("RTMP_URL", "rtmp://YOUR_VPS/live/movie")
# GPU profile:  vaapi = Intel (all on GPU) | vaapi-amd = AMD (VA-API, CPU tone-map/pad)
#               nvenc = NVIDIA (CUDA/NVENC)  | x264 = no GPU
ENCODER = os.environ.get("ENCODER", "vaapi").lower()
if ENCODER not in ("vaapi", "vaapi-amd", "nvenc", "x264"):
    raise SystemExit(f"Unknown ENCODER={ENCODER!r} (use vaapi, vaapi-amd, nvenc or x264)")
VAAPI_DEVICE = os.environ.get("VAAPI_DEVICE", "/dev/dri/renderD128")
CUDA_DEVICE = os.environ.get("CUDA_DEVICE", "0")             # NVIDIA GPU index
DEFAULT_BITRATE = os.environ.get("VIDEO_BITRATE", "6M")
DEFAULT_MAX_HEIGHT = int(os.environ.get("MAX_HEIGHT", "1080"))
AUDIO_BITRATE = os.environ.get("AUDIO_BITRATE", "192k")
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE", "ffprobe")
APP_USER = os.environ.get("APP_USER", "admin")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")              # empty = no auth
STATE_DIR = Path(os.environ.get("STATE_DIR", "/var/lib/movie-streamer"))
SUB_CACHE = Path(os.environ.get("SUB_CACHE", "/var/cache/movie-streamer"))
SUB_CACHE_DAYS = int(os.environ.get("SUB_CACHE_DAYS", "60"))
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8080"))

VIDEO_EXTS = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".ts", ".m2ts", ".wmv",
              ".webm", ".mpg", ".mpeg", ".flv"}
SUB_EXTS = {".srt", ".ass", ".ssa", ".vtt"}
TEXT_SUB_CODECS = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text"}
IMAGE_SUB_CODECS = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub"}

app = Flask(__name__, static_folder=None)
STATIC_DIR = Path(__file__).parent / "static"


# --------------------------------------------------------------------------
# Auth (optional HTTP basic auth)
# --------------------------------------------------------------------------
@app.before_request
def check_auth():
    if not APP_PASSWORD:
        return None
    auth = request.authorization
    if auth and auth.username == APP_USER and auth.password == APP_PASSWORD:
        return None
    return Response("Authentication required", 401,
                    {"WWW-Authenticate": 'Basic realm="Movie Streamer"'})


# --------------------------------------------------------------------------
# Path helpers
# --------------------------------------------------------------------------
def safe_path(rel: str) -> Path:
    """Resolve a path relative to MEDIA_ROOT and refuse anything outside it."""
    p = (MEDIA_ROOT / (rel or "").lstrip("/\\")).resolve()
    if p != MEDIA_ROOT and MEDIA_ROOT not in p.parents:
        raise ValueError("Path outside media root")
    return p


def rel_path(p: Path) -> str:
    return p.relative_to(MEDIA_ROOT).as_posix()


# --------------------------------------------------------------------------
# Library browsing
# --------------------------------------------------------------------------
def dir_added(d: Path) -> float:
    """'Date added' for a folder: newest video file directly inside it (so a later
    subtitle download doesn't bump it), falling back to the folder's own mtime."""
    best = 0.0
    try:
        with os.scandir(d) as it:
            for e in it:
                if os.path.splitext(e.name)[1].lower() in VIDEO_EXTS and e.is_file():
                    best = max(best, e.stat().st_mtime)
    except OSError:
        pass
    if not best:
        try:
            best = d.stat().st_mtime
        except OSError:
            pass
    return best


@app.get("/api/browse")
def browse():
    try:
        d = safe_path(request.args.get("path", ""))
    except ValueError as e:
        return jsonify(error=str(e)), 400
    if not d.is_dir():
        return jsonify(error="Not a directory"), 404

    dirs, files = [], []
    try:
        entries = sorted(d.iterdir(), key=lambda e: e.name.lower())
    except PermissionError:
        return jsonify(error="Permission denied"), 403
    for e in entries:
        if e.name.startswith("."):
            continue
        try:
            if e.is_dir():
                dirs.append({"name": e.name, "path": rel_path(e), "added": dir_added(e)})
            elif e.suffix.lower() in VIDEO_EXTS:
                st = e.stat()
                files.append({"name": e.name, "path": rel_path(e),
                              "size": st.st_size, "added": st.st_mtime})
        except OSError:
            continue
    rp = "" if d == MEDIA_ROOT else rel_path(d)
    return jsonify(path=rp, dirs=dirs, files=files)


@app.get("/api/search")
def search():
    q = request.args.get("q", "").strip().lower()
    if len(q) < 2:
        return jsonify(files=[])
    terms = q.split()
    results = []
    for root, dnames, fnames in os.walk(MEDIA_ROOT, followlinks=False):
        dnames[:] = [n for n in dnames if not n.startswith(".")]
        for f in fnames:
            if Path(f).suffix.lower() not in VIDEO_EXTS:
                continue
            low = f.lower()
            if all(t in low for t in terms):
                p = Path(root) / f
                try:
                    added = p.stat().st_mtime
                except OSError:
                    added = 0
                results.append({"name": f, "path": rel_path(p), "added": added,
                                "folder": rel_path(p.parent) if p.parent != MEDIA_ROOT else ""})
                if len(results) >= 200:
                    return jsonify(files=results, truncated=True)
    results.sort(key=lambda r: r["name"].lower())
    return jsonify(files=results)


# --------------------------------------------------------------------------
# Probing
# --------------------------------------------------------------------------
def ffprobe(path: Path) -> dict:
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or "ffprobe failed")
    return json.loads(out.stdout)


def external_subs(movie: Path) -> list:
    """Find subtitle files next to the movie: Movie.srt, Movie.en.srt, Movie.en.forced.srt ..."""
    subs = []
    stem = movie.stem.lower()
    try:
        siblings = sorted(movie.parent.iterdir(), key=lambda e: e.name.lower())
    except OSError:
        return subs
    for f in siblings:
        if f.suffix.lower() not in SUB_EXTS or not f.is_file():
            continue
        fstem = f.stem
        if fstem.lower() == stem:
            label = "external"
        elif fstem.lower().startswith(stem + "."):
            label = fstem[len(stem) + 1:]
        else:
            continue
        subs.append({"file": f.name, "label": label, "codec": f.suffix[1:].lower()})
    return subs


def detect_hdr(stream: dict):
    """Return 'HDR10', 'HLG', 'Dolby Vision' (+ base layer) or None."""
    trc = stream.get("color_transfer") or ""
    dovi = None
    for sd in stream.get("side_data_list") or []:
        if "DOVI" in (sd.get("side_data_type") or "") or "dv_profile" in sd:
            dovi = sd.get("dv_profile")
    base = "HDR10" if trc == "smpte2084" else "HLG" if trc == "arib-std-b67" else None
    if dovi is not None:
        return f"Dolby Vision P{dovi}" + (f" ({base})" if base else "")
    return base


@app.get("/api/probe")
def probe():
    try:
        p = safe_path(request.args.get("path", ""))
    except ValueError as e:
        return jsonify(error=str(e)), 400
    if not p.is_file():
        return jsonify(error="File not found"), 404
    try:
        info = ffprobe(p)
    except Exception as e:  # noqa: BLE001
        return jsonify(error=f"ffprobe: {e}"), 500

    audio, subs, video = [], [], None
    for s in info.get("streams", []):
        tags = {k.lower(): v for k, v in (s.get("tags") or {}).items()}
        disp = s.get("disposition") or {}
        kind = s.get("codec_type")
        if kind == "video" and video is None and not disp.get("attached_pic"):
            video = {"codec": s.get("codec_name"), "width": s.get("width"),
                     "height": s.get("height"), "pix_fmt": s.get("pix_fmt"),
                     "hdr": detect_hdr(s)}
        elif kind == "audio":
            audio.append({
                "index": len(audio),
                "codec": s.get("codec_name"),
                "channels": s.get("channels"),
                "layout": s.get("channel_layout"),
                "language": tags.get("language", ""),
                "title": tags.get("title", ""),
                "default": bool(disp.get("default")),
            })
        elif kind == "subtitle":
            codec = s.get("codec_name") or ""
            subs.append({
                "index": len(subs),
                "codec": codec,
                "kind": "image" if codec in IMAGE_SUB_CODECS else
                        "text" if codec in TEXT_SUB_CODECS else "unknown",
                "language": tags.get("language", ""),
                "title": tags.get("title", ""),
                "default": bool(disp.get("default")),
                "forced": bool(disp.get("forced")),
            })

    try:
        duration = float(info.get("format", {}).get("duration", 0))
    except (TypeError, ValueError):
        duration = 0
    return jsonify(path=rel_path(p), name=p.name, duration=duration, video=video,
                   audio=audio, subtitles=subs, external_subtitles=external_subs(p))


# --------------------------------------------------------------------------
# Embedded text subtitle extraction + cache
#
# ffmpeg's subtitles filter reads the *whole* movie to collect a subtitle track
# before playback starts (~1 min for a 4K remux). Instead we extract the track
# once, cache it, and point the filter at the small file.
# --------------------------------------------------------------------------
_sub_guard = threading.Lock()
_sub_locks: dict = {}
_prefetch = {"proc": None, "key": None}


def needs_extract(sub) -> bool:
    return bool(sub) and sub.get("type") == "internal" and sub.get("kind") != "image"


def _sub_key(src: Path, idx: int) -> str:
    st = src.stat()
    return hashlib.sha1(f"{src}|{st.st_mtime_ns}|{st.st_size}|{idx}".encode()).hexdigest()[:24]


def cached_text_sub(src: Path, idx: int, codec: str, on_proc=None) -> Path:
    key = _sub_key(src, idx)
    d = SUB_CACHE / key
    is_ass = codec in ("ass", "ssa")
    out = d / ("sub.ass" if is_ass else "sub.srt")
    with _sub_guard:
        lk = _sub_locks.setdefault(key, threading.Lock())
    with lk:
        if out.is_file():
            os.utime(d)
            return out
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / ("tmp.ass" if is_ass else "tmp.srt")
        cmd = [FFMPEG, "-nostdin", "-hide_banner", "-v", "error", "-y"]
        if is_ass:   # keep embedded fonts for styled subs
            cmd += ["-dump_attachment:t", ""]
        cmd += ["-i", str(src), "-map", f"0:s:{idx}",
                "-c:s", "copy" if (is_ass or codec == "subrip") else "srt",
                "-f", "ass" if is_ass else "srt", str(tmp)]
        fonts = d / "fonts"
        fonts.mkdir(exist_ok=True)
        p = subprocess.Popen(cmd, cwd=fonts, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        if on_proc:
            on_proc(p, key)
        _, err = p.communicate()
        if p.returncode != 0 or not tmp.is_file():
            tmp.unlink(missing_ok=True)
            raise RuntimeError("subtitle extraction failed: " + (err or "").strip()[-300:])
        tmp.replace(out)
        return out


def prune_sub_cache():
    if not SUB_CACHE.is_dir():
        return
    cutoff = time.time() - SUB_CACHE_DAYS * 86400
    for d in SUB_CACHE.iterdir():
        try:
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


def _set_prefetch(p, key):
    _prefetch.update(proc=p, key=key)


@app.post("/api/prepare_sub")
def prepare_sub():
    """Start extracting a subtitle track in the background (when it's selected in the UI)."""
    body = request.get_json(force=True, silent=True) or {}
    try:
        src = safe_path(body.get("path", ""))
        idx = int(body.get("index", 0))
        key = _sub_key(src, idx)
    except (ValueError, OSError) as e:
        return jsonify(error=str(e)), 400
    # Only one background extraction at a time: cancel a different one.
    old = _prefetch.get("proc")
    if old and old.poll() is None and _prefetch.get("key") != key:
        old.kill()

    def run():
        try:
            cached_text_sub(src, idx, str(body.get("codec", "")), on_proc=_set_prefetch)
        except Exception:  # noqa: BLE001  (cancelled or failed; start() will retry)
            pass
    threading.Thread(target=run, daemon=True).start()
    return jsonify(ok=True)


# --------------------------------------------------------------------------
# Streaming
#
# One long-lived "relay" ffmpeg holds the RTMP connection to the server and
# copies an MPEG-TS feed into it. Each movie (and each seek) is a short-lived
# "segment" encoder whose output is piped into the relay, with timestamps that
# continue from the relay's clock. Every segment is letterboxed to the same
# 16:9 frame, so VLC sees one unbroken stream across seeks and queue changes.
# If the connection to the server drops, the relay is restarted and the movie
# resumes where it was.
# --------------------------------------------------------------------------
TS_PACKET = 188
QUEUE_FILE = STATE_DIR / "queue.json"


def validate_bitrate(opts: dict) -> str:
    bitrate = str(opts.get("bitrate") or DEFAULT_BITRATE)
    if not re.fullmatch(r"\d+(\.\d+)?[kKmM]?", bitrate):
        raise ValueError("Bad bitrate")
    return bitrate


def canvas_size(opts: dict, src: Path) -> tuple:
    """Fixed 16:9 output frame. 'Original' (0) = sized from the source width."""
    max_h = opts.get("max_height")
    max_h = DEFAULT_MAX_HEIGHT if max_h in (None, "") else int(max_h)
    if max_h <= 0:
        try:
            out = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0",
                                  "-show_entries", "stream=width", "-of", "csv=p=0", str(src)],
                                 capture_output=True, text=True, timeout=30).stdout
            w = int(out.strip().splitlines()[0])
        except (ValueError, IndexError, OSError, subprocess.SubprocessError):
            w = 1920
        max_h = round(w * 9 / 16)
    h = max(2, max_h // 2 * 2)
    return round(h * 16 / 9 / 2) * 2, h


def audio_lead(src: Path, audio_idx: int, pos: float) -> float:
    """How many seconds after the picture (from `pos`) the audio track really starts.
    0 for normal files; e.g. ~11s for a movie whose audio begins at 0:11."""
    def first_pts(sel):
        try:
            out = subprocess.run([FFPROBE, "-v", "error", "-select_streams", sel,
                                  "-read_intervals", f"{pos:.3f}%+#1", "-show_entries",
                                  "packet=pts_time", "-of", "csv=p=0", str(src)],
                                 capture_output=True, text=True, timeout=20).stdout.split()
            return float(out[0])
        except (ValueError, IndexError, OSError, subprocess.SubprocessError):
            return None
    a, v = first_pts(f"a:{audio_idx}"), first_pts("v:0")
    if a is None or v is None:
        return 0.0
    return max(0.0, a - max(pos, v))


def relay_command(url: str) -> list:
    return [FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "warning", "-nostats",
            "-fflags", "+genpts+discardcorrupt", "-probesize", "2M", "-analyzeduration", "3M",
            # No -re here: segments are already paced by their own -re. Pacing the relay
            # too made every seek/movie change add the relay's startup lag (gaps grew to
            # 4-7s and accumulated).
            "-f", "mpegts", "-i", "pipe:0", "-map", "0:v", "-map", "0:a", "-c", "copy",
            "-f", "flv", "-flvflags", "no_duration_filesize", url]


def _mask_url(url: str) -> str:
    server, _, key = url.rpartition("/")
    return f"{server}/******" if server and key and "://" in server else url


def _fmt(sec: float) -> str:
    sec = int(max(0, sec))
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


class Streamer:
    def __init__(self):
        self.lock = threading.RLock()
        self.wlock = threading.Lock()       # serialises writes into the relay
        self.relay = None
        self.relay_t0 = 0.0
        self.seg = None
        self.seg_gen = 0
        self.seg_start = 0.0                # movie position where this segment began
        self.seg_ts_offset = 0.0
        self.ts_head = 0.0                  # furthest timestamp sent to the relay
        self.prep_proc = None
        self.current = None                 # item being played
        self.state = "idle"                 # idle | preparing | playing | failed
        self.preparing_msg = None
        self.error = None
        self.queue = self._load_queue()
        self.log = deque(maxlen=300)
        self.progress = {}
        self.reconnects = deque(maxlen=20)

    # ---- items & queue persistence ----------------------------------------------
    @staticmethod
    def make_item(body: dict) -> dict:
        src = safe_path(body.get("path", ""))
        if not src.is_file():
            raise ValueError("File not found")
        validate_bitrate(body)
        opts = {k: body[k] for k in ("audio", "subtitle", "max_height", "bitrate",
                                     "hw_decode", "tonemap") if k in body}
        return {"id": uuid.uuid4().hex[:8], "path": rel_path(src), "name": src.name,
                "duration": float(body.get("duration") or 0),
                "label": str(body.get("label") or "")[:200],
                "start": max(0.0, float(body.get("start") or 0)), "opts": opts}

    def _load_queue(self) -> list:
        try:
            q = json.loads(QUEUE_FILE.read_text())
            return [i for i in q if isinstance(i, dict) and "path" in i and "id" in i]
        except (OSError, ValueError):
            return []

    def _save_queue(self):
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            tmp = QUEUE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.queue))
            tmp.replace(QUEUE_FILE)
        except OSError:
            pass

    # ---- command building -------------------------------------------------------
    @staticmethod
    def build_command(src: Path, opts: dict, workdir: Path, text_sub: Path = None,
                      canvas=(1920, 1080), ts_offset: float = 0.0, progress_fd: int = None) -> list:
        audio_idx = int(opts.get("audio", 0) or 0)
        sub = opts.get("subtitle")             # None | {"type": "internal"|"external", ...}
        start = max(0.0, float(opts.get("start", 0) or 0))
        bitrate = validate_bitrate(opts)
        prof = ENCODER
        va = prof in ("vaapi", "vaapi-amd")
        nv = prof == "nvenc"
        hw_decode = prof != "x264" and bool(opts.get("hw_decode", True))
        W, H = canvas

        # Symlink the input into a temp dir so the subtitles filter never
        # has to deal with escaping odd characters in file names.
        link = workdir / ("input" + src.suffix.lower())
        os.symlink(src, link)

        cmd = [FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "warning", "-nostats"]
        if progress_fd is not None:
            cmd += ["-progress", f"pipe:{progress_fd}"]
        if va:
            cmd += ["-init_hw_device", f"vaapi=va:{VAAPI_DEVICE}", "-filter_hw_device", "va"]
            if hw_decode:
                cmd += ["-hwaccel", "vaapi", "-hwaccel_device", "va",
                        "-hwaccel_output_format", "vaapi"]
        elif nv:
            cmd += ["-init_hw_device", f"cuda=cu:{CUDA_DEVICE}", "-filter_hw_device", "cu"]
            if hw_decode:
                cmd += ["-hwaccel", "cuda", "-hwaccel_device", "cu",
                        "-hwaccel_output_format", "cuda"]
        cmd += ["-re"]
        lead = float(opts.get("_audio_lead") or 0)
        if lead > 0.2:
            # Read the audio-less stretch at full speed: ffmpeg can't start the output
            # until the first real audio frame arrives, which at real-time pace would
            # stall the stream for `lead` seconds.
            cmd += ["-readrate_initial_burst", f"{lead + 1:.1f}"]
        if start > 0:
            # copyts keeps original timestamps so burned-in subs line up after seeking
            cmd += ["-ss", f"{start:.3f}", "-copyts"]
        cmd += ["-i", str(link)]

        tonemap = bool(opts.get("tonemap"))
        fit = f"w={W}:h={H}:force_original_aspect_ratio=decrease:force_divisible_by=2"

        # 1) Base picture: tone-map + fit inside the output frame, in 8-bit.
        #    base:  filters producing the fitted picture
        #    to_sw: GPU frames -> CPU frames, for the software subtitle filters
        #    up:    back to the GPU after subtitles
        #    pad:   letterbox to the fixed frame and leave frames ready for the encoder
        cpu_tm = "tonemapx=tonemap=bt2390:t=bt709:m=bt709:p=bt709:format=nv12"
        if prof == "vaapi":
            # Intel: everything heavy stays on the GPU. With CPU decoding we upload first.
            parts = [] if hw_decode else ["format=pix_fmts=nv12|p010le", "hwupload"]
            if tonemap:   # GPU (VPP) HDR10 -> SDR BT.709
                parts.append("tonemap_vaapi=format=nv12:p=bt709:t=bt709:m=bt709")
            parts.append(f"scale_vaapi={fit}:format=nv12")
            base = ",".join(parts)
            to_sw = ",hwdownload,format=nv12"
            up = ",format=nv12,hwupload"
            pad = f",pad_vaapi=w={W}:h={H}:x=(ow-iw)/2:y=(oh-ih)/2,setsar=1"
        elif prof == "vaapi-amd":
            # AMD (Mesa): decode + scale on the GPU, then work on the small frame on
            # the CPU (Mesa has no VA-API tone-mapping); the GPU encodes.
            if hw_decode:
                # keep 10-bit through the download when it still has to be tone-mapped
                gpu_fmt, cpu_fmt = ("p010", "p010le") if tonemap else ("nv12", "nv12")
                parts = [f"scale_vaapi={fit}:format={gpu_fmt}", "hwdownload", f"format={cpu_fmt}"]
            else:
                parts = [f"scale={fit}"]
            if tonemap:
                parts.append(cpu_tm)
            base = ",".join(parts)
            to_sw = up = ""
            pad = f",pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=nv12,hwupload"
        elif nv:
            # NVIDIA: everything on the GPU with the CUDA filters.
            parts = [] if hw_decode else ["format=pix_fmts=nv12|p010le", "hwupload_cuda"]
            if tonemap:
                parts.append("tonemap_cuda=format=nv12:transfer=bt709:matrix=bt709:primaries=bt709")
            parts.append(f"scale_cuda={fit}:format=nv12")
            base = ",".join(parts)
            to_sw = ",hwdownload,format=nv12"
            up = ",format=nv12,hwupload_cuda"
            pad = f",pad_cuda=w={W}:h={H}:x=(ow-iw)/2:y=(oh-ih)/2,setsar=1"
        else:
            parts = []
            if tonemap:   # CPU tone-mapping (x264 mode only)
                parts.append("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
                             "tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p")
            parts.append(f"scale={fit}")
            base = ",".join(parts)
            to_sw = up = ""
            pad = f",pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p"

        # 2) Subtitles burned in at the fitted size, then letterbox to the frame.
        if not sub:
            vchain = f"[0:v:0]{base}{pad}"
        elif sub.get("type") == "external" or sub.get("kind") != "image":
            if sub.get("type") == "external":
                sub_src = safe_path(rel_path(src.parent / sub["file"]))
                if sub_src.parent != src.parent or not sub_src.is_file():
                    raise ValueError("Subtitle file not found")
                sub_link = workdir / ("sub" + sub_src.suffix.lower())
                os.symlink(sub_src, sub_link)
                sub_filter = f"subtitles=filename={sub_link.as_posix()}"
            else:
                if text_sub is None:
                    raise ValueError("Subtitle track not extracted")
                sub_filter = f"subtitles=filename={text_sub.as_posix()}"
                fonts = text_sub.parent / "fonts"
                if fonts.is_dir() and any(fonts.iterdir()):
                    sub_filter += f":fontsdir={fonts.as_posix()}"
            vchain = f"[0:v:0]{base}{to_sw},{sub_filter}{up}{pad}"
        else:
            # Image subs (PGS / VobSub / DVB): stretch the subtitle canvas to the
            # fitted picture (the canvas is often taller than cropped video), then overlay.
            vchain = (f"[0:v:0]{base}{to_sw}[base];"
                      f"[0:s:{int(sub['index'])}][base]scale2ref=w=rw:h=rh:flags=fast_bilinear[sub][base2];"
                      f"[base2][sub]overlay=eof_action=pass{up}{pad}")

        # After a seek (-copyts keeps the file's own timestamps), shift video and audio
        # by the SAME amount so they stay in sync even when one track starts later.
        shift = f"PTS-{start:.3f}/TB"
        if start > 0:
            vchain += f",setpts={shift}"
        vchain += "[v]"
        # Audio gets its own filter graph. Sharing one graph with the VAAPI video
        # made the GPU decoder fail on some files ("Failed to upload decode
        # parameters: invalid parameter", seen with an av1_vaapi-encoded movie).
        cmd += ["-filter_complex", vchain, "-map", "[v]"]
        pre = f"asetpts={shift}," if start > 0 else ""
        if lead > 0.2:
            # The audio track starts `lead` seconds after the picture (some files' audio
            # begins well after the video). Emit that much silence first so the stream
            # carries audio from its very first packet; the relay can't set up the
            # RTMP stream without it, and waiting for the real audio would stall.
            fmt = "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo"
            cmd += ["-filter_complex",
                    f"aevalsrc=0|0:c=stereo:s=48000:d={lead:.3f},{fmt}[sil];"
                    # concat appends without rebasing, so the real audio must start at 0
                    f"[0:a:{audio_idx}]{pre}aresample=async=1,asetpts=PTS-STARTPTS,{fmt}[real];"
                    f"[sil][real]concat=n=2:v=0:a=1[a]", "-map", "[a]"]
        else:
            # first_pts=0 also covers small gaps at the start.
            cmd += ["-map", f"0:a:{audio_idx}", "-af", f"{pre}aresample=async=1:first_pts=0"]
        if va:
            cmd += ["-c:v", "h264_vaapi", "-profile:v", "high"]
        elif nv:
            cmd += ["-c:v", "h264_nvenc", "-preset", "p5", "-profile:v", "high"]
        else:
            cmd += ["-c:v", "libx264", "-preset", "veryfast", "-tune", "film", "-pix_fmt", "yuv420p"]
        cmd += ["-b:v", bitrate, "-maxrate", bitrate, "-bufsize", _double_rate(bitrate), "-g", "60",
                "-c:a", "aac", "-b:a", AUDIO_BITRATE, "-ac", "2", "-ar", "48000",
                "-max_muxing_queue_size", "4096", "-max_interleave_delta", "0",
                "-f", "mpegts", "-output_ts_offset", f"{ts_offset:.3f}", "pipe:1"]
        return cmd

    # ---- public controls ----------------------------------------------------------
    def play(self, item: dict):
        with self.lock:
            self.log.clear()
            self.reconnects.clear()
            self._begin(item, item.get("start", 0))

    def enqueue(self, item: dict):
        with self.lock:
            self.queue.append(item)
            self._save_queue()
        _prefetch_item_subs(item)

    def remove(self, iid: str):
        with self.lock:
            self.queue = [i for i in self.queue if i["id"] != iid]
            self._save_queue()

    def move(self, iid: str, d: int):
        with self.lock:
            idx = next((n for n, i in enumerate(self.queue) if i["id"] == iid), None)
            if idx is None:
                raise ValueError("Not in queue")
            new = max(0, min(len(self.queue) - 1, idx + (1 if d > 0 else -1)))
            self.queue.insert(new, self.queue.pop(idx))
            self._save_queue()

    def play_from_queue(self, iid: str):
        with self.lock:
            item = next((i for i in self.queue if i["id"] == iid), None)
            if not item:
                raise ValueError("Not in queue")
            self.queue.remove(item)
            self._save_queue()
            self.play(item)

    def clear(self):
        with self.lock:
            self.queue = []
            self._save_queue()

    def start_queue(self):
        with self.lock:
            if not self.queue:
                raise ValueError("Queue is empty")
            if self.state in ("playing", "preparing"):
                return
            item = self.queue.pop(0)
            self._save_queue()
            self.play(item)

    def skip(self):
        with self.lock:
            if self.queue:
                self.log.append("⏭ Skipping to next")
                self._advance()
            else:
                self.stop()

    def seek(self, pos: float):
        with self.lock:
            if not self.current:
                raise ValueError("Nothing is playing")
            dur = self.current.get("duration") or 0
            pos = max(0.0, min(pos, dur - 3) if dur > 3 else pos)
            self.log.append(f"⏩ Seek to {_fmt(pos)}")
            self.reconnects.clear()
            self._begin(self.current, pos)

    def stop(self):
        with self.lock:
            self._kill_segment()
            if self.current:
                self.log.append("■ Stopped")
            self.current = None
            self.state = "idle"
            self.error = None
            self.progress = {}
            self._stop_relay()

    # ---- internals ----------------------------------------------------------------
    def _begin(self, item: dict, pos: float):
        self._kill_segment()
        gen = self.seg_gen
        if item is not self.current:
            self.log.append(f"▶ {item['name']}")
        self.current = item
        self.seg_start = float(pos or 0)
        self.progress = {}
        self.error = None
        self.state = "preparing"
        self.preparing_msg = ("Preparing subtitles" if needs_extract(item["opts"].get("subtitle"))
                              else "Starting")
        threading.Thread(target=self._run_segment, args=(gen, item, self.seg_start),
                         daemon=True).start()

    def _kill_segment(self):
        self.seg_gen += 1
        pp, self.prep_proc = self.prep_proc, None
        if pp and pp.poll() is None:
            pp.kill()
        seg, self.seg = self.seg, None
        if seg and seg.poll() is None:
            seg.kill()

    def _advance(self):
        if self.queue:
            item = self.queue.pop(0)
            self._save_queue()
            self.reconnects.clear()
            self._begin(item, item.get("start", 0))
        else:
            self.log.append("Queue finished")
            self._kill_segment()
            self.current = None
            self.state = "idle"
            self.progress = {}
            self._stop_relay()

    def _fail(self, msg: str):
        self.log.append(msg)
        self.error = msg
        self._kill_segment()
        self.state = "failed"
        self._stop_relay()

    def _ensure_relay(self):
        if self.relay and self.relay.poll() is None:
            return self.relay
        url = output_url()
        if not re.match(r"^rtmps?://[^\s/]+", url) or "YOUR_VPS" in url:
            raise ValueError("Set the server and stream key in Stream settings first")
        self.log.append("Connecting to " + _mask_url(url))
        r = subprocess.Popen(relay_command(url), stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                             stderr=subprocess.PIPE, bufsize=0, start_new_session=True)
        self.relay = r
        self.relay_t0 = time.monotonic()
        self.ts_head = 0.0
        threading.Thread(target=self._watch_relay, args=(r,), daemon=True).start()
        return r

    def _stop_relay(self):
        r, self.relay = self.relay, None
        if r and r.poll() is None:
            try:
                r.stdin.close()          # EOF -> relay finishes the FLV stream cleanly
            except OSError:
                pass

            def reap():
                try:
                    r.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    r.kill()
            threading.Thread(target=reap, daemon=True).start()

    def _watch_relay(self, r):
        for raw in r.stderr:
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                self.log.append("[server] " + line)
        code = r.wait()
        with self.lock:
            if r is not self.relay:      # stopped on purpose
                return
            self.relay = None
            if not self.current or self.state == "failed":
                return
            now = time.monotonic()
            self.reconnects.append(now)
            if sum(1 for t in self.reconnects if now - t < 180) > 5:
                self._fail(f"Lost the connection to the server {len(self.reconnects)} times; giving up. "
                           "Check the server address/stream key and that the VPS is up.")
                return
            item = self.current
            pos = max(0.0, self._position() - 2)
            self.log.append(f"Stream to the server stopped (exit {code}); "
                            f"restarting in 3s at {_fmt(pos)}")
            self._kill_segment()
            gen = self.seg_gen
            self.state = "preparing"
            self.preparing_msg = "Reconnecting"
        time.sleep(3)
        with self.lock:
            if gen == self.seg_gen and self.current is item:
                self._begin(item, pos)

    def _run_segment(self, gen: int, item: dict, pos: float):
        tmpdir = None
        try:
            src = safe_path(item["path"])
            opts = item["opts"]
            sub = opts.get("subtitle")
            text_sub = None
            if needs_extract(sub):
                t = time.time()
                self.log.append("Getting subtitle track ready (slow only the first time per file)...")
                text_sub = cached_text_sub(src, int(sub["index"]), str(sub.get("codec", "")),
                                           on_proc=lambda p, k: setattr(self, "prep_proc", p))
                self.log.append(f"Subtitles ready ({time.time() - t:.1f}s)")
            canvas = canvas_size(opts, src)
            lead = audio_lead(src, int(opts.get("audio", 0) or 0), pos)
            if lead > 0.2:
                self.log.append(f"Audio starts {lead:.1f}s after the picture here; padding with silence")
            tmpdir = Path(tempfile.mkdtemp(prefix="moviestream-"))
            with self.lock:
                if gen != self.seg_gen:
                    return
                self.prep_proc = None
                relay = self._ensure_relay()
                # Continue from the relay's clock, but never before what was already sent
                # (a segment can run ahead of the clock), so timestamps only move forward.
                offset = max(time.monotonic() - self.relay_t0 + 1.0, self.ts_head + 0.5)
                pr, pw = os.pipe()
                try:
                    cmd = self.build_command(src, dict(opts, start=pos, _audio_lead=lead), tmpdir,
                                             text_sub, canvas, offset, pw)
                    seg = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, pass_fds=(pw,),
                                           start_new_session=True)
                except Exception:
                    os.close(pr)
                    raise
                finally:
                    os.close(pw)
                self.log.append("$ " + " ".join(_quote(c) for c in cmd))
                self.seg = seg
                self.seg_ts_offset = offset
                self.state = "playing"
                self.preparing_msg = None
            threading.Thread(target=self._read_progress, args=(gen, pr), daemon=True).start()
            threading.Thread(target=self._read_log, args=(gen, seg, item), daemon=True).start()
            self._pump(gen, seg, relay)
            code = seg.wait()
        except Exception as e:  # noqa: BLE001
            with self.lock:
                if gen == self.seg_gen:
                    self._fail(f"Error: {e}")
            return
        finally:
            if tmpdir:
                shutil.rmtree(tmpdir, ignore_errors=True)

        with self.lock:
            if gen != self.seg_gen:          # superseded: seek / skip / stop / reconnect
                return
            self.seg = None
            if code == 0:
                self.log.append(f"Finished {item['name']}")
                self._advance()
            elif self.relay and self.relay.poll() is None:
                msg = f"ffmpeg failed on {item['name']} (exit {code})"
                if self.queue:
                    self.log.append(msg + "; skipping to the next movie")
                    self._advance()
                else:
                    self._fail(msg)
            # otherwise the relay died and _watch_relay handles the reconnect

    def _pump(self, gen: int, seg, relay):
        """Copy whole TS packets from the segment encoder into the relay."""
        fd = seg.stdout.fileno()
        rem = b""
        while True:
            try:
                data = os.read(fd, 1 << 16)
            except OSError:
                break
            if not data:
                break
            data = rem + data
            n = len(data) - len(data) % TS_PACKET
            rem = data[n:]
            if not n:
                continue
            with self.wlock:
                if gen != self.seg_gen:
                    break
                try:
                    mv = memoryview(data)[:n]
                    while mv:
                        mv = mv[relay.stdin.write(mv):]
                except (OSError, ValueError):
                    break                     # relay gone; _watch_relay deals with it

    def _read_progress(self, gen: int, fd: int):
        cur = {}
        with os.fdopen(fd, "r", errors="replace") as f:
            for line in f:
                k, _, v = line.strip().partition("=")
                cur[k] = v
                if k == "progress":
                    if gen == self.seg_gen:
                        t = _out_time(cur)
                        if t is not None:
                            self.progress = dict(cur)
                            self.ts_head = max(self.ts_head, self.seg_ts_offset + t)
                    cur = {}

    HW_ERR = ("HW accel", "Failed to upload decode parameters", "Failed to end picture decode",
              "CUDA_ERROR", "cuvid", "Failed setup for format")

    def _read_log(self, gen: int, seg, item: dict):
        hw_errors = 0
        for raw in seg.stderr:
            line = raw.decode("utf-8", "replace").rstrip()
            if not line or "Could not find codec parameters" in line or "analyzeduration" in line:
                continue
            if any(m in line for m in self.HW_ERR):
                hw_errors += 1
                if hw_errors == 1:
                    self.log.append(line)
                if hw_errors == 12:
                    self._fallback_to_cpu(gen, item)
                continue
            self.log.append(line)

    def _fallback_to_cpu(self, gen: int, item: dict):
        """The GPU can't decode this file: restart it at the same spot with CPU
        decoding (tone-mapping, scaling and encoding stay on the GPU)."""
        with self.lock:
            if gen != self.seg_gen or self.current is not item or \
                    item["opts"].get("hw_decode") is False:
                return
            pos = self._position()
            self.log.append(f"GPU can't decode {item['name']}; switching to CPU decoding "
                            f"and resuming at {_fmt(pos)}")
            item["opts"]["hw_decode"] = False
            self._begin(item, pos)

    def _position(self) -> float:
        # out_time counts from 0 for each segment (it does not include -output_ts_offset)
        return self.seg_start + max(0.0, _out_time(self.progress) or 0.0)

    def status(self) -> dict:
        with self.lock:
            pos = None
            if self.current:
                pos = self._position() if self.state == "playing" else self.seg_start
            return {
                "state": self.state,
                "preparing": self.preparing_msg if self.state == "preparing" else None,
                "current": self.current,
                "position": pos,
                "speed": self.progress.get("speed") if self.state == "playing" else None,
                "fps": self.progress.get("fps") if self.state == "playing" else None,
                "connected": bool(self.relay and self.relay.poll() is None),
                "queue": self.queue,
                "error": self.error,
                "log": list(self.log)[-50:],
            }


def _out_time(progress: dict):
    """Seconds encoded so far from an ffmpeg -progress block, or None when it's not a
    real value (ffmpeg reports N/A, or INT64_MAX in its final block at end of file)."""
    try:
        us = int(progress.get("out_time_us") or 0)
    except ValueError:
        return None
    return us / 1_000_000 if 0 <= us < 10 ** 12 else None   # < ~11.6 days


def _prefetch_item_subs(item: dict):
    sub = item["opts"].get("subtitle")
    if not needs_extract(sub):
        return

    def run():
        try:
            cached_text_sub(safe_path(item["path"]), int(sub["index"]), str(sub.get("codec", "")))
        except Exception:  # noqa: BLE001
            pass
    threading.Thread(target=run, daemon=True).start()


def _double_rate(rate: str) -> str:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([kKmM]?)", rate)
    return f"{float(m.group(1)) * 2:g}{m.group(2)}"


def _quote(s: str) -> str:
    return s if re.fullmatch(r"[\w@%+=:,./\-\[\]()]+", s) else "'" + s.replace("'", "'\\''") + "'"


streamer = Streamer()


def _api(fn):
    try:
        res = fn()
    except (ValueError, KeyError, OSError) as e:
        return jsonify(error=str(e)), 400
    return jsonify(res if res is not None else {"ok": True})


def _body() -> dict:
    return request.get_json(force=True, silent=True) or {}


@app.post("/api/play")
@app.post("/api/start")
def api_play():
    return _api(lambda: streamer.play(Streamer.make_item(_body())))


@app.post("/api/queue")
def api_enqueue():
    return _api(lambda: streamer.enqueue(Streamer.make_item(_body())))


@app.post("/api/queue/start")
def api_queue_start():
    return _api(streamer.start_queue)


@app.post("/api/queue/clear")
def api_queue_clear():
    return _api(streamer.clear)


@app.delete("/api/queue/<iid>")
def api_queue_remove(iid):
    return _api(lambda: streamer.remove(iid))


@app.post("/api/queue/<iid>/move")
def api_queue_move(iid):
    return _api(lambda: streamer.move(iid, int(_body().get("dir", 1))))


@app.post("/api/queue/<iid>/play")
def api_queue_play(iid):
    return _api(lambda: streamer.play_from_queue(iid))


@app.post("/api/skip")
def api_skip():
    return _api(streamer.skip)


@app.post("/api/seek")
def api_seek():
    return _api(lambda: streamer.seek(float(_body().get("position", 0))))


@app.post("/api/stop")
def api_stop():
    return _api(streamer.stop)


@app.get("/api/status")
def api_status():
    return jsonify(streamer.status())


# --------------------------------------------------------------------------
# Stream destination (OBS-style server + stream key), saved on disk
# --------------------------------------------------------------------------
SETTINGS_FILE = STATE_DIR / "settings.json"
_settings_lock = threading.Lock()


def load_settings() -> dict:
    server, _, key = RTMP_URL.rstrip("/").rpartition("/")
    s = {"server": server or RTMP_URL, "key": key if server else ""}
    try:
        s.update({k: v for k, v in json.loads(SETTINGS_FILE.read_text()).items()
                  if k in ("server", "key") and isinstance(v, str)})
    except (OSError, ValueError):
        pass
    return s


def output_url(s: dict = None) -> str:
    s = s or load_settings()
    return s["server"].rstrip("/") + ("/" + s["key"] if s["key"] else "")


@app.get("/api/settings")
def get_settings():
    s = load_settings()
    return jsonify(server=s["server"], key=s["key"], url=output_url(s))


@app.post("/api/settings")
def save_settings():
    body = request.get_json(force=True, silent=True) or {}
    server = str(body.get("server", "")).strip()
    key = str(body.get("key", "")).strip().strip("/")
    if not re.match(r"^rtmps?://[^\s/]+", server):
        return jsonify(error="Server must look like rtmp://host[:port]/app"), 400
    if re.search(r"\s", key):
        return jsonify(error="Stream key can't contain spaces"), 400
    s = {"server": server.rstrip("/"), "key": key}
    with _settings_lock:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(s))
        os.chmod(tmp, 0o600)
        tmp.replace(SETTINGS_FILE)
    return jsonify(server=s["server"], key=s["key"], url=output_url(s))


@app.get("/api/config")
def api_config():
    return jsonify(encoder=ENCODER, bitrate=DEFAULT_BITRATE, max_height=DEFAULT_MAX_HEIGHT)


@app.get("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


if __name__ == "__main__":
    prune_sub_cache()
    try:
        from waitress import serve
        print(f"Serving on http://{HOST}:{PORT}  (media: {MEDIA_ROOT}, output: {RTMP_URL})")
        serve(app, host=HOST, port=PORT, threads=8)
    except ImportError:
        app.run(host=HOST, port=PORT, threaded=True)
