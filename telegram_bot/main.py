"""
Matrix Script Telegram Bot
Webhook-based Instagram content pipeline controller.

Full conversation flow:
  idea → 5 topics → post type → tone → image source
  → GAS step① (content only) → preview + confirm
  → GAS step②③④ (images + overlay + reel)
  → review (Approve / Regen / New idea)

Bot commands:
  /start   — welcome + reset
  /cancel  — abort current flow
  /help    — show all commands
  /last    — show last generated content again
  /status  — check active pipeline state
"""
import os
import json
import logging
import threading
import requests
from flask import Flask, request, jsonify
from ai import expand_topics

# ── Config ────────────────────────────────────────────────────
BOT_TOKEN      = os.environ.get('BOT_TOKEN', '')
GAS_WEBAPP_URL = os.environ.get('GAS_WEBAPP_URL', '')
GAS_SECRET     = os.environ.get('GAS_SECRET', '')
WEBHOOK_SECRET = os.environ.get('WEBHOOK_SECRET', '')
TELEGRAM_API   = f'https://api.telegram.org/bot{BOT_TOKEN}'

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

app = Flask(__name__)

# ── State ─────────────────────────────────────────────────────
# { user_id: { step, topics, idea, post_type, tone, image_source,
#              chat_id, username, row_id, last_content } }
_STATE: dict = {}
_LAST:  dict = {}   # { user_id: last content_ready payload }

# ── Constants ─────────────────────────────────────────────────
POST_TYPES = [
    ('reel',       'Рилс 🎬'),
    ('carousel',   'Карусель 🖼️'),
    ('image post', 'Фото 📸'),
    ('story',      'История ✨'),
]
POST_TYPE_MAP = dict(POST_TYPES)

TONES = [
    ('mystical, premium, clear, emotionally engaging', 'Мистик ✨'),
    ('bright, optimistic, uplifting, sunny, joyful',   'Позитив ☀️'),
    ('warm, friendly, motivational',                   'Тепло 🌸'),
    ('bold, direct, no-nonsense',                      'Дерзко 🔥'),
    ('poetic, dreamy, lyrical',                        'Поэзия 🌙'),
    ('light and airy, soft, pastel, fresh, feminine',  'Нежность 🕊️'),
]
TONE_MAP = {code: label for code, label in TONES}

IMAGE_SOURCES = [
    ('Pexels',       'Pexels 📸'),
    ('Pollinations', 'Pollinations 🤖'),
    ('Unsplash',     'Unsplash 🌿'),
    ('HuggingFace',  'HuggingFace 🧠'),
    ('Pixabay',      'Pixabay 🎨'),
]
IMAGE_SOURCE_MAP = {code: label for code, label in IMAGE_SOURCES}

TEMPLATES = [
    ('Gold Classic',    'Gold Classic ✨'),
    ('Dark Mystery',    'Dark Mystery 🌑'),
    ('Celestial Blue',  'Celestial Blue 💙'),
    ('Rose Gold',       'Rose Gold 🌸'),
    ('Crimson Power',   'Crimson Power 🔴'),
    ('Snow White',      'Snow White ⬜'),
    ('Slate Pro',       'Slate Pro 🩶'),
    ('Emerald Elite',   'Emerald Elite 💚'),
]
TEMPLATE_MAP = {code: label for code, label in TEMPLATES}

MUSIC_VIBES = [
    ('ambient meditation spiritual',      'Медитация 🔮'),
    ('ambient spiritual uplifting',       'Подъём ⚡'),
    ('ambient spiritual relaxing',        'Релакс 🌊'),
    ('ambient spiritual epic',            'Эпик 🎺'),
    ('none',                              'Без музыки 🔇'),
]
MUSIC_VIBE_MAP = {code: label for code, label in MUSIC_VIBES}

# ── Telegram helpers ──────────────────────────────────────────

def _tg(method, payload, files=None):
    if not BOT_TOKEN:
        return {}
    url = f'{TELEGRAM_API}/{method}'
    try:
        if files:
            r = requests.post(url, data=payload, files=files, timeout=30)
        else:
            r = requests.post(url, json=payload, timeout=15)
        return r.json()
    except Exception as e:
        log.error('Telegram [%s]: %s', method, e)
        return {}


