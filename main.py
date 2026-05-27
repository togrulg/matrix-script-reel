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

        # ── 1. Download + cover-crop each image to target size ────
        # Google Drive "export=view" URLs redirect through a consent page;
        # rewrite them to "export=download" for a direct binary response.
        def _normalise_url(u):
            if "drive.google.com/uc" in u:
                return u.replace("export=view", "export=download")
            # drive.google.com/file/d/<ID>/view → direct download
            import re
            m = re.search(r"/file/d/([^/]+)", u)
            if m:
                return f"https://drive.google.com/uc?export=download&id={m.group(1)}"
            return u

        _headers = {"User-Agent": "Mozilla/5.0"}

        frame_paths = []
        for i, url in enumerate(images):
            dl_url = _normalise_url(url)
            resp = requests.get(dl_url, timeout=30, allow_redirects=True, headers=_headers)
            if resp.status_code != 200:
                return jsonify(error=f"Image {i+1} download failed: HTTP {resp.status_code} url={dl_url}"), 502

            # Google sometimes returns an HTML virus-scan warning for large files;
            # detect it and follow the confirm link.
            if resp.headers.get("Content-Type", "").startswith("text/html"):
                import re as _re
                m = _re.search(r'href="(/uc\?[^"]*confirm=[^"]+)"', resp.text)
                if m:
                    confirm_url = "https://drive.google.com" + m.group(1).replace("&amp;", "&")
                    resp = requests.get(confirm_url, timeout=30, allow_redirects=True, headers=_headers)

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
            img.close()

        # ── 2. Encode each frame to a clip ────────────────────────
        clip_paths = []
        for i, (frame, dur) in enumerate(zip(frame_paths, durations)):
            clip = os.path.join(tmp, f"clip_{i:03d}.mp4")

            if transition == "fade":
                # Fade to black at end; fade from black at start (except first/last clips
                # get only one fade so the very beginning/end isn't double-faded).
                fade_in  = 0 if i == 0 else fade_dur
                fade_out = 0 if i == len(images) - 1 else fade_dur
                vf_parts = ["setsar=1", "fps=24"]
                if fade_in  > 0:
                    vf_parts.append(f"fade=t=in:st=0:d={fade_in}")
                if fade_out > 0:
                    vf_parts.append(f"fade=t=out:st={dur - fade_out:.3f}:d={fade_out}")
                vf = ",".join(vf_parts)
            else:
                # cut or xfade both use plain clips; xfade is applied at concat time
                vf = "setsar=1,fps=24"

            cmd = [
                "ffmpeg", "-y",
                "-loop", "1", "-t", str(dur), "-i", frame,
                "-vf", vf,
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26",
                "-pix_fmt", "yuv420p",
                clip,
            ]
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode != 0:
                return jsonify(error=f"FFmpeg clip {i+1} failed", stderr=r.stderr[-500:]), 500
            clip_paths.append(clip)

        output = os.path.join(tmp, "reel.mp4")

        if transition == "xfade":
            # ── 3a. xfade: build a single filtergraph chaining all clips ──
            r = _concat_xfade(clip_paths, durations, fade_dur, output)
        else:
            # ── 3b. cut / fade: simple concat demuxer ─────────────────────
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
            r = subprocess.run(cmd, capture_output=True, text=True)

        if r.returncode != 0:
            return jsonify(error="FFmpeg concat failed", stderr=r.stderr[-500:]), 500

        return send_file(output, mimetype="video/mp4", as_attachment=True,
                         download_name="reel.mp4")


def _concat_xfade(clip_paths, durations, fade_dur, output):
    """Chains N clips with xfade=dissolve between each pair."""
    n = len(clip_paths)
    if n == 1:
        # Nothing to dissolve — just copy the single clip
        import shutil
        shutil.copy(clip_paths[0], output)
        class _R:
            returncode = 0
            stderr = ""
        return _R()

    # Build ffmpeg inputs + filtergraph
    inputs = []
    for p in clip_paths:
        inputs += ["-i", p]

    # Each xfade offset = sum of preceding clip durations minus accumulated fade overlap
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
    return subprocess.run(cmd, capture_output=True, text=True)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
