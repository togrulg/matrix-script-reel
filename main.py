import os
import base64
import subprocess
import tempfile
import traceback
import time
import threading
import uuid
from collections import deque
import requests
from PIL import Image
from io import BytesIO
from flask import Flask, request, send_file, jsonify

app = Flask(__name__)

# ── In-process request log (last 40 entries) ─────────────────
_LOG = deque(maxlen=40)

# ── Async job store ───────────────────────────────────────────
# { job_id: { status: 'pending'|'processing'|'done'|'error',
#             result_path: str|None, error: str|None, created: float } }
_JOBS = {}
_JOBS_LOCK = threading.Lock()


def _log(msg):
    entry = f"[{time.strftime('%H:%M:%S')}] {msg}"
    _LOG.append(entry)
    print(entry, flush=True)


def _cleanup_old_jobs():
    """Remove jobs older than 10 minutes to free memory/disk."""
    now = time.time()
    with _JOBS_LOCK:
        stale = [k for k, v in _JOBS.items() if now - v['created'] > 600]
        for k in stale:
            path = _JOBS[k].get('result_path')
            if path and os.path.exists(path):
                try: os.remove(path)
                except: pass
            del _JOBS[k]


@app.errorhandler(Exception)
def handle_exception(e):
    tb = traceback.format_exc()
    _log(f"UNHANDLED: {e}\n{tb[-1000:]}")
    return jsonify(error=str(e), traceback=tb[-3000:]), 500


@app.route("/")
@app.route("/health")
def health():
    _cleanup_old_jobs()
    return jsonify(status="ok", jobs=len(_JOBS))


@app.route("/logs")
def logs():
    return jsonify(logs=list(_LOG))


# ── Async render ──────────────────────────────────────────────

@app.route("/render-reel", methods=["POST"])
def render_reel():
    """
    Accepts the render payload and returns immediately (202) with a job_id.
    FFmpeg runs in a background thread — no timeout risk.
    Poll GET /status/<job_id> until 'done' or 'error'.
    Fetch MP4 via GET /result/<job_id>.
    """
    data   = request.get_json(force=True)
    job_id = str(uuid.uuid4())

    with _JOBS_LOCK:
        _JOBS[job_id] = {
            'status'     : 'pending',
            'result_path': None,
            'error'      : None,
            'created'    : time.time(),
        }

    threading.Thread(target=_run_job, args=(job_id, data), daemon=True).start()
    _log(f"Job {job_id[:8]} queued — {len(data.get('images', []))} frames")
    return jsonify(job_id=job_id, status='pending'), 202


@app.route("/status/<job_id>", methods=["GET"])
def job_status(job_id):
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
    if not job:
        return jsonify(error='job not found'), 404
    resp = {'status': job['status']}
    if job.get('error'):
        resp['error'] = job['error']
    return jsonify(resp), 200


@app.route("/result/<job_id>", methods=["GET"])
def job_result(job_id):
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
    if not job:
        return jsonify(error='job not found'), 404
    if job['status'] != 'done':
        return jsonify(error='not ready', status=job['status']), 425
    path = job['result_path']
    if not path or not os.path.exists(path):
        return jsonify(error='result file missing'), 500
    return send_file(path, mimetype='video/mp4', as_attachment=True,
                     download_name='reel.mp4')


def _run_job(job_id, data):
    with _JOBS_LOCK:
        _JOBS[job_id]['status'] = 'processing'
    try:
        path = _render_reel_impl(job_id, data)
        with _JOBS_LOCK:
            _JOBS[job_id].update(status='done', result_path=path)
        _log(f"Job {job_id[:8]} DONE — {os.path.getsize(path)//1024}KB")
    except Exception as e:
        _log(f"Job {job_id[:8]} ERROR: {e}\n{traceback.format_exc()[-400:]}")
        with _JOBS_LOCK:
            _JOBS[job_id].update(status='error', error=str(e))