def send(chat_id, text, reply_markup=None, parse_mode='HTML'):
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
    p = {'chat_id': chat_id, 'photo': photo_url,
         'caption': caption, 'parse_mode': 'HTML'}
    if reply_markup:
        p['reply_markup'] = json.dumps(reply_markup)
    return _tg('sendPhoto', p)


def send_media_group(chat_id, photo_urls, caption=''):
    media = []
    for i, url in enumerate(photo_urls):
        item = {'type': 'photo', 'media': url}
        if i == 0 and caption:
            item['caption'] = caption
            item['parse_mode'] = 'HTML'
        media.append(item)
    return _tg('sendMediaGroup', {'chat_id': chat_id, 'media': json.dumps(media)})


def send_video_bytes(chat_id, video_bytes, caption='', reply_markup=None):
    p = {'chat_id': chat_id, 'caption': caption, 'parse_mode': 'HTML'}
    if reply_markup:
        p['reply_markup'] = json.dumps(reply_markup)
    return _tg('sendVideo', p, files={'video': ('reel.mp4', video_bytes, 'video/mp4')})


def answer_cb(callback_id, text=''):
    _tg('answerCallbackQuery', {'callback_query_id': callback_id, 'text': text})


# ── Keyboards ─────────────────────────────────────────────────

def _topics_kb(topics):
    return {'inline_keyboard': [
        [{'text': f'{i+1}. {t[:80]}', 'callback_data': f'topic:{i}'}]
        for i, t in enumerate(topics)
    ]}


def _type_kb():
    return {'inline_keyboard': [
        [{'text': lbl, 'callback_data': f'type:{code}'} for code, lbl in POST_TYPES[:2]],
        [{'text': lbl, 'callback_data': f'type:{code}'} for code, lbl in POST_TYPES[2:]],
    ]}


def _tone_kb():
    rows = []
    for i in range(0, len(TONES), 2):
        row = []
        for code, lbl in TONES[i:i+2]:
            row.append({'text': lbl, 'callback_data': f'tone:{code}'})
        rows.append(row)
    return {'inline_keyboard': rows}


def _source_kb():
    rows = []
    for i in range(0, len(IMAGE_SOURCES), 2):
        row = []
        for code, lbl in IMAGE_SOURCES[i:i+2]:
            row.append({'text': lbl, 'callback_data': f'source:{code}'})
        rows.append(row)
    return {'inline_keyboard': rows}


def _template_kb():
    rows = []
    for i in range(0, len(TEMPLATES), 2):
        row = []
        for code, lbl in TEMPLATES[i:i+2]:
            row.append({'text': lbl, 'callback_data': f'tmpl:{code}'})
        rows.append(row)
    return {'inline_keyboard': rows}


def _music_kb():
    rows = []
    for i in range(0, len(MUSIC_VIBES), 2):
        row = []
        for code, lbl in MUSIC_VIBES[i:i+2]:
            row.append({'text': lbl, 'callback_data': f'music:{code}'})
        rows.append(row)
    return {'inline_keyboard': rows}


def _confirm_kb(row_id):
    return {'inline_keyboard': [[
        {'text': '✅ Генерировать изображения', 'callback_data': f'confirm:{row_id}'},
        {'text': '🔄 Перегенерировать текст',  'callback_data': f'regen_content:{row_id}'},
    ], [
        {'text': '✏️ Новая идея', 'callback_data': 'newidea:0'},
    ]]}


def _review_kb(row_id):
    return {'inline_keyboard': [[
        {'text': '✅ Одобрить',       'callback_data': f'approve:{row_id}'},
        {'text': '🔄 Регенерировать', 'callback_data': f'regen:{row_id}'},
        {'text': '✏️ Новая идея',     'callback_data': 'newidea:0'},
    ]]}


# ── Webhook ───────────────────────────────────────────────────

@app.route('/webhook', methods=['POST'])
def webhook():
    if WEBHOOK_SECRET:
        token = request.headers.get('X-Telegram-Bot-Api-Secret-Token', '')
        if token != WEBHOOK_SECRET:
            return jsonify(ok=False), 403

    update = request.get_json(force=True, silent=True) or {}

    if 'callback_query' in update:
        _handle_callback(update['callback_query'])
    elif 'message' in update:
        _handle_message(update['message'])

    return jsonify(ok=True)


