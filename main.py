import os
import base64
import subprocess
import tempfile
import traceback
import requests
from PIL import Image
from io import BytesIO
from flask import Flask, request, send_file, jsonify

app = Flask(__name__)


@app.errorhandler(Exception)
def handle_exception(e):
    return jsonify(error=str(e), traceback=traceback.format_exc()[-2000:]), 500


@app.route("/health")
def health():
    return "ok"


@app.route("/render-reel", methods=["POST"])
def render_reel():
    try:
        return _render_reel_impl()
    except Exception as e:
        return jsonify(error=str(e), traceback=traceback.format_exc()[-3000:]), 500


def _render_reel_impl():
    data = request.get_json(force=True)

    images     = data.get("images", [])
    durations  = data.get("durations", [])
    fade_dur   = float(data.get("fade_dur", 0.5))
    transition = data.get("transition", "fade").lower()   # cut | fade | xfade
    width      = int(data.get("width", 720))
    height     = int(data.get("height", 1280))

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
            if url.startswith("data:"):
                _, b64data = url.split(",", 1)
                raw = base64.b64decode(b64data)
            else:
                resp = _sess.get(url, timeout=60, allow_redirects=True)
                if resp.status_code != 200:
                    return jsonify(error=f"Image {i+1} download failed: HTTP {resp.status_code} from {url[:120]}"), 502
                ct = resp.headers.get("Content-Type", "")
                if "text/html" in ct:
                    return jsonify(error=f"Image {i+1}: got HTML instead of image from {url[:120]}"), 502
                raw = resp.content

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

        # ── 2. Encode each frame to a clip ────────────────────────
        clip_paths = []
        for i, (frame, dur) in enumerate(zip(frame_paths, durations)):
            clip = os.path.join(tmp, f"clip_{i:03d}.mp4")

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
                "ffmpeg", "-y",
                "-loop", "1", "-t", str(dur), "-i", frame,
                "-vf", vf,
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26",
                "-pix_fmt", "yuv420p",
                clip,
            ]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if r.returncode != 0:
                return jsonify(error=f"FFmpeg clip {i+1} failed", stderr=r.stderr[-500:]), 500
            clip_paths.append(clip)

        output = os.path.join(tmp, "reel.mp4")

        if transition == "xfade":
            r = _concat_xfade(clip_paths, durations, fade_dur, output)
        else:
            concat_list = os.path.join(tmp, "concat.txt")
            with open(concat_list, "w") as f:
                for clip in clip_paths:
                    f.write(f"file '{clip}'\n")
            cmd = [
                "ffmpeg", "-y",
                "-f", "concat", "-safe", "0", "-i", concat_list,
                "-c", "copy",
                output,
            ]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

        if r.returncode != 0:
            return jsonify(error="FFmpeg concat failed", stderr=r.stderr[-500:]), 500

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
        ["ffmpeg", "-y"]
        + inputs
        + ["-filter_complex", ";".join(filters),
           "-map", "[vout]",
           "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26",
           "-pix_fmt", "yuv420p",
           output]
    )
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
