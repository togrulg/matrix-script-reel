import os
import subprocess
import tempfile
import requests
from PIL import Image
from io import BytesIO
from flask import Flask, request, send_file, jsonify

app = Flask(__name__)


@app.route("/health")
def health():
    return "ok"


@app.route("/render-reel", methods=["POST"])
def render_reel():
    data = request.get_json(force=True)

    images    = data.get("images", [])
    durations = data.get("durations", [])
    fade_dur  = float(data.get("fade_dur", 0.5))
    width     = int(data.get("width", 720))
    height    = int(data.get("height", 1280))

    if not images:
        return jsonify(error="images list is empty"), 400
    if len(durations) != len(images):
        durations = [4.0] * len(images)

    durations = [float(d) for d in durations]
    n = len(images)

    with tempfile.TemporaryDirectory() as tmp:

        # ── Download frames (resize to target dims to save RAM) ──
        frame_paths = []
        for i, url in enumerate(images):
            resp = requests.get(url, timeout=30, allow_redirects=True)
            if resp.status_code != 200:
                return jsonify(error=f"Failed to download image {i+1}: HTTP {resp.status_code}"), 502
            # Resize to target dimensions with Pillow before handing to FFmpeg
            img = Image.open(BytesIO(resp.content)).convert("RGB")
            # Cover-scale: fill canvas without black bars
            src_ratio  = img.width / img.height
            tgt_ratio  = width / height
            if src_ratio > tgt_ratio:
                new_h = height
                new_w = int(img.width * height / img.height)
            else:
                new_w = width
                new_h = int(img.height * width / img.width)
            img = img.resize((new_w, new_h), Image.LANCZOS)
            left = (new_w - width)  // 2
            top  = (new_h - height) // 2
            img  = img.crop((left, top, left + width, top + height))
            path = os.path.join(tmp, f"frame_{i:03d}.png")
            img.save(path, "PNG")
            frame_paths.append(path)

        output = os.path.join(tmp, "reel.mp4")

        # ── Build FFmpeg command ─────────────────────────────
        if n == 1:
            cmd = _ffmpeg_single(frame_paths[0], durations[0], width, height, output)
        else:
            cmd = _ffmpeg_xfade(frame_paths, durations, fade_dur, width, height, output)

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            return jsonify(error="FFmpeg failed", stderr=result.stderr[-1000:]), 500

        return send_file(output, mimetype="video/mp4", as_attachment=True,
                         download_name="reel.mp4")


def _scale_filter(width, height):
    # Images are already cover-cropped by Pillow; just set SAR and fps
    return f"setsar=1,fps=30"


def _ffmpeg_single(path, duration, width, height, output):
    return [
        "ffmpeg", "-y",
        "-loop", "1", "-t", str(duration), "-i", path,
        "-vf", _scale_filter(width, height),
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-pix_fmt", "yuv420p",
        output,
    ]


def _ffmpeg_xfade(paths, durations, fade_dur, width, height, output):
    """Build an ffmpeg command that crossfades N images using the xfade filter."""
    n = len(paths)
    sf = _scale_filter(width, height)

    # Input args: each image loops for its full duration
    inputs = []
    for i, (path, dur) in enumerate(zip(paths, durations)):
        inputs += ["-loop", "1", "-t", str(dur), "-i", path]

    # Filter graph
    filters = []

    # Scale each input
    for i in range(n):
        filters.append(f"[{i}:v]{sf}[v{i}]")

    # Chain xfade transitions
    # offset for xfade i = sum(durations[0..i]) - (i+1)*fade_dur
    cumulative = 0.0
    prev_label = "v0"
    for i in range(n - 1):
        cumulative += durations[i]
        offset = cumulative - (i + 1) * fade_dur
        next_label = f"x{i+1}" if i < n - 2 else "vout"
        filters.append(
            f"[{prev_label}][v{i+1}]"
            f"xfade=transition=fade:duration={fade_dur}:offset={offset:.3f}"
            f"[{next_label}]"
        )
        prev_label = next_label

    filter_complex = ";".join(filters)

    return [
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", "[vout]",
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-pix_fmt", "yuv420p",
        output,
    ]


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
