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
    fade_dur  = float(data.get("fade_dur", 0.5))  # reserved for future use
    width     = int(data.get("width", 720))
    height    = int(data.get("height", 1280))

    if not images:
        return jsonify(error="images list is empty"), 400
    if len(durations) != len(images):
        durations = [4.0] * len(images)
    durations = [float(d) for d in durations]

    with tempfile.TemporaryDirectory() as tmp:

        # ── 1. Download + cover-crop each image to target size ────
        frame_paths = []
        for i, url in enumerate(images):
            resp = requests.get(url, timeout=30, allow_redirects=True)
            if resp.status_code != 200:
                return jsonify(error=f"Image {i+1} download failed: HTTP {resp.status_code}"), 502

            img = Image.open(BytesIO(resp.content)).convert("RGB")
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
            img.close()  # free RAM immediately

        # ── 2. Encode each frame to a short clip (one at a time) ──
        # Processing clips sequentially keeps peak RAM low on free tier.
        clip_paths = []
        for i, (frame, dur) in enumerate(zip(frame_paths, durations)):
            clip = os.path.join(tmp, f"clip_{i:03d}.mp4")
            cmd = [
                "ffmpeg", "-y",
                "-loop", "1", "-t", str(dur), "-i", frame,
                "-vf", "setsar=1,fps=24",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26",
                "-pix_fmt", "yuv420p",
                clip,
            ]
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode != 0:
                return jsonify(error=f"FFmpeg clip {i+1} failed", stderr=r.stderr[-500:]), 500
            clip_paths.append(clip)

        # ── 3. Concat all clips into final MP4 ───────────────────
        concat_list = os.path.join(tmp, "concat.txt")
        with open(concat_list, "w") as f:
            for clip in clip_paths:
                f.write(f"file '{clip}'\n")

        output = os.path.join(tmp, "reel.mp4")
        cmd = [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0", "-i", concat_list,
            "-c", "copy",
            output,
        ]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            return jsonify(error="FFmpeg concat failed", stderr=r.stderr[-500:]), 500

        return send_file(output, mimetype="video/mp4", as_attachment=True,
                         download_name="reel.mp4")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
