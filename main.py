import os
import base64
import subprocess
import tempfile
import traceback
import time
from collections import deque
import requests
from PIL import Image
from io import BytesIO
from flask import Flask, request, send_file, jsonify

app = Flask(__name__)

# ── In-process request log (last 20 entries) ─────────────────
_LOG = deque(maxlen=20)

def _log(msg):
    entry = f"[{time.strftime('%H:%M:%S')}] {msg}"
    _LOG.append(entry)
    print(entry, flush=True)


@app.errorhandler(Exception)
def handle_exception(e):
    tb = traceback.format_exc()
    _log(f"UNHANDLED: {e}\n{tb[-1000:]}")
    return jsonify(error=str(e), traceback=tb[-3000:]), 500


@app.route("/")
@app.route("/health")
def health():
    return "ok"


@app.route("/logs")
def logs():
    """Returns the last 20 request log lines — useful for debugging 500s."""
    return jsonify(logs=list(_LOG))


@app.route("/render-reel", methods=["POST"])
def render_reel():
    try:
        return _render_reel_impl()
    except Exception as e:
        tb = traceback.format_exc()
        _log(f"ERROR in render_reel: {e}\n{tb[-500:]}")
        return jsonify(error=str(e), traceback=tb[-3000:]), 500


def _render_reel_impl():
    data = request.get_json(force=True)

    images     = data.get("images", [])
    durations  = data.get("durations", [])
    fade_dur   = float(data.get("fade_dur", 0.5))
    transition = data.get("transition", "fade").lower()
    width      = int(data.get("width", 720))
    height     = int(data.get("height", 1280))

    _log(f"START render_reel: {len(images)} images, transition={transition}, {width}x{height}")

    if not images:
        return jsonify(error="images list is empty"), 400
    if len(durations) != len(images):
        durations = [4.0] * len(images)
    durations = [float(d) for d in durations]

    with tempfile.TemporaryDirectory() as tmp:

        # ── 1. Decode / download each image ──────────────────────
        frame_paths = []
        _sess = requests.Session()
        _sess.headers.update({"User-Agent": "Mozilla/5.0"})

        for i, url in enumerate(images):
            _log(f"  Frame {i+1}/{len(images)}: {'data URI' if url.startswith('data:') else url[:80]}")
            if url.startswith("data:"):
                _, b64data = url.split(",", 1)
                raw = base64.b64decode(b64data)
            else:
                resp = _sess.get(url, timeout=60, allow_redirects=True)
                if resp.status_code != 200:
                    msg = f"Image {i+1} download failed: HTTP {resp.status_code} from {url[:120]}"
                    _log(f"  ERROR: {msg}")
                    return jsonify(error=msg), 502
                ct = resp.headers.get("Content-Type", "")
                if "text/html" in ct:
                    msg = f"Image {i+1}: got HTML instead of image (URL expired or needs auth): {url[:120]}"
                    _log(f"  ERROR: {msg}")
                    return jsonify(error=msg), 502
                raw = resp.content

            _log(f"  Frame {i+1}: {len(raw)//1024}KB downloaded, opening with Pillow…")
            img = Image.open(BytesIO(raw)).convert("RGB")
            src_ratio = img.width / img.height
            tgt_ratio = width / height
            if src_ratio > tgt_ratio:
                new_h = height
                new_w = int(img.width * height / img.height)
            else:
                new_w = width
                new_h = int(img.height * width / img.width)
            img  = img.resize((new_w, new_h), Image.LANCZOS)
            left = (new_w - width)  // 2
            top  = (new_h - height) // 2
            img  = img.crop((left, top, left + width, top + height))
            path = os.path.join(tmp, f"frame_{i:03d}.png")
            img.save(path, "PNG")
            frame_paths.append(path)
            img.close()
            _log(f"  Frame {i+1}: saved to {path}")

        # ── 2. Encode each frame to a clip ────────────────────────
        clip_paths = []
        for i, (frame, dur) in enumerate(zip(frame_paths, durations)):
            clip = os.path.join(tmp, f"clip_{i:03d}.mp4")
            _log(f"  Encoding clip {i+1}/{len(frame_paths)} ({dur}s, transition={transition})…")

            if transition == "fade":
                fade_in  = 0 if i == 0 else fade_dur
                fade_out = 0 if i == len(images) - 1 else fade_dur
                vf_parts = ["setsar=1", "fps=24"]
                if fade_in  > 0:
                    vf_parts.append(f"fade=t=in:st=0:d={fade_in}")
                if fade_out > 0:
                    vf_parts.append(f"fade=t=out:st={dur - fade_out:.3f}:d={fade_out}")
                vf = ",".join(vf_parts)
            else:
                vf = "setsar=1,fps=24"

            cmd = [
                "ffmpeg", "-y", "-loglevel", "error",
                "-loop", "1", "-t", str(dur), "-i", frame,
                "-vf", vf,
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26",
                "-pix_fmt", "yuv420p",
                clip,
            ]
            r = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                               stderr=subprocess.PIPE, timeout=120)
            if r.returncode != 0:
                err = r.stderr.decode(errors="replace")[-300:]
                _log(f"  FFmpeg clip {i+1} FAILED: {err}")
                return jsonify(error=f"FFmpeg clip {i+1} failed", stderr=err), 500
            # Free the source PNG immediately after encoding
            try: os.remove(frame)
            except: pass
            clip_paths.append(clip)
            _log(f"  Clip {i+1} done")

        output = os.path.join(tmp, "reel.mp4")

        if transition == "xfade":
            _log("  Concatenating with xfade…")
            r = _concat_xfade(clip_paths, durations, fade_dur, output)
        else:
            _log("  Concatenating clips…")
            concat_list = os.path.join(tmp, "concat.txt")
            with open(concat_list, "w") as f:
                for clip in clip_paths:
                    f.write(f"file '{clip}'\n")
            cmd = [
                "ffmpeg", "-y", "-loglevel", "error",
                "-f", "concat", "-safe", "0", "-i", concat_list,
                "-c", "copy",
                output,
            ]
            r = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                               stderr=subprocess.PIPE, timeout=120)
            r.stderr = r.stderr.decode(errors="replace") if isinstance(r.stderr, bytes) else (r.stderr or "")

        if r.returncode != 0:
            _log(f"  FFmpeg concat FAILED: {r.stderr[-300:]}")
            return jsonify(error="FFmpeg concat failed", stderr=r.stderr[-500:]), 500

        size_kb = os.path.getsize(output) // 1024
        _log(f"  SUCCESS: reel.mp4 {size_kb}KB — sending to client")
        return send_file(output, mimetype="video/mp4", as_attachment=True,
                         download_name="reel.mp4")


def _concat_xfade(clip_paths, durations, fade_dur, output):
    n = len(clip_paths)
    if n == 1:
        import shutil
        shutil.copy(clip_paths[0], output)
        class _R:
            returncode = 0; stderr = ""
        return _R()

    inputs = []
    for p in clip_paths:
        inputs += ["-i", p]

    filters = []
    offset = durations[0] - fade_dur
    prev_label = "[0:v]"
    for i in range(1, n):
        out_label = f"[v{i}]" if i < n - 1 else "[vout]"
        filters.append(
            f"{prev_label}[{i}:v]xfade=transition=dissolve:duration={fade_dur}:offset={offset:.3f}{out_label}"
        )
        offset += durations[i] - fade_dur
        prev_label = out_label

    cmd = (
        ["ffmpeg", "-y", "-loglevel", "error"]
        + inputs
        + ["-filter_complex", ";".join(filters),
           "-map", "[vout]",
           "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26",
           "-pix_fmt", "yuv420p",
           output]
    )
    r = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                       stderr=subprocess.PIPE, timeout=300)
    r.stderr = r.stderr.decode(errors="replace") if isinstance(r.stderr, bytes) else (r.stderr or "")
    return r


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
