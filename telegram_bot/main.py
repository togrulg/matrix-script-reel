"""
Matrix Script Telegram Bot
Webhook-based bot that lets users generate Instagram content via GAS pipeline.

Flow:
  1. User sends idea → AI expands to 5 topics
  2. User picks topic + post type
  3. Bot sends order to GAS Web App
  4. GAS runs pipeline and calls back via POST /gas_callback
  5. Bot shows content preview with Approve / Regenerate / Edit buttons
"""
import os
import json
import logging
import requests
from flask import Flask, request, jsonify
from ai import expand_topics

# ── Config ────────────────────────────────────────────────────
BOT_TOKEN     = os.environ.get('BOT_TOKEN', '')
GAS_WEBAPP_URL = os.environ.get('GAS_WEBAPP_URL', '')   # deployed GAS Web App URL
GAS_SECRET    = os.environ.get('GAS_SECRET', '')        # shared secret for request auth
WEBHOOK_SECRET = os.environ.get('WEBHOOK_SECRET', '')   # optional Telegram webhook secret token

TELEGRAM_API = f'https://api.telegram.org/bot{BOT_TOKEN}'

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

app = Flask(__name__)

# ── Per-user conversation state ───────────────────────────────
# { user_id: { step, topics, idea, chat_id, original_idea } }
_STATE: dict = {}

POST_TYPES = [
    ('reel',       'Рилс 🎬'),
    ('carousel',   'Карусель 🖼️'),
    ('image post', 'Фото 📸'),
    ('story',      'История ✨'),
]
POST_TYPE_MAP = dict(POST_TYPES)

# ── Telegram helpers ──────────────────────────────────────────

def _tg(method: str, payload: dict, files=None) -> dict:
    url = f'{TELEGRAM_API}/{method}'
    try:
        if files:
            r = requests.post(url, data=payload, files=files, timeout=30)
        else:
            r = requests.post(url, json=payload, timeout=15)
        return r.json()
    except Exception as e:
        log.error('Telegram API error [%s]: %s', method, e)
        return {}


def send(chat_id, text, reply_markup=None, parse_mode='HTML') -> dict:
    p = {'chat_id': chat_id, 'text': text, 'parse_mode': parse_mode}
    if reply_markup:
        p['reply_markup'] = json.dumps(reply_markup)
    return _tg('sendMessage', p)


def edit_msg(chat_id, message_id, text, reply_markup=None, parse_mode='HTML'):
    p = {'chat_id': chat_id, 'message_id': message_id,
         'text': text, 'parse_mode': parse_mode}
    if reply_markup:
        p['reply_markup'] = json.dumps(reply_markup)
    _tg('editMessageText', p)


def send_photo(chat_id, photo_url, caption='', reply_markup=None):
    p = {'chat_id': chat_id, 'photo': photo_url, 'caption': caption, 'parse_mode': 'HTML'}
    if reply_markup:
        p['reply_markup'] = json.dumps(reply_markup)
    return _tg('sendPhoto', p)


def send_video_bytes(chat_id, video_bytes: bytes, caption='', reply_markup=None):
    p = {'chat_id': chat_id, 'caption': caption, 'parse_mode': 'HTML'}
    if reply_markup:
        p['reply_markup'] = json.dumps(reply_markup)
    return _tg('sendVideo', p, files={'video': ('reel.mp4', video_bytes, 'video/mp4')})


def send_media_group(chat_id, photo_urls: list, caption=''):
    media = []
    for i, url in enumerate(photo_urls):
        item = {'type': 'photo', 'media': url}
        if i == 0 and caption:
            item['caption'] = caption
            item['parse_mode'] = 'HTML'
        media.append(item)
    return _tg('sendMediaGroup', {'chat_id': chat_id, 'media': json.dumps(media)})


def answer_cb(callback_id, text=''):
    _tg('answerCallbackQuery', {'callback_query_id': callback_id, 'text': text})


# ── Keyboards ─────────────────────────────────────────────────

def _topics_kb(topics: list) -> dict:
    return {
        'inline_keyboard': [
            [{'text': f'{i+1}. {t[:80]}', 'callback_data': f'topic:{i}'}]
            for i, t in enumerate(topics)
        ]
    }