def _handle_message(msg):
    user_id  = msg['from']['id']
    chat_id  = msg['chat']['id']
    text     = (msg.get('text') or '').strip()
    username = msg['from'].get('username') or msg['from'].get('first_name', 'User')

    if not text:
        return

    # ── Commands ──────────────────────────────────────────────
    if text == '/start':
        _STATE.pop(user_id, None)
        send(chat_id,
             '👋 <b>Matrix Script Bot</b>\n\n'
             'Напиши идею для поста — я предложу 5 тем, ты выберешь нужную '
             'и бот сгенерирует готовый контент.\n\n'
             '<i>Пример: «хочу про деньги и матрицу судьбы»</i>\n\n'
             'Команды: /help /cancel /last /status')
        return

    if text == '/help':
        send(chat_id,
             '📋 <b>Команды бота:</b>\n\n'
             '• Напиши любую идею — старт нового поста\n'
             '• /cancel — отменить текущую генерацию\n'
             '• /last — показать последний сгенерированный контент\n'
             '• /status — статус текущей операции\n'
             '• /start — сбросить и начать заново\n'
             '• /help — эта справка')
        return

    if text == '/cancel':
        _STATE.pop(user_id, None)
        send(chat_id, '❌ Отменено. Напиши новую идею когда будешь готов.')
        return

    if text == '/last':
        last = _LAST.get(user_id)
        if not last:
            send(chat_id, 'Нет сохранённого контента. Начни с новой идеи.')
        else:
            _send_content_preview(chat_id, last, last.get('rowId', '?'))
        return

    if text == '/status':
        state = _STATE.get(user_id, {})
        step  = state.get('step', 'idle')
        if step == 'idle' or not state:
            send(chat_id, '💤 Нет активных операций. Напиши идею чтобы начать.')
        else:
            idea = state.get('idea', '—')
            pt   = POST_TYPE_MAP.get(state.get('post_type', ''), '—')
            send(chat_id, f'⚙️ <b>Статус:</b> {step}\n<b>Тема:</b> {idea}\n<b>Тип:</b> {pt}')
        return

    if text.startswith('/'):
        return

    # ── New idea ──────────────────────────────────────────────
    _STATE[user_id] = {'step': 'idle', 'chat_id': chat_id, 'username': username}
    thinking = send(chat_id, '🤔 <i>Анализирую идею и генерирую темы…</i>')
    thinking_id = (thinking.get('result') or {}).get('message_id')

    def _run():
        try:
            topics = expand_topics(text)
        except Exception as e:
            log.exception('expand_topics failed')
            send(chat_id, f'❌ Не удалось сгенерировать темы:\n<code>{e}</code>')
            return

        _STATE[user_id].update(step='topics_shown', topics=topics, original_idea=text)
        body = (
            '💡 <b>Вот 5 тем по твоей идее:</b>\n\n'
            + '\n'.join(f'{i+1}. {t}' for i, t in enumerate(topics))
            + '\n\n<i>Выбери одну:</i>'
        )
        if thinking_id:
            edit_msg(chat_id, thinking_id, body, reply_markup=_topics_kb(topics))
        else:
            send(chat_id, body, reply_markup=_topics_kb(topics))

    threading.Thread(target=_run, daemon=True).start()