def _render_reel_impl(job_id, data):
    images       = data.get("images", [])
    durations    = data.get("durations", [])
    fade_dur     = float(data.get("fade_dur", 0.5))
    music_data   = data.get("music")           # base64 data URI (legacy)
    music_url    = data.get("music_url")       # direct download URL (Jamendo)
    music_volume = float(data.get("music_volume", 0.3))
    # xfade loads ALL clips into memory simultaneously — OOM on 512MB free tier.
    # Force 'fade' which processes clips one at a time then stream-copies.
    transition = "fade"
    width      = int(data.get("width",  540))   # 540x960 = 9:16, ~40% less memory than 720x1280
    height     = int(data.get("height", 960))

    if not images:
        raise ValueError("No images provided")
    if len(durations) != len(images):
        durations = [4.0] * len(images)

    # Persistent temp dir — result must survive after function returns
    tmp = tempfile.mkdtemp(prefix=f"reel_{job_id[:8]}_")
    _log(f"Job {job_id[:8]}: {len(images)} frames, {transition}, {width}x{height}")

    sess = requests.Session()
    sess.headers.update({"User-Agent": "Mozilla/5.0"})

    # ── 1. Decode / download frames ───────────────────────────
    frame_paths = []
    for i, url in enumerate(images):
        _log(f"  Frame {i+1}/{len(images)}: {'base64' if url.startswith('data:') else url[:80]}")
        if url.startswith("data:"):
            _, b64data = url.split(",", 1)
            raw = base64.b64decode(b64data)
        else:
            resp = sess.get(url, timeout=60, allow_redirects=True)
            if resp.status_code != 200:
                raise ValueError(f"Frame {i+1} download failed: HTTP {resp.status_code}")
            if "text/html" in resp.headers.get("Content-Type", ""):
                raise ValueError(f"Frame {i+1}: got HTML — URL needs auth: {url[:100]}")
            raw = resp.content

        img = Image.open(BytesIO(raw)).convert("RGB")
        src_r, tgt_r = img.width / img.height, width / height
        if src_r > tgt_r:
            new_h = height; new_w = int(img.width * height / img.height)
        else:
            new_w = width;  new_h = int(img.height * width / img.width)
        img  = img.resize((new_w, new_h), Image.LANCZOS)
        left = (new_w - width) // 2; top = (new_h - height) // 2
        img  = img.crop((left, top, left + width, top + height))
        path = os.path.join(tmp, f"frame_{i:03d}.png")
        img.save(path, "PNG")
        frame_paths.append(path)
        img.close()
        _log(f"  Frame {i+1}: {len(raw)//1024}KB saved")

    # ── 2. Encode each frame to a clip ────────────────────────
    clip_paths = []
    for i, (frame, dur) in enumerate(zip(frame_paths, durations)):
        clip = os.path.join(tmp, f"clip_{i:03d}.mp4")
        if transition == "fade":
            fade_in  = 0 if i == 0              else fade_dur
            fade_out = 0 if i == len(images) - 1 else fade_dur
            vf_parts = ["setsar=1", "fps=24"]
            if fade_in  > 0: vf_parts.append(f"fade=t=in:st=0:d={fade_in}")
            if fade_out > 0: vf_parts.append(f"fade=t=out:st={dur - fade_out:.3f}:d={fade_out}")
            vf = ",".join(vf_parts)
        else:
            vf = "setsar=1,fps=24"

        cmd = ["ffmpeg", "-y", "-loglevel", "error",
               "-loop", "1", "-t", str(dur), "-i", frame,
               "-vf", vf, "-c:v", "libx264", "-preset", "ultrafast",
               "-crf", "26", "-pix_fmt", "yuv420p", clip]
        r = subprocess.run(cmd, stderr=subprocess.PIPE, timeout=180)
        if r.returncode != 0:
            raise RuntimeError(f"FFmpeg clip {i+1} failed: {r.stderr.decode(errors='replace')[-300:]}")
        try: os.remove(frame)
        except: pass
        clip_paths.append(clip)
        _log(f"  Clip {i+1}/{len(frame_paths)} done")

    # ── 3. Concatenate ────────────────────────────────────────
    output = os.path.join(tmp, "reel.mp4")

    if transition == "xfade":
        _log("  xfade concat…")
        r = _concat_xfade(clip_paths, durations, fade_dur, output)
    else:
        _log("  fade concat…")
        concat_txt = os.path.join(tmp, "concat.txt")
        with open(concat_txt, "w") as f:
            for c in clip_paths: f.write(f"file '{c}'\n")
        cmd = ["ffmpeg", "-y", "-loglevel", "error",
               "-f", "concat", "-safe", "0", "-i", concat_txt,
               "-c", "copy", output]
        r = subprocess.run(cmd, stderr=subprocess.PIPE, timeout=180)
        r.stderr = r.stderr.decode(errors="replace") if isinstance(r.stderr, bytes) else ""

    if r.returncode != 0:
        raise RuntimeError(f"FFmpeg concat failed: {r.stderr[-500:]}")

    # ── 4. Mix in background music (optional) ─────────────────
    has_music = music_data or music_url
    if has_music:
        _log(f"  Mixing background music (volume={music_volume})…")
        audio_path = os.path.join(tmp, "music.mp3")

        if music_url:
            # Download directly from Jamendo (or any public URL)
            _log(f"  Downloading music from: {music_url[:80]}")
            resp = sess.get(music_url, timeout=60, allow_redirects=True)
            if resp.status_code != 200:
                _log(f"  Music download failed HTTP {resp.status_code} — skipping")
                has_music = False
            else:
                with open(audio_path, "wb") as f:
                    f.write(resp.content)
        else:
            # Legacy: base64 data URI
            if "," in music_data:
                _, b64audio = music_data.split(",", 1)
            else:
                b64audio = music_data
            with open(audio_path, "wb") as f:
                f.write(base64.b64decode(b64audio))

    if has_music:
        import random

        mixed = os.path.join(tmp, "reel_music.mp4")

        # Skip the quiet intro — start 15–25 s into the track so the music
        # is already at full energy when the reel begins.
        music_start = random.uniform(15, 25)

        # Fade in over 1.5 s at the start; fade out over 2 s before the end.
        # Total video duration is sum of slide durations.
        video_dur = sum(durations)
        fade_in_dur  = 1.5
        fade_out_dur = 2.0
        fade_out_start = max(0, video_dur - fade_out_dur)

        audio_filter = (
            f"afade=t=in:st=0:d={fade_in_dur},"
            f"afade=t=out:st={fade_out_start:.3f}:d={fade_out_dur},"
            f"volume={music_volume}"
        )

        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", output,                              # video (no audio)
            "-ss", str(music_start), "-i", audio_path, # music starting at offset
            "-c:v", "copy",                            # don't re-encode video
            "-c:a", "aac", "-b:a", "128k",             # encode audio as AAC
            "-filter:a", audio_filter,
            "-shortest",                               # cut when video ends
            mixed,
        ]
        r2 = subprocess.run(cmd, stderr=subprocess.PIPE, timeout=120)
        if r2.returncode != 0:
            err = r2.stderr.decode(errors="replace")[-300:]
            _log(f"  Music mix failed (continuing without music): {err}")
        else:
            output = mixed
            _log(f"  Music mixed (start={music_start:.1f}s, fade-in={fade_in_dur}s, fade-out={fade_out_dur}s)")

    return output


def _concat_xfade(clip_paths, durations, fade_dur, output):
    n = len(clip_paths)
    if n == 1:
        import shutil; shutil.copy(clip_paths[0], output)
        class _R: returncode = 0; stderr = ""
        return _R()

    inputs = []
    for p in clip_paths: inputs += ["-i", p]

    filters    = []
    offset     = durations[0] - fade_dur
    prev_label = "[0:v]"
    for i in range(1, n):
        out_label = f"[v{i}]" if i < n - 1 else "[vout]"
        filters.append(
            f"{prev_label}[{i}:v]xfade=transition=dissolve:"
            f"duration={fade_dur}:offset={offset:.3f}{out_label}"
        )
        offset    += durations[i] - fade_dur
        prev_label = out_label

    cmd = (["ffmpeg", "-y", "-loglevel", "error"]
           + inputs
           + ["-filter_complex", ";".join(filters),
              "-map", "[vout]",
              "-c:v", "libx264", "-preset", "ultrafast",
              "-crf", "26", "-pix_fmt", "yuv420p", output])
    r = subprocess.run(cmd, stderr=subprocess.PIPE, timeout=300)
    r.stderr = r.stderr.decode(errors="replace") if isinstance(r.stderr, bytes) else ""
    return r


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