def _type_kb() -> dict:
    return {
        'inline_keyboard': [
            [{'text': label, 'callback_data': f'type:{code}'} for code, label in POST_TYPES[:2]],
            [{'text': label, 'callback_data': f'type:{code}'} for code, label in POST_TYPES[2:]],
        ]
    }


def _review_kb(row_id: str) -> dict:
    return {
        'inline_keyboard': [[
            {'text': '✅ Одобрить',       'callback_data': f'approve:{row_id}'},
            {'text': '🔄 Регенерировать', 'callback_data': f'regen:{row_id}'},
            {'text': '✏️ Новая идея',     'callback_data': f'newidea:{row_id}'},
        ]]
    }


# ── Webhook handler ───────────────────────────────────────────

@app.route('/webhook', methods=['POST'])
def webhook():
    # Optional webhook secret validation
    if WEBHOOK_SECRET:
        token = request.headers.get('X-Telegram-Bot-Api-Secret-Token', '')
        if token != WEBHOOK_SECRET:
            return jsonify(ok=False), 403

    update = request.get_json(force=True, silent=True) or {}

    # ── Callback query (inline button press) ──────────────────
    if 'callback_query' in update:
        _handle_callback(update['callback_query'])
        return jsonify(ok=True)

    # ── Regular message ────────────────────────────────────────
    if 'message' in update:
        _handle_message(update['message'])

    return jsonify(ok=True)


def _handle_message(msg: dict):
    user_id  = msg['from']['id']
    chat_id  = msg['chat']['id']
    text     = (msg.get('text') or '').strip()
    username = msg['from'].get('username') or msg['from'].get('first_name', 'User')

    if not text:
        return

    if text == '/start':
        _STATE.pop(user_id, None)
        send(chat_id,
             '👋 <b>Matrix Script Bot</b>\n\n'
             'Напиши идею для поста — я предложу 5 тем на выбор, '
             'ты выберешь нужную и бот сгенерирует готовый контент.\n\n'
             '<i>Пример: «хочу про деньги и матрицу судьбы»</i>')
        return

    if text.startswith('/'):
        return

    # Any non-command text → treat as new idea
    _STATE[user_id] = {'step': 'idle', 'chat_id': chat_id, 'username': username}

    thinking = send(chat_id, '🤔 <i>Анализирую идею и генерирую темы…</i>')
    thinking_id = (thinking.get('result') or {}).get('message_id')

    try:
        topics = expand_topics(text)
    except Exception as e:
        log.exception('expand_topics failed')
        send(chat_id, f'❌ Не удалось сгенерировать темы:\n<code>{e}</code>')
        return

    _STATE[user_id].update(step='topics_shown', topics=topics, original_idea=text)

    topics_text = '\n'.join(f'{i+1}. {t}' for i, t in enumerate(topics))
    body = (f'💡 <b>Вот 5 тем по твоей идее:</b>\n\n{topics_text}\n\n'
            f'<i>Выбери одну — и скажу, какой тип поста сделать:</i>')

    if thinking_id:
        edit_msg(chat_id, thinking_id, body, reply_markup=_topics_kb(topics))
    else:
        send(chat_id, body, reply_markup=_topics_kb(topics))


