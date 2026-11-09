"""Bisect which part of the app's segment command triggers VAAPI decode errors."""
import pathlib, re, subprocess, sys, tempfile
sys.path.insert(0, "/opt/movie-streamer")
import app as A

src = A.safe_path(sys.argv[1])
start = float(sys.argv[2]) if len(sys.argv) > 2 else 0


def run(name, cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    hw = sum(1 for l in p.stderr.splitlines() if "HW accel" in l)
    print(f"{name:40s} hw_errors={hw}")


wd = pathlib.Path(tempfile.mkdtemp())
base = A.Streamer.build_command(src, {"hw_decode": True, "start": start}, wd, None,
                                A.canvas_size({}, src), 5.0)
base.remove("-re")
base[1:1] = ["-y"]
base[-1:-1] = ["-t", "10"]
base[-1] = "/dev/null"
print("CMD:", " ".join(A._quote(c) for c in base))
fc = base.index("-filter_complex") + 1


def variant(**kw):
    c = list(base)
    if "graph" in kw:
        c[fc] = kw["graph"]
    for opt in kw.get("drop", []):
        i = c.index(opt)
        del c[i:i + 2]
    return c


g = base[fc]
run("as built", base)
run("no pad_vaapi/setsar", variant(graph=re.sub(r",pad_vaapi=[^,\[]*,setsar=1", "", g)))
run("no force_original_aspect_ratio", variant(graph=g.replace(":force_original_aspect_ratio=decrease:force_divisible_by=2", "")))
run("no aresample", variant(graph=g.replace("aresample=async=1", "anull")))
run("no -output_ts_offset", variant(drop=["-output_ts_offset"]))
run("no -max_muxing_queue_size", variant(drop=["-max_muxing_queue_size"]))
run("no -bufsize/-maxrate", variant(drop=["-bufsize", "-maxrate"]))
run("-f null instead of mpegts", [x if x != "mpegts" else "null" for x in base])