def _handle_callback(cq):
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
                 f'✅ <b>Тема:</b> {chosen}\n\n<b>Тип поста:</b>',
                 reply_markup=_type_kb())

    # ── Post type selected ─────────────────────────────────────
    elif data.startswith('type:'):
        post_type = data.split(':', 1)[1]
        idea      = state.get('idea', '')
        if not idea:
            send(chat_id, '❌ Потеряна идея. Начни заново.')
            return
        _STATE[user_id] = {**state, 'step': 'tone_shown', 'post_type': post_type}
        edit_msg(chat_id, msg_id,
                 f'✅ <b>Тема:</b> {idea}\n<b>Тип:</b> {POST_TYPE_MAP.get(post_type, post_type)}\n\n'
                 f'<b>Тон и стиль:</b>',
                 reply_markup=_tone_kb())

    # ── Tone selected ──────────────────────────────────────────
    elif data.startswith('tone:'):
        tone = data.split(':', 1)[1]
        idea = state.get('idea', '')
        _STATE[user_id] = {**state, 'step': 'source_shown', 'tone': tone}
        edit_msg(chat_id, msg_id,
                 f'✅ <b>Тема:</b> {idea}\n'
                 f'<b>Тип:</b> {POST_TYPE_MAP.get(state.get("post_type",""), "")}\n'
                 f'<b>Тон:</b> {TONE_MAP.get(tone, tone)}\n\n'
                 f'<b>Источник изображений:</b>',
                 reply_markup=_source_kb())

    # ── Image source selected ──────────────────────────────────
    elif data.startswith('source:'):
        source = data.split(':', 1)[1]
        idea   = state.get('idea', '')
        _STATE[user_id] = {**state, 'step': 'template_shown', 'image_source': source}
        edit_msg(chat_id, msg_id,
                 f'✅ <b>Тема:</b> {idea}\n'
                 f'<b>Тип:</b> {POST_TYPE_MAP.get(state.get("post_type",""), "")}\n'
                 f'<b>Тон:</b> {TONE_MAP.get(state.get("tone",""), "")}\n'
                 f'<b>Источник:</b> {IMAGE_SOURCE_MAP.get(source, source)}\n\n'
                 f'<b>Шаблон оформления:</b>',
                 reply_markup=_template_kb())

    # ── Template selected ──────────────────────────────────────
    elif data.startswith('tmpl:'):
        template  = data.split(':', 1)[1]
        idea      = state.get('idea', '')
        post_type = state.get('post_type', 'carousel')
        tone      = state.get('tone', 'mystical, premium, clear, emotionally engaging')
        source    = state.get('image_source', 'Pexels')
        _STATE[user_id] = {**state, 'step': 'music_shown' if post_type == 'reel' else 'generating_content',
                           'template': template}

        if post_type == 'reel':
            edit_msg(chat_id, msg_id,
                     f'✅ <b>Тема:</b> {idea}\n'
                     f'<b>Тип:</b> {POST_TYPE_MAP.get(post_type, post_type)}\n'
                     f'<b>Тон:</b> {TONE_MAP.get(tone, tone)}\n'
                     f'<b>Источник:</b> {IMAGE_SOURCE_MAP.get(source, source)}\n'
                     f'<b>Шаблон:</b> {TEMPLATE_MAP.get(template, template)}\n\n'
                     f'<b>Фоновая музыка:</b>',
                     reply_markup=_music_kb())
        else:
            edit_msg(chat_id, msg_id,
                     f'✅ <b>Тема:</b> {idea}\n'
                     f'<b>Тип:</b> {POST_TYPE_MAP.get(post_type, post_type)}\n'
                     f'<b>Тон:</b> {TONE_MAP.get(tone, tone)}\n'
                     f'<b>Источник:</b> {IMAGE_SOURCE_MAP.get(source, source)}\n'
                     f'<b>Шаблон:</b> {TEMPLATE_MAP.get(template, template)}\n\n'
                     f'⏳ <i>Генерирую контент…</i>')
            _send_to_gas_content(chat_id, user_id, username, idea, post_type, tone, source, template)

    # ── Music vibe selected (reels only) ──────────────────────
    elif data.startswith('music:'):
        music_vibe = data.split(':', 1)[1]
        idea       = state.get('idea', '')
        post_type  = state.get('post_type', 'reel')
        tone       = state.get('tone', 'mystical, premium, clear, emotionally engaging')
        source     = state.get('image_source', 'Pexels')
        template   = state.get('template', 'Gold Classic')
        _STATE[user_id] = {**state, 'step': 'generating_content', 'music_vibe': music_vibe}

        edit_msg(chat_id, msg_id,
                 f'✅ <b>Тема:</b> {idea}\n'
                 f'<b>Тип:</b> {POST_TYPE_MAP.get(post_type, post_type)}\n'
                 f'<b>Тон:</b> {TONE_MAP.get(tone, tone)}\n'
                 f'<b>Источник:</b> {IMAGE_SOURCE_MAP.get(source, source)}\n'
                 f'<b>Шаблон:</b> {TEMPLATE_MAP.get(template, template)}\n'
                 f'<b>Музыка:</b> {MUSIC_VIBE_MAP.get(music_vibe, music_vibe)}\n\n'
                 f'⏳ <i>Генерирую контент…</i>')
        _send_to_gas_content(chat_id, user_id, username, idea, post_type, tone, source, template, music_vibe)

    # ── Confirm → generate images ──────────────────────────────
    elif data.startswith('confirm:'):
        row_id    = data.split(':', 1)[1]
        idea      = state.get('idea', '—')
        post_type = state.get('post_type', 'carousel')
        _STATE[user_id] = {**state, 'step': 'generating_media'}
        edit_msg(chat_id, msg_id,
                 cq['message'].get('text', '') +
                 '\n\n⏳ <i>Генерирую изображения и оформление…\nОбычно 3–5 минут.</i>')
        _send_to_gas_media(chat_id, user_id, username, row_id, post_type)

    # ── Regen content ──────────────────────────────────────────
    elif data.startswith('regen_content:'):
        idea       = state.get('idea', '')
        post_type  = state.get('post_type', 'carousel')
        tone       = state.get('tone', 'mystical, premium, clear, emotionally engaging')
        source     = state.get('image_source', 'Pexels')
        template   = state.get('template', 'Gold Classic')
        music_vibe = state.get('music_vibe', '')
        if not idea:
            send(chat_id, '❌ Потеряна идея. Начни заново.')
            return
        _STATE[user_id] = {**state, 'step': 'generating_content'}
        edit_msg(chat_id, msg_id,
                 f'🔄 <b>Регенерирую контент…</b>\n<b>Тема:</b> {idea}\n\n<i>Подожди немного.</i>')
        _send_to_gas_content(chat_id, user_id, username, idea, post_type, tone, source, template, music_vibe)

    # ── Regen (full) ───────────────────────────────────────────
    elif data.startswith('regen:'):
        idea       = state.get('idea', '')
        post_type  = state.get('post_type', 'carousel')
        tone       = state.get('tone', 'mystical, premium, clear, emotionally engaging')
        source     = state.get('image_source', 'Pexels')
        template   = state.get('template', 'Gold Classic')
        music_vibe = state.get('music_vibe', '')
        if not idea:
            send(chat_id, '❌ Потеряна идея. Начни заново.')
            return
        _STATE[user_id] = {**state, 'step': 'generating_content'}
        edit_msg(chat_id, msg_id,
                 f'🔄 <b>Регенерирую с нуля…</b>\n<b>Тема:</b> {idea}\n\n<i>Подожди 3–5 минут.</i>')
        _send_to_gas_content(chat_id, user_id, username, idea, post_type, tone, source, template, music_vibe)

    # ── Approve ────────────────────────────────────────────────
    elif data.startswith('approve:'):
        edit_msg(chat_id, msg_id,
                 cq['message'].get('text', '') + '\n\n✅ <b>Одобрено!</b>',
                 reply_markup=None)
        send(chat_id, '👍 Контент одобрен. Напиши новую идею когда будешь готов.')
        _STATE.pop(user_id, None)

    # ── New idea ───────────────────────────────────────────────
    elif data.startswith('newidea:'):
        _STATE.pop(user_id, None)
        orig = cq['message'].get('text', '')
        edit_msg(chat_id, msg_id, orig + '\n\n✏️ <i>Начинаем заново.</i>')
        send(chat_id, '✏️ Напиши новую идею для поста:')