def _handle_callback(cq: dict):
    user_id  = cq['from']['id']
    chat_id  = cq['message']['chat']['id']
    msg_id   = cq['message']['message_id']
    data     = cq.get('data', '')
    username = cq['from'].get('username') or cq['from'].get('first_name', 'User')

    answer_cb(cq['id'])
    state = _STATE.get(user_id, {})

    # ── Topic selected ─────────────────────────────────────────
    if data.startswith('topic:'):
        idx    = int(data.split(':')[1])
        topics = state.get('topics', [])
        if idx >= len(topics):
            return
        chosen = topics[idx]
        _STATE[user_id] = {**state, 'step': 'type_shown', 'idea': chosen}
        edit_msg(chat_id, msg_id,
                 f'✅ <b>Выбрана тема:</b>\n{chosen}\n\n<b>Выбери тип поста:</b>',
                 reply_markup=_type_kb())

    # ── Post type selected ─────────────────────────────────────
    elif data.startswith('type:'):
        post_type = data.split(':', 1)[1]
        idea      = state.get('idea', '')
        if not idea:
            send(chat_id, '❌ Что-то пошло не так. Начни заново — отправь идею.')
            return

        type_label = POST_TYPE_MAP.get(post_type, post_type)
        _STATE[user_id] = {**state, 'step': 'generating', 'post_type': post_type}

        edit_msg(chat_id, msg_id,
                 f'✅ <b>Тема:</b> {idea}\n'
                 f'<b>Тип:</b> {type_label}\n\n'
                 f'⏳ <i>Отправляю в генерацию…\n'
                 f'Обычно 3–5 минут. Пришлю, как будет готово.</i>')

        _send_to_gas(chat_id, user_id, username, idea, post_type)

    # ── Approve ────────────────────────────────────────────────
    elif data.startswith('approve:'):
        edit_msg(chat_id, msg_id,
                 cq['message'].get('text', '') + '\n\n✅ <b>Одобрено!</b>',
                 reply_markup=None)
        send(chat_id, '👍 Контент одобрен. Напиши новую идею когда будешь готов.')

    # ── Regenerate ─────────────────────────────────────────────
    elif data.startswith('regen:'):
        idea      = state.get('idea', '')
        post_type = state.get('post_type', 'carousel')
        if not idea:
            send(chat_id, '❌ Не могу регенерировать — потеряна идея. Отправь заново.')
            return
        type_label = POST_TYPE_MAP.get(post_type, post_type)
        edit_msg(chat_id, msg_id,
                 f'🔄 <b>Регенерирую…</b>\n'
                 f'<b>Тема:</b> {idea}\n<b>Тип:</b> {type_label}\n\n'
                 f'⏳ <i>Подожди 3–5 минут.</i>')
        _STATE[user_id] = {**state, 'step': 'generating'}
        _send_to_gas(chat_id, user_id, username, idea, post_type)

    # ── New idea ───────────────────────────────────────────────
    elif data.startswith('newidea:'):
        _STATE.pop(user_id, None)
        edit_msg(chat_id, msg_id,
                 cq['message'].get('text', '') + '\n\n✏️ <i>Начинаем заново.</i>')
        send(chat_id, '✏️ Напиши новую идею для поста:')


def _send_to_gas(chat_id, user_id, username, idea, post_type):
    if not GAS_WEBAPP_URL:
        send(chat_id, '❌ GAS_WEBAPP_URL не настроен. Обратись к администратору.')
        return
    try:
        resp = requests.post(GAS_WEBAPP_URL, json={
            'action':   'generate',
            'idea':     idea,
            'postType': post_type,
            'chatId':   chat_id,
            'userId':   user_id,
            'username': username,
            'secret':   GAS_SECRET,
        }, timeout=30)
        data = resp.json()
        if data.get('status') != 'queued':
            raise Exception(data.get('error') or resp.text[:200])
    except Exception as e:
        log.exception('GAS request failed')
        send(chat_id, f'❌ Не удалось запустить генерацию:\n<code>{e}</code>')
        _STATE[user_id]['step'] = 'idle'


# ── GAS callback endpoint ─────────────────────────────────────
# GAS calls POST /gas_callback when content is ready.

