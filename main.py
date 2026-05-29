import os
import base64
import json
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

# ── Telegram bot config ───────────────────────────────────────
BOT_TOKEN      = os.environ.get("BOT_TOKEN", "")
GAS_SECRET     = os.environ.get("GAS_SECRET", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
GROQ_API_KEY   = os.environ.get("GROQ_API_KEY", "")
GAS_WEBAPP_URL = os.environ.get("GAS_WEBAPP_URL", "")
TELEGRAM_API   = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Per-user conversation state: { user_id: { step, topics, idea, chat_id, post_type, ... } }
_BOT_STATE: dict = {}

_POST_TYPES = [
    ("reel",       "Рилс 🎬"),
    ("carousel",   "Карусель 🖼️"),
    ("image post", "Фото 📸"),
    ("story",      "История ✨"),
]
_POST_TYPE_MAP = dict(_POST_TYPES)

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


# ═══════════════════════════════════════════════════════════════
# TELEGRAM BOT ROUTES
# ═══════════════════════════════════════════════════════════════

# ── Telegram helpers ──────────────────────────────────────────

def _tg(method, payload, files=None):
    if not BOT_TOKEN:
        return {}
    url = f"{TELEGRAM_API}/{method}"
    try:
        if files:
            r = requests.post(url, data=payload, files=files, timeout=30)
        else:
            r = requests.post(url, json=payload, timeout=15)
        return r.json()
    except Exception as e:
        _log(f"Telegram API [{method}] error: {e}")
        return {}

def _tg_send(chat_id, text, reply_markup=None, parse_mode="HTML"):
    p = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    if reply_markup:
        p["reply_markup"] = json.dumps(reply_markup)
    return _tg("sendMessage", p)

def _tg_edit(chat_id, message_id, text, reply_markup=None, parse_mode="HTML"):
    p = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": parse_mode}
    if reply_markup:
        p["reply_markup"] = json.dumps(reply_markup)
    _tg("editMessageText", p)

def _tg_send_photo(chat_id, photo_url, caption="", reply_markup=None):
    p = {"chat_id": chat_id, "photo": photo_url, "caption": caption, "parse_mode": "HTML"}
    if reply_markup:
        p["reply_markup"] = json.dumps(reply_markup)
    return _tg("sendPhoto", p)

def _tg_send_media_group(chat_id, photo_urls, caption=""):
    media = []
    for i, url in enumerate(photo_urls):
        item = {"type": "photo", "media": url}
        if i == 0 and caption:
            item["caption"] = caption
            item["parse_mode"] = "HTML"
        media.append(item)
    return _tg("sendMediaGroup", {"chat_id": chat_id, "media": json.dumps(media)})

def _tg_send_video(chat_id, video_bytes, caption="", reply_markup=None):
    p = {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"}
    if reply_markup:
        p["reply_markup"] = json.dumps(reply_markup)
    return _tg("sendVideo", p, files={"video": ("reel.mp4", video_bytes, "video/mp4")})

def _tg_answer_cb(callback_id, text=""):
    _tg("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

# ── Keyboards ─────────────────────────────────────────────────

def _topics_kb(topics):
    return {"inline_keyboard": [
        [{"text": f"{i+1}. {t[:80]}", "callback_data": f"topic:{i}"}]
        for i, t in enumerate(topics)
    ]}

def _type_kb():
    return {"inline_keyboard": [
        [{"text": lbl, "callback_data": f"type:{code}"} for code, lbl in _POST_TYPES[:2]],
        [{"text": lbl, "callback_data": f"type:{code}"} for code, lbl in _POST_TYPES[2:]],
    ]}

def _review_kb(row_id):
    return {"inline_keyboard": [[
        {"text": "✅ Одобрить",       "callback_data": f"approve:{row_id}"},
        {"text": "🔄 Регенерировать", "callback_data": f"regen:{row_id}"},
        {"text": "✏️ Новая идея",     "callback_data": f"newidea:{row_id}"},
    ]]}

# ── Topic expansion via Groq ───────────────────────────────────

def _expand_topics(idea):
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY not set")
    system = (
        "Ты — эксперт по контент-стратегии для Instagram в нише «Матрица Судьбы», "
        "нумерология, эзотерика, личностный рост. Аудитория — русскоязычные женщины 25–45 лет. "
        "По заданной грубой идее придумай ровно 5 конкретных, цепляющих тем для поста или рилса. "
        "Каждая — одно завершённое предложение (макс. 15 слов), интригующий заголовок. "
        "Темы разные по углу: вопрос, провокация, история, факт, практика. "
        "Верни ТОЛЬКО JSON-массив из 5 строк — без пояснений, без markdown."
    )
    resp = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        json={
            "model": "llama-3.3-70b-versatile",
            "messages": [
                {"role": "system", "content": system},
                {"role": "user",   "content": f"Идея: {idea}"},
            ],
            "max_tokens": 600,
            "temperature": 0.9,
            "stream": False,
        },
        headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    raw = resp.json()["choices"][0]["message"]["content"].strip()

    # Strip markdown fences if present
    if "```" in raw:
        parts = raw.split("```")
        raw = parts[1] if len(parts) > 1 else parts[0]
        if raw.lower().startswith("json"):
            raw = raw[4:]
    raw = raw.strip()

    # Extract just the [...] array — model sometimes adds extra text before/after
    start = raw.find("[")
    end   = raw.rfind("]")
    if start != -1 and end != -1 and end > start:
        raw = raw[start:end + 1]

    topics = json.loads(raw)
    if not isinstance(topics, list):
        raise ValueError("Expected a JSON array")
    return [str(t).strip() for t in topics[:5]]

# ── Webhook handler ───────────────────────────────────────────

@app.route("/webhook", methods=["POST"])
def bot_webhook():
    if WEBHOOK_SECRET:
        token = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if token != WEBHOOK_SECRET:
            return jsonify(ok=False), 403

    update = request.get_json(force=True, silent=True) or {}

    if "callback_query" in update:
        _bot_handle_callback(update["callback_query"])
    elif "message" in update:
        _bot_handle_message(update["message"])

    return jsonify(ok=True)


def _bot_handle_message(msg):
    user_id  = msg["from"]["id"]
    chat_id  = msg["chat"]["id"]
    text     = (msg.get("text") or "").strip()
    username = msg["from"].get("username") or msg["from"].get("first_name", "User")

    if not text:
        return

    if text == "/start":
        _BOT_STATE.pop(user_id, None)
        _tg_send(chat_id,
            "👋 <b>Matrix Script Bot</b>\n\n"
            "Напиши идею для поста — я предложу 5 тем, ты выберешь нужную "
            "и бот сгенерирует готовый контент.\n\n"
            "<i>Пример: «хочу про деньги и матрицу судьбы»</i>")
        return

    if text.startswith("/"):
        return

    # Any non-command text → new idea
    _BOT_STATE[user_id] = {"step": "idle", "chat_id": chat_id, "username": username}
    thinking = _tg_send(chat_id, "🤔 <i>Анализирую идею и генерирую темы…</i>")
    thinking_id = (thinking.get("result") or {}).get("message_id")

    def _run():
        try:
            topics = _expand_topics(text)
        except Exception as e:
            _log(f"expand_topics error: {e}")
            _tg_send(chat_id, f"❌ Не удалось сгенерировать темы:\n<code>{e}</code>")
            return

        _BOT_STATE[user_id].update(step="topics_shown", topics=topics, original_idea=text)
        body = (
            "💡 <b>Вот 5 тем по твоей идее:</b>\n\n"
            + "\n".join(f"{i+1}. {t}" for i, t in enumerate(topics))
            + "\n\n<i>Выбери одну:</i>"
        )
        if thinking_id:
            _tg_edit(chat_id, thinking_id, body, reply_markup=_topics_kb(topics))
        else:
            _tg_send(chat_id, body, reply_markup=_topics_kb(topics))

    threading.Thread(target=_run, daemon=True).start()


def _bot_handle_callback(cq):
    user_id  = cq["from"]["id"]
    chat_id  = cq["message"]["chat"]["id"]
    msg_id   = cq["message"]["message_id"]
    data     = cq.get("data", "")
    username = cq["from"].get("username") or cq["from"].get("first_name", "User")

    _tg_answer_cb(cq["id"])
    state = _BOT_STATE.get(user_id, {})

    if data.startswith("topic:"):
        idx    = int(data.split(":")[1])
        topics = state.get("topics", [])
        if idx >= len(topics):
            return
        chosen = topics[idx]
        _BOT_STATE[user_id] = {**state, "step": "type_shown", "idea": chosen}
        _tg_edit(chat_id, msg_id,
            f"✅ <b>Выбрана тема:</b>\n{chosen}\n\n<b>Выбери тип поста:</b>",
            reply_markup=_type_kb())

    elif data.startswith("type:"):
        post_type = data.split(":", 1)[1]
        idea      = state.get("idea", "")
        if not idea:
            _tg_send(chat_id, "❌ Что-то пошло не так. Начни заново — отправь идею.")
            return
        type_label = _POST_TYPE_MAP.get(post_type, post_type)
        _BOT_STATE[user_id] = {**state, "step": "generating", "post_type": post_type}
        _tg_edit(chat_id, msg_id,
            f"✅ <b>Тема:</b> {idea}\n<b>Тип:</b> {type_label}\n\n"
            f"⏳ <i>Отправляю в генерацию… обычно 3–5 минут. Пришлю как будет готово.</i>")
        _bot_send_to_gas(chat_id, user_id, username, idea, post_type)

    elif data.startswith("approve:"):
        orig = cq["message"].get("text", "")
        _tg_edit(chat_id, msg_id, orig + "\n\n✅ <b>Одобрено!</b>")
        _tg_send(chat_id, "👍 Контент одобрен. Напиши новую идею когда будешь готов.")

    elif data.startswith("regen:"):
        idea      = state.get("idea", "")
        post_type = state.get("post_type", "carousel")
        if not idea:
            _tg_send(chat_id, "❌ Не могу регенерировать — потеряна идея. Отправь заново.")
            return
        type_label = _POST_TYPE_MAP.get(post_type, post_type)
        _BOT_STATE[user_id] = {**state, "step": "generating"}
        _tg_edit(chat_id, msg_id,
            f"🔄 <b>Регенерирую…</b>\n<b>Тема:</b> {idea}\n<b>Тип:</b> {type_label}\n\n"
            f"⏳ <i>Подожди 3–5 минут.</i>")
        _bot_send_to_gas(chat_id, user_id, username, idea, post_type)

    elif data.startswith("newidea:"):
        _BOT_STATE.pop(user_id, None)
        orig = cq["message"].get("text", "")
        _tg_edit(chat_id, msg_id, orig + "\n\n✏️ <i>Начинаем заново.</i>")
        _tg_send(chat_id, "✏️ Напиши новую идею для поста:")


def _bot_send_to_gas(chat_id, user_id, username, idea, post_type):
    if not GAS_WEBAPP_URL:
        _tg_send(chat_id, "❌ GAS_WEBAPP_URL не настроен. Обратись к администратору.")
        return
    try:
        resp = requests.post(GAS_WEBAPP_URL, json={
            "action":   "generate",
            "idea":     idea,
            "postType": post_type,
            "chatId":   chat_id,
            "userId":   user_id,
            "username": username,
            "secret":   GAS_SECRET,
        }, timeout=30)
        data = resp.json()
        if data.get("status") != "queued":
            raise Exception(data.get("error") or resp.text[:200])
    except Exception as e:
        _log(f"GAS request failed: {e}")
        _tg_send(chat_id, f"❌ Не удалось запустить генерацию:\n<code>{e}</code>")
        _BOT_STATE.get(user_id, {}).update(step="idle")


# ── GAS callback endpoint ─────────────────────────────────────

@app.route("/gas_callback", methods=["POST"])
def gas_callback():
    body = request.get_json(force=True, silent=True) or {}

    if GAS_SECRET and body.get("secret") != GAS_SECRET:
        return jsonify(ok=False, error="forbidden"), 403

    event     = body.get("event", "")
    chat_id   = body.get("chatId")
    row_id    = str(body.get("rowId", ""))
    post_type = body.get("postType", "")

    if not chat_id:
        return jsonify(ok=False, error="missing chatId"), 400

    _log(f"gas_callback event={event} chatId={chat_id} rowId={row_id}")

    if event == "content_ready":
        hook     = body.get("hook", "")
        slides   = body.get("slides", [])
        caption  = body.get("caption", "")
        type_label = _POST_TYPE_MAP.get(post_type, post_type)

        slides_fmt = ""
        for i, s in enumerate(slides):
            if "|" in s:
                head, sub = s.split("|", 1)
                slides_fmt += f"\n<b>{i+1}. {head.strip()}</b>\n<i>{sub.strip()}</i>"
            else:
                slides_fmt += f"\n{i+1}. {s}"

        msg = (
            f"✅ <b>Контент готов!</b> ({type_label})\n\n"
            f"🎣 <b>Hook:</b>\n{hook}\n\n"
            f"📋 <b>Слайды:</b>{slides_fmt}\n\n"
            f"📝 <b>Подпись:</b>\n{caption[:250]}…\n\n"
            f"<i>Изображения скоро придут отдельным сообщением.</i>"
        )
        _tg_send(chat_id, msg)

    elif event == "images_ready":
        thumb_urls = body.get("thumbUrls", [])
        if len(thumb_urls) == 1:
            _tg_send_photo(chat_id, thumb_urls[0],
                           caption="🖼️ Готовые слайды:",
                           reply_markup=_review_kb(row_id))
        elif len(thumb_urls) > 1:
            _tg_send_media_group(chat_id, thumb_urls, caption="🖼️ Готовые слайды:")
            _tg_send(chat_id, "👆 Проверь слайды:", reply_markup=_review_kb(row_id))
        else:
            _tg_send(chat_id, "✅ Слайды готовы.", reply_markup=_review_kb(row_id))

    elif event == "reel_ready":
        video_b64 = body.get("videoB64", "")
        video_url = body.get("videoUrl", "")
        if video_b64:
            video_bytes = base64.b64decode(video_b64)
            _tg_send_video(chat_id, video_bytes,
                           caption="🎬 Рилс готов!",
                           reply_markup=_review_kb(row_id))
        elif video_url:
            _tg_send(chat_id,
                     f"🎬 <b>Рилс готов!</b>\n<a href=\"{video_url}\">Скачать MP4</a>",
                     reply_markup=_review_kb(row_id))
        else:
            _tg_send(chat_id, "🎬 Рилс готов! Проверь папку на Google Drive.",
                     reply_markup=_review_kb(row_id))

    elif event == "error":
        msg = body.get("message", "Неизвестная ошибка")
        _tg_send(chat_id, f"❌ <b>Ошибка генерации:</b>\n<code>{msg}</code>")

    return jsonify(ok=True)


@app.route("/set_webhook")
def set_webhook():
    base = request.args.get("url", "").rstrip("/")
    if not base:
        return "Pass ?url=https://your-domain.onrender.com", 400
    payload = {"url": f"{base}/webhook"}
    if WEBHOOK_SECRET:
        payload["secret_token"] = WEBHOOK_SECRET
    r = requests.post(f"{TELEGRAM_API}/setWebhook", json=payload, timeout=10)
    return jsonify(r.json())


@app.route("/webhook_info")
def webhook_info():
    r = requests.get(f"{TELEGRAM_API}/getWebhookInfo", timeout=10)
    return jsonify(r.json())


# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