# ── GAS communication ─────────────────────────────────────────

def _send_to_gas_content(chat_id, user_id, username, idea, post_type, tone,
                         image_source, template='Gold Classic', music_vibe=''):
    """Step ① only — generate content text, then wait for user confirmation."""
    if not GAS_WEBAPP_URL:
        send(chat_id, '❌ GAS_WEBAPP_URL не настроен.')
        return
    try:
        resp = requests.post(GAS_WEBAPP_URL, json={
            'action':      'generate_content',
            'idea':        idea,
            'postType':    post_type,
            'tone':        tone,
            'imageSource': image_source,
            'template':    template,
            'musicVibe':   music_vibe,
            'chatId':      chat_id,
            'userId':      user_id,
            'username':    username,
            'secret':      GAS_SECRET,
        }, timeout=30)
        data = resp.json()
        if data.get('status') != 'queued':
            raise Exception(data.get('error') or resp.text[:200])
    except Exception as e:
        log.exception('GAS content request failed')
        send(chat_id, f'❌ Ошибка запуска генерации:\n<code>{e}</code>')
        _STATE.get(user_id, {}).update(step='idle')


def _send_to_gas_media(chat_id, user_id, username, row_id, post_type):
    """Steps ②③④ — generate images, overlays, reel."""
    if not GAS_WEBAPP_URL:
        send(chat_id, '❌ GAS_WEBAPP_URL не настроен.')
        return
    try:
        resp = requests.post(GAS_WEBAPP_URL, json={
            'action':   'generate_media',
            'rowId':    row_id,
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
        log.exception('GAS media request failed')
        send(chat_id, f'❌ Ошибка запуска генерации изображений:\n<code>{e}</code>')
        _STATE.get(user_id, {}).update(step='idle')


# ── GAS callback ──────────────────────────────────────────────

def _send_content_preview(chat_id, body, row_id):
    """Format and send content preview with confirm/regen/new buttons."""
    hook      = body.get('hook', '')
    slides    = body.get('slides', [])
    caption   = body.get('caption', '')
    post_type = body.get('postType', '')
    type_lbl  = POST_TYPE_MAP.get(post_type, post_type)

    slides_fmt = ''
    for i, s in enumerate(slides):
        if '|' in s:
            head, sub = s.split('|', 1)
            slides_fmt += f'\n<b>{i+1}. {head.strip()}</b>\n<i>{sub.strip()}</i>'
        else:
            slides_fmt += f'\n{i+1}. {s}'

    # Send hook + slides as one message, full caption as a second message
    # (Telegram has a 4096-char limit per message)
    msg1 = (
        f'✅ <b>Контент готов!</b> ({type_lbl})\n\n'
        f'🎣 <b>Hook:</b>\n{hook}\n\n'
        f'📋 <b>Слайды:</b>{slides_fmt}'
    )
    send(chat_id, msg1[:4090])

    # Full caption in a separate message so nothing is truncated
    hashtags = body.get('hashtags', '')
    caption_msg = f'📝 <b>Подпись:</b>\n{caption}\n\n🏷 <b>Хэштеги:</b>\n{hashtags}'
    send(chat_id, caption_msg[:4090], reply_markup=_confirm_kb(row_id))


@app.route('/gas_callback', methods=['POST'])
def gas_callback():
    body = request.get_json(force=True, silent=True) or {}

    if GAS_SECRET and body.get('secret') != GAS_SECRET:
        return jsonify(ok=False, error='forbidden'), 403

    event     = body.get('event', '')
    chat_id   = body.get('chatId')
    row_id    = str(body.get('rowId', ''))
    post_type = body.get('postType', '')
    user_id   = body.get('userId')

    if not chat_id:
        return jsonify(ok=False, error='missing chatId'), 400

    log.info('gas_callback event=%s chatId=%s rowId=%s', event, chat_id, row_id)

    if event == 'content_ready':
        # Save for /last command
        if user_id:
            _LAST[user_id] = body

        # Update state with row_id
        if user_id and user_id in _STATE:
            _STATE[user_id]['row_id']  = row_id
            _STATE[user_id]['step']    = 'awaiting_confirm'

        _send_content_preview(chat_id, body, row_id)

    elif event == 'images_ready':
        thumb_urls = body.get('thumbUrls', [])
        if len(thumb_urls) == 1:
            send_photo(chat_id, thumb_urls[0],
                       caption='🖼️ Готовые слайды:',
                       reply_markup=_review_kb(row_id))
        elif len(thumb_urls) > 1:
            send_media_group(chat_id, thumb_urls, caption='🖼️ Готовые слайды:')
            send(chat_id, '👆 Проверь слайды:', reply_markup=_review_kb(row_id))
        else:
            send(chat_id, '✅ Слайды готовы.', reply_markup=_review_kb(row_id))

    elif event == 'reel_ready':
        video_b64 = body.get('videoB64', '')
        video_url = body.get('videoUrl', '')
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
            send(chat_id, '🎬 Рилс готов! Проверь папку Google Drive.',
                 reply_markup=_review_kb(row_id))

    elif event == 'error':
        msg = body.get('message', 'Неизвестная ошибка')
        send(chat_id, f'❌ <b>Ошибка:</b>\n<code>{msg}</code>')

    return jsonify(ok=True)


# ── Utility routes ────────────────────────────────────────────

@app.route('/health')
def health():
    return jsonify(status='ok', states=len(_STATE))


@app.route('/set_webhook')
def set_webhook():
    base = request.args.get('url', '').rstrip('/')
    if not base:
        return 'Pass ?url=https://your-domain.onrender.com', 400
    payload = {'url': f'{base}/webhook'}
    if WEBHOOK_SECRET:
        payload['secret_token'] = WEBHOOK_SECRET
    r = requests.post(f'{TELEGRAM_API}/setWebhook', json=payload, timeout=10)
    return jsonify(r.json())


@app.route('/webhook_info')
def webhook_info():
    r = requests.get(f'{TELEGRAM_API}/getWebhookInfo', timeout=10)
    return jsonify(r.json())


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)