@app.route('/gas_callback', methods=['POST'])
def gas_callback():
    """
    Receives progress updates and final content from GAS.

    Expected JSON shapes:

    Content ready (step ①):
    {
      "event":     "content_ready",
      "secret":    "...",
      "chatId":    123,
      "rowId":     "5",
      "postType":  "reel",
      "hook":      "...",
      "slides":    ["slide1", "slide2", ...],
      "caption":   "...",
      "hashtags":  "..."
    }

    Images ready (step ②③ — carousel/image):
    {
      "event":      "images_ready",
      "secret":     "...",
      "chatId":     123,
      "rowId":      "5",
      "thumbUrls":  ["https://lh3...", ...]
    }

    Reel ready (step ④ — after Render finishes):
    {
      "event":    "reel_ready",
      "secret":   "...",
      "chatId":   123,
      "rowId":    "5",
      "videoUrl": "https://drive.google.com/..."   ← or base64
    }

    Error:
    {
      "event":   "error",
      "secret":  "...",
      "chatId":  123,
      "message": "..."
    }
    """
    body = request.get_json(force=True, silent=True) or {}

    if GAS_SECRET and body.get('secret') != GAS_SECRET:
        return jsonify(ok=False, error='forbidden'), 403

    event    = body.get('event', '')
    chat_id  = body.get('chatId')
    row_id   = str(body.get('rowId', ''))
    post_type = body.get('postType', '')

    if not chat_id:
        return jsonify(ok=False, error='missing chatId'), 400

    log.info('gas_callback event=%s chatId=%s rowId=%s', event, chat_id, row_id)

    # ── Content text ready ─────────────────────────────────────
    if event == 'content_ready':
        hook     = body.get('hook', '')
        slides   = body.get('slides', [])
        caption  = body.get('caption', '')
        hashtags = body.get('hashtags', '')

        # Format slides nicely
        slides_fmt = ''
        for i, s in enumerate(slides):
            # Replace | separator with bold headline + normal sub-line
            if '|' in s:
                head, sub = s.split('|', 1)
                slides_fmt += f'\n<b>{i+1}. {head.strip()}</b>\n<i>{sub.strip()}</i>'
            else:
                slides_fmt += f'\n{i+1}. {s}'

        type_label = POST_TYPE_MAP.get(post_type, post_type)
        msg = (
            f'✅ <b>Контент готов!</b> ({type_label})\n\n'
            f'🎣 <b>Hook:</b>\n{hook}\n\n'
            f'📋 <b>Слайды:</b>{slides_fmt}\n\n'
            f'📝 <b>Подпись (первые 200 символов):</b>\n{caption[:200]}…\n\n'
            f'<i>Изображения и видео скоро придут отдельным сообщением.</i>'
        )
        send(chat_id, msg)

    # ── Slide images ready ─────────────────────────────────────
    elif event == 'images_ready':
        thumb_urls = body.get('thumbUrls', [])
        if thumb_urls:
            if len(thumb_urls) == 1:
                send_photo(chat_id, thumb_urls[0],
                           caption='🖼️ Готовые слайды:',
                           reply_markup=_review_kb(row_id))
            else:
                send_media_group(chat_id, thumb_urls,
                                 caption='🖼️ Готовые слайды:')
                # Send review buttons in a separate message
                send(chat_id, '👆 Проверь слайды:', reply_markup=_review_kb(row_id))
        else:
            send(chat_id, '✅ Слайды готовы (URL не получены).', reply_markup=_review_kb(row_id))

    # ── Reel MP4 ready ─────────────────────────────────────────
    elif event == 'reel_ready':
        video_url = body.get('videoUrl', '')
        video_b64 = body.get('videoB64', '')

        if video_b64:
            import base64
            video_bytes = base64.b64decode(video_b64)
            send_video_bytes(chat_id, video_bytes,
                             caption='🎬 Рилс готов!',
                             reply_markup=_review_kb(row_id))
        elif video_url:
            send(chat_id,
                 f'🎬 <b>Рилс готов!</b>\n<a href="{video_url}">Скачать MP4</a>',
                 reply_markup=_review_kb(row_id))
        else:
            send(chat_id, '🎬 Рилс готов! Проверь папку на Google Drive.',
                 reply_markup=_review_kb(row_id))

    # ── Error ──────────────────────────────────────────────────
    elif event == 'error':
        msg = body.get('message', 'Неизвестная ошибка')
        send(chat_id, f'❌ <b>Ошибка генерации:</b>\n<code>{msg}</code>')

    return jsonify(ok=True)


# ── Utility routes ────────────────────────────────────────────

@app.route('/health')
def health():
    return jsonify(status='ok', states=len(_STATE))


@app.route('/set_webhook')
def set_webhook():
    """Call once to register the webhook: GET /set_webhook?url=https://your-bot.onrender.com"""
    base_url = request.args.get('url', '').rstrip('/')
    if not base_url:
        return 'Pass ?url=https://your-bot-domain.onrender.com', 400
    payload = {'url': f'{base_url}/webhook'}
    if WEBHOOK_SECRET:
        payload['secret_token'] = WEBHOOK_SECRET
    r = requests.post(f'{TELEGRAM_API}/setWebhook', json=payload, timeout=10)
    return jsonify(r.json())


@app.route('/webhook_info')
def webhook_info():
    r = requests.get(f'{TELEGRAM_API}/getWebhookInfo', timeout=10)
    return jsonify(r.json())


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8081))
    app.run(host='0.0.0.0', port=port, debug=False)
