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
REDIS_URL           = os.environ.get('REDIS_URL', '')
UPSTASH_REDIS_URL   = os.environ.get('UPSTASH_REDIS_REST_URL', '')
UPSTASH_REDIS_TOKEN = os.environ.get('UPSTASH_REDIS_REST_TOKEN', '')
TELEGRAM_API        = f'https://api.telegram.org/bot{BOT_TOKEN}'

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

app = Flask(__name__)

# ── Redis (optional) ──────────────────────────────────────────
# Supports two backends:
#   1. REDIS_URL (redis-py / any Redis)
#   2. UPSTASH_REDIS_REST_URL + UPSTASH_REDIS_REST_TOKEN (Upstash REST API — no redis-py needed)
# Falls back to in-memory if neither is configured.

_redis      = None   # redis-py client
_upstash    = None   # (base_url, token) tuple for REST API

if REDIS_URL:
    try:
        import redis as _redis_lib
        _redis = _redis_lib.from_url(REDIS_URL, decode_responses=True, socket_timeout=2)
        _redis.ping()
        log.info('Redis (redis-py) connected')
    except Exception as _re:
        log.warning('Redis unavailable (%s) — falling back to in-memory', _re)
        _redis = None

if not _redis and UPSTASH_REDIS_URL and UPSTASH_REDIS_TOKEN:
    _upstash = (UPSTASH_REDIS_URL.rstrip('/'), UPSTASH_REDIS_TOKEN)
    log.info('Upstash REST API configured: %s', UPSTASH_REDIS_URL[:50])


def _kv_get(key: str):
    """Get a string value from whichever KV backend is active."""
    if _redis:
        return _redis.get(key)
    if _upstash:
        base, token = _upstash
        try:
            r = requests.get(f'{base}/get/{key}',
                             headers={'Authorization': f'Bearer {token}'}, timeout=3)
            data = r.json()
            return data.get('result')  # None if key missing
        except Exception as e:
            log.warning('Upstash GET %s: %s', key, e)
    return None


def _kv_setex(key: str, ttl: int, value: str):
    """Set a key with TTL (seconds)."""
    if _redis:
        _redis.setex(key, ttl, value)
        return
    if _upstash:
        base, token = _upstash
        try:
            requests.post(f'{base}/set/{key}/{requests.utils.quote(value, safe="")}',
                          params={'ex': ttl},
                          headers={'Authorization': f'Bearer {token}'}, timeout=3)
        except Exception as e:
            log.warning('Upstash SET %s: %s', key, e)


def _kv_delete(key: str):
    """Delete a key."""
    if _redis:
        _redis.delete(key)
        return
    if _upstash:
        base, token = _upstash
        try:
            requests.post(f'{base}/del/{key}',
                          headers={'Authorization': f'Bearer {token}'}, timeout=3)
        except Exception as e:
            log.warning('Upstash DEL %s: %s', key, e)


def _kv_scan(pattern: str):
    """Return all keys matching a pattern (best-effort)."""
    if _redis:
        return list(_redis.scan_iter(pattern))
    if _upstash:
        base, token = _upstash
        keys = []
        cursor = 0
        try:
            while True:
                r = requests.get(f'{base}/scan/{cursor}',
                                 params={'match': pattern, 'count': 200},
                                 headers={'Authorization': f'Bearer {token}'}, timeout=5)
                result = r.json().get('result', [cursor + 1, []])
                cursor = int(result[0])
                keys.extend(result[1])
                if cursor == 0:
                    break
        except Exception as e:
            log.warning('Upstash SCAN %s: %s', pattern, e)
        return keys
    return []


def _kv_active():
    return bool(_redis or _upstash)


class PersistentDict:
    """
    Dict-like object that mirrors every write to Redis (if available).
    Falls back to pure in-memory if Redis is not configured.
    Keys are always integers (Telegram user IDs).
    Values are JSON-serialisable dicts.
    TTL: 7 days per entry.
    """
    _TTL = 7 * 24 * 3600  # 7 days

    def __init__(self, prefix: str):
        self._d: dict = {}
        self._prefix = prefix

    # ── dict interface ──────────────────────────────────────────
    def __setitem__(self, key, value):
        self._d[key] = value
        if _kv_active():
            try:
                _kv_setex(f'{self._prefix}:{key}', self._TTL, json.dumps(value, default=str))
            except Exception as e:
                log.warning('KV write [%s:%s]: %s', self._prefix, key, e)

    def __getitem__(self, key):
        return self._d[key]

    def __contains__(self, key):
        return key in self._d

    def __len__(self):
        return len(self._d)

    def get(self, key, default=None):
        return self._d.get(key, default)

    def pop(self, key, *args):
        if _kv_active():
            try:
                _kv_delete(f'{self._prefix}:{key}')
            except Exception as e:
                log.warning('KV delete [%s:%s]: %s', self._prefix, key, e)
        return self._d.pop(key, *args)

    def update_key(self, key, patch: dict):
        """Update a subset of fields without replacing the whole entry."""
        current = self._d.get(key, {})
        current.update(patch)
        self[key] = current

    # ── persistence ─────────────────────────────────────────────
    def load(self):
        """Load all entries from KV store into memory (called once at startup)."""
        if not _kv_active():
            return
        try:
            for rkey in _kv_scan(f'{self._prefix}:*'):
                try:
                    uid   = int(rkey.split(':', 1)[1])
                    raw   = _kv_get(rkey)
                    if raw:
                        self._d[uid] = json.loads(raw)
                except Exception:
                    pass
            log.info('Loaded %d %s entries from KV', len(self._d), self._prefix)
        except Exception as e:
            log.warning('KV load [%s]: %s', self._prefix, e)


# ── State ─────────────────────────────────────────────────────
# { user_id: { step, topics, idea, post_type, tone, image_source,
#              chat_id, username, row_id, last_content } }
_STATE = PersistentDict('state')
_LAST  = PersistentDict('last')   # { user_id: last content_ready payload }


# ── Bot commands list ─────────────────────────────────────────
BOT_COMMANDS = [
    ('start',  'Сбросить и начать заново'),
    ('cancel', 'Отменить текущую генерацию'),
    ('reset',  'Алиас для /cancel'),
    ('last',   'Показать последний сгенерированный контент'),
    ('status', 'Статус текущей операции'),
    ('help',   'Список всех команд'),
]


def _register_commands():
    """Register bot commands with Telegram so / autocomplete works."""
    if not BOT_TOKEN:
        return
    try:
        r = requests.post(
            f'{TELEGRAM_API}/setMyCommands',
            json={'commands': [{'command': c, 'description': d} for c, d in BOT_COMMANDS]},
            timeout=10,
        )
        result = r.json()
        if result.get('ok'):
            log.info('Bot commands registered (%d)', len(BOT_COMMANDS))
        else:
            log.warning('setMyCommands failed: %s', result)
    except Exception as e:
        log.warning('setMyCommands error: %s', e)

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

_DEFAULT_TEMPLATES = [
    'Gold Classic', 'Dark Mystery', 'Celestial Blue', 'Rose Gold',
    'Crimson Power', 'Snow White', 'Slate Pro', 'Emerald Elite',
]
_TEMPLATE_CACHE: list = []   # refreshed from GAS on first use
_TEMPLATE_CACHE_AT: float = 0

def _get_templates() -> list:
    """Return template names, refreshing from GAS at most once per hour."""
    import time
    global _TEMPLATE_CACHE, _TEMPLATE_CACHE_AT
    if _TEMPLATE_CACHE and (time.time() - _TEMPLATE_CACHE_AT) < 3600:
        return _TEMPLATE_CACHE
    if GAS_WEBAPP_URL:
        try:
            r = requests.get(GAS_WEBAPP_URL,
                             params={'action': 'templates', 'secret': GAS_SECRET},
                             timeout=10)
            names = r.json().get('templates', [])
            if names:
                _TEMPLATE_CACHE    = names
                _TEMPLATE_CACHE_AT = time.time()
                log.info('Templates refreshed: %s', names)
                return _TEMPLATE_CACHE
        except Exception as e:
            log.warning('Could not fetch templates: %s', e)
    _TEMPLATE_CACHE    = _DEFAULT_TEMPLATES
    _TEMPLATE_CACHE_AT = __import__('time').time()
    return _TEMPLATE_CACHE

TEMPLATE_MAP = {}  # populated dynamically

MUSIC_VIBES = [
    ('pixabay',                           'Pixabay 🎵'),
    ('ambient meditation spiritual',      'Медитация 🔮'),
    ('ambient spiritual uplifting',       'Подъём ⚡'),
    ('ambient spiritual relaxing',        'Релакс 🌊'),
    ('ambient spiritual epic',            'Эпик 🎺'),
    ('drive',                             'Моя музыка 🎧'),
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
    templates = _get_templates()
    rows = []
    for i in range(0, len(templates), 2):
        row = []
        for name in templates[i:i+2]:
            row.append({'text': name, 'callback_data': f'tmpl:{name}'})
        rows.append(row)
    return {'inline_keyboard': rows}


def _reel_mode_kb():
    return {'inline_keyboard': [
        [{'text': '🎬 Видео B-roll',   'callback_data': 'reelmode:video'},
         {'text': '🖼️ Слайды (фото)', 'callback_data': 'reelmode:images'}],
    ]}

REEL_MODE_MAP = {'video': '🎬 Видео B-roll', 'images': '🖼️ Слайды (фото)'}

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
        {'text': '✏️ Редактировать',  'callback_data': f'edit:{row_id}'},
        {'text': '✏️ Новая идея',     'callback_data': 'newidea:0'},
    ]]}


def _edit_field_kb(row_id, post_type=''):
    rows = [
        [{'text': '🎣 Hook',      'callback_data': f'editfield:{row_id}:hook'},
         {'text': '📋 Слайды',   'callback_data': f'editfield:{row_id}:slides'}],
        [{'text': '📝 Подпись',  'callback_data': f'editfield:{row_id}:caption'},
         {'text': '🏷 Хэштеги', 'callback_data': f'editfield:{row_id}:hashtags'}],
        [{'text': '« Назад',     'callback_data': f'editback:{row_id}'}],
    ]
    # Story has no caption/hashtags
    if post_type == 'story':
        rows = [rows[0], rows[-1]]
    return {'inline_keyboard': rows}


EDIT_FIELD_LABELS = {
    'hook'     : '🎣 Hook',
    'slides'   : '📋 Слайды',
    'caption'  : '📝 Подпись',
    'hashtags' : '🏷 Хэштеги',
}
EDIT_FIELD_HINTS = {
    'hook'    : 'Отправь новый <b>hook</b> (первая фраза, цепляющая внимание):',
    'slides'  : ('Отправь новые <b>слайды</b>.\n'
                 'Формат каждого слайда: <code>ЗАГОЛОВОК|подзаголовок</code>\n'
                 'Слайды разделяй строкой <code>---</code>'),
    'caption' : 'Отправь новую <b>подпись</b> (caption) для поста:',
    'hashtags': 'Отправь новые <b>хэштеги</b> (через пробел или с новой строки):',
}


def _regen_menu_kb(row_id):
    """Sub-menu shown when user taps Regenerate."""
    return {'inline_keyboard': [
        [{'text': '🖼️ Только изображения',     'callback_data': f'regen_images:{row_id}'},
         {'text': '📝 Только контент',          'callback_data': f'regen_content:{row_id}'}],
        [{'text': '🎨 Новый шаблон',            'callback_data': f'regen_tmpl_pick:{row_id}'},
         {'text': '🎵 Новая музыка',            'callback_data': f'regen_music_pick:{row_id}'}],
        [{'text': '🔄 Всё заново',              'callback_data': f'regen_all:{row_id}'}],
        [{'text': '« Назад',                    'callback_data': f'regen_cancel:{row_id}'}],
    ]}


def _regen_tmpl_kb(row_id):
    """Template picker for regen — appends row_id as suffix."""
    templates = _get_templates()
    rows = []
    for i in range(0, len(templates), 2):
        row = []
        for name in templates[i:i+2]:
            row.append({'text': name, 'callback_data': f'regen_set_tmpl:{row_id}:{name}'})
        rows.append(row)
    return {'inline_keyboard': rows}


def _regen_music_kb(row_id):
    """Music picker for regen."""
    rows = []
    for i in range(0, len(MUSIC_VIBES), 2):
        row = []
        for code, lbl in MUSIC_VIBES[i:i+2]:
            row.append({'text': lbl, 'callback_data': f'regen_set_music:{row_id}:{code}'})
        rows.append(row)
    return {'inline_keyboard': rows}


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

    if text in ('/cancel', '/reset'):
        prev_state = _STATE.get(user_id, {})
        prev_step  = prev_state.get('step', '')
        if prev_step == 'awaiting_edit':
            # Cancel edit mode — clean up helper messages and go back to confirm
            val_msg = prev_state.get('edit_val_msg')
            fr_msg  = prev_state.get('edit_fr_msg')
            if val_msg: _tg('deleteMessage', {'chat_id': chat_id, 'message_id': val_msg})
            if fr_msg:  _tg('deleteMessage', {'chat_id': chat_id, 'message_id': fr_msg})
            _STATE[user_id] = {**prev_state, 'step': 'awaiting_confirm',
                               'edit_field': None, 'edit_val_msg': None, 'edit_fr_msg': None}
            send(chat_id, '❌ Редактирование отменено. Нажми ✅ чтобы продолжить или выбери другое поле.')
            return
        _STATE.pop(user_id, None)
        if prev_step in ('generating_media', 'generating_content'):
            send(chat_id,
                 '❌ <b>Отменено.</b>\n\n'
                 'Генерация в Google Apps Script может ещё идти 1–2 минуты в фоне. '
                 'Подожди немного перед новой идеей — иначе получишь ошибку "занят".\n\n'
                 'Напиши идею когда будешь готов.')
        else:
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

    # ── Awaiting field edit ───────────────────────────────────
    current_step = _STATE.get(user_id, {}).get('step', 'idle')
    if current_step == 'awaiting_edit':
        state     = _STATE.get(user_id, {})
        row_id    = state.get('row_id', '')
        field     = state.get('edit_field', '')
        post_type = state.get('post_type', '')
        val_msg   = state.get('edit_val_msg')
        fr_msg    = state.get('edit_fr_msg')

        if not row_id or not field:
            _STATE[user_id]['step'] = 'awaiting_confirm'
            send(chat_id, '❌ Потеряна ссылка на контент. Начни заново.')
            return

        # Clean up the code-block and force-reply helper messages
        if val_msg:
            _tg('deleteMessage', {'chat_id': chat_id, 'message_id': val_msg})
        if fr_msg:
            _tg('deleteMessage', {'chat_id': chat_id, 'message_id': fr_msg})

        # Normalise slides: accept "---" as separator
        new_value = text.strip()
        if field == 'slides':
            new_value = '\n---\n'.join(
                p.strip() for p in new_value.replace('\n---\n', '|||').replace('---', '|||').split('|||')
                if p.strip()
            )

        _STATE[user_id] = {**state, 'step': 'awaiting_confirm',
                           'edit_field': None, 'edit_val_msg': None, 'edit_fr_msg': None}
        send(chat_id, '⏳ <i>Сохраняю правки…</i>')
        _send_field_update(chat_id, user_id, row_id, field, new_value, post_type)
        return

    # ── Block new idea if generation is already in progress ───
    _BUSY_STEPS = {'generating_content', 'generating_media', 'awaiting_confirm',
                   'topics_shown', 'type_shown', 'reelmode_shown', 'tone_shown',
                   'source_shown', 'template_shown', 'music_shown'}
    current_step = _STATE.get(user_id, {}).get('step', 'idle')
    if current_step in _BUSY_STEPS:
        idea_in_progress = _STATE[user_id].get('idea', '…')
        step_labels = {
            'generating_content': '⏳ генерирую контент',
            'generating_media':   '⏳ генерирую изображения',
            'awaiting_confirm':   '✅ жду твоего подтверждения',
            'topics_shown':       '💡 жду выбора темы',
            'type_shown':         '📋 жду выбора типа поста',
            'tone_shown':         '🎨 жду выбора тона',
            'source_shown':       '📸 жду выбора источника изображений',
            'template_shown':     '🖼️ жду выбора шаблона',
            'music_shown':        '🎵 жду выбора музыки',
        }
        step_label = step_labels.get(current_step, current_step)
        send(chat_id,
             f'⚙️ <b>Уже идёт генерация!</b>\n\n'
             f'Тема: <i>{idea_in_progress}</i>\n'
             f'Статус: {step_label}\n\n'
             f'Дождись завершения или нажми /cancel чтобы отменить и начать заново.')
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
        if post_type == 'reel':
            # Extra step for reels: choose video B-roll or still images
            _STATE[user_id] = {**state, 'step': 'reelmode_shown', 'post_type': post_type}
            edit_msg(chat_id, msg_id,
                     f'✅ <b>Тема:</b> {idea}\n<b>Тип:</b> {POST_TYPE_MAP.get(post_type, post_type)}\n\n'
                     f'<b>Режим рилса:</b>',
                     reply_markup=_reel_mode_kb())
        else:
            _STATE[user_id] = {**state, 'step': 'tone_shown', 'post_type': post_type}
            edit_msg(chat_id, msg_id,
                     f'✅ <b>Тема:</b> {idea}\n<b>Тип:</b> {POST_TYPE_MAP.get(post_type, post_type)}\n\n'
                     f'<b>Тон и стиль:</b>',
                     reply_markup=_tone_kb())

    # ── Reel mode selected ─────────────────────────────────────
    elif data.startswith('reelmode:'):
        reel_mode = data.split(':', 1)[1]
        idea      = state.get('idea', '')
        post_type = state.get('post_type', 'reel')
        _STATE[user_id] = {**state, 'step': 'tone_shown', 'reel_mode': reel_mode}
        edit_msg(chat_id, msg_id,
                 f'✅ <b>Тема:</b> {idea}\n'
                 f'<b>Тип:</b> {POST_TYPE_MAP.get(post_type, post_type)}\n'
                 f'<b>Режим:</b> {REEL_MODE_MAP.get(reel_mode, reel_mode)}\n\n'
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
                     f'<b>Шаблон:</b> {template}\n\n'
                     f'<b>Фоновая музыка:</b>',
                     reply_markup=_music_kb())
        else:
            edit_msg(chat_id, msg_id,
                     f'✅ <b>Тема:</b> {idea}\n'
                     f'<b>Тип:</b> {POST_TYPE_MAP.get(post_type, post_type)}\n'
                     f'<b>Тон:</b> {TONE_MAP.get(tone, tone)}\n'
                     f'<b>Источник:</b> {IMAGE_SOURCE_MAP.get(source, source)}\n'
                     f'<b>Шаблон:</b> {template}\n\n'
                     f'⏳ <i>Генерирую контент…</i>')
            _send_to_gas_content(chat_id, user_id, username, idea, post_type, tone, source, template, reel_mode=state.get('reel_mode', ''))

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
                 f'<b>Шаблон:</b> {template}\n'
                 f'<b>Музыка:</b> {MUSIC_VIBE_MAP.get(music_vibe, music_vibe)}\n\n'
                 f'⏳ <i>Генерирую контент…</i>')
        _send_to_gas_content(chat_id, user_id, username, idea, post_type, tone, source, template, music_vibe, reel_mode=state.get('reel_mode', ''))

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

    # ── Edit content ───────────────────────────────────────────
    elif data.startswith('edit:'):
        row_id    = data.split(':', 1)[1]
        post_type = state.get('post_type', '')
        _STATE[user_id] = {**state, 'row_id': row_id}
        edit_msg(chat_id, msg_id,
                 cq['message'].get('text', '') + '\n\n<b>Что хочешь изменить?</b>',
                 reply_markup=_edit_field_kb(row_id, post_type))

    elif data.startswith('editback:'):
        row_id = data.split(':', 1)[1]
        _STATE[user_id] = {**state, 'step': 'awaiting_confirm'}
        edit_msg(chat_id, msg_id,
                 cq['message'].get('text', '').split('\n\n<b>Что хочешь изменить?</b>')[0],
                 reply_markup=_confirm_kb(row_id))

    elif data.startswith('editcancel:'):
        # editcancel:{row_id}:{picker_msg_id}
        parts          = data.split(':', 2)
        row_id         = parts[1] if len(parts) > 1 else ''
        picker_msg_id  = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else msg_id
        # Clean up the code-block and force-reply messages if we know their IDs
        val_msg  = state.get('edit_val_msg')
        fr_msg   = state.get('edit_fr_msg')
        if val_msg:
            _tg('deleteMessage', {'chat_id': chat_id, 'message_id': val_msg})
        if fr_msg:
            _tg('deleteMessage', {'chat_id': chat_id, 'message_id': fr_msg})
        # Restore confirm keyboard on the picker message
        _STATE[user_id] = {**state, 'step': 'awaiting_confirm',
                           'edit_field': None, 'edit_val_msg': None, 'edit_fr_msg': None}
        post_type = state.get('post_type', '')
        edit_msg(chat_id, picker_msg_id,
                 f'✏️ <b>Редактирование отменено.</b>\n\n<b>Что хочешь изменить?</b>',
                 reply_markup=_edit_field_kb(row_id, post_type))

    elif data.startswith('editfield:'):
        parts  = data.split(':', 2)
        row_id = parts[1] if len(parts) > 1 else ''
        field  = parts[2] if len(parts) > 2 else ''

        # Read current value from last known content
        last   = _LAST.get(user_id, {})
        if field == 'hook':
            cur_val = last.get('hook', '')
        elif field == 'slides':
            slides = last.get('slides', [])
            cur_val = '\n---\n'.join(slides)
        elif field == 'caption':
            cur_val = last.get('caption', '')
        elif field == 'hashtags':
            cur_val = last.get('hashtags', '')
        else:
            cur_val = ''

        lbl = EDIT_FIELD_LABELS.get(field, field)

        # Edit the picker message to show current value + cancel button
        edit_msg(chat_id, msg_id,
                 f'✏️ <b>Редактирую: {lbl}</b>\n\n'
                 f'<b>Текущий текст</b> (скопируй, измени и отправь):',
                 reply_markup={'inline_keyboard': [[
                     {'text': '❌ Отмена', 'callback_data': f'editcancel:{row_id}:{msg_id}'}
                 ]]})

        # Send the current value as a standalone code block — one tap copies on mobile
        val_msg = _tg('sendMessage', {
            'chat_id'    : chat_id,
            'text'       : f'<code>{cur_val}</code>' if cur_val else '<i>(пусто)</i>',
            'parse_mode' : 'HTML',
        })
        val_msg_id = (val_msg.get('result') or {}).get('message_id')

        # Force-reply prompt so Telegram auto-opens the reply field
        fr_msg = _tg('sendMessage', {
            'chat_id'      : chat_id,
            'text'         : f'⬆️ Скопируй текст выше, измени и отправь в ответ.\n<i>/cancel — отмена</i>',
            'parse_mode'   : 'HTML',
            'reply_markup' : json.dumps({'force_reply': True, 'selective': True}),
        })
        fr_msg_id = (fr_msg.get('result') or {}).get('message_id')

        _STATE[user_id] = {
            **state,
            'step'          : 'awaiting_edit',
            'row_id'        : row_id,
            'edit_field'    : field,
            'edit_msg_id'   : msg_id,       # picker message (has cancel button)
            'edit_val_msg'  : val_msg_id,   # code block message (to delete after submit)
            'edit_fr_msg'   : fr_msg_id,    # force-reply message (to delete after submit)
        }

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
        _send_to_gas_content(chat_id, user_id, username, idea, post_type, tone, source, template, music_vibe, reel_mode=state.get('reel_mode', ''))

    # ── Regen → show sub-menu ──────────────────────────────────
    elif data.startswith('regen:'):
        row_id = data.split(':', 1)[1]
        # Ensure post_type stays 'reel' — don't let it default to 'carousel'
        pt = state.get('post_type') or 'reel'
        _STATE[user_id] = {**state, 'row_id': row_id, 'post_type': pt}
        edit_msg(chat_id, msg_id,
                 cq['message'].get('text', '') + '\n\n<b>Что регенерировать?</b>',
                 reply_markup=_regen_menu_kb(row_id))

    # ── Regen sub-options ──────────────────────────────────────

    # Images only — re-run steps ②③ keeping existing content
    elif data.startswith('regen_images:'):
        row_id    = data.split(':', 1)[1]
        post_type = state.get('post_type', 'carousel')
        _STATE[user_id] = {**state, 'step': 'generating_media'}
        edit_msg(chat_id, msg_id,
                 cq['message'].get('text', '').split('\n\nЧто регенерировать?')[0] +
                 '\n\n⏳ <i>Регенерирую изображения…</i>')
        _send_to_gas_media(chat_id, user_id, username, row_id, post_type)

    # Content only — re-run step ①
    elif data.startswith('regen_content:'):
        row_id     = data.split(':', 1)[1] if ':' in data else ''
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
        _send_to_gas_content(chat_id, user_id, username, idea, post_type, tone, source, template, music_vibe, reel_mode=state.get('reel_mode', ''))

    # New template picker
    elif data.startswith('regen_tmpl_pick:'):
        row_id = data.split(':', 1)[1]
        _STATE[user_id] = {**state, 'row_id': row_id}
        edit_msg(chat_id, msg_id,
                 cq['message'].get('text', '').split('\n\nЧто регенерировать?')[0] +
                 '\n\n<b>Выбери новый шаблон:</b>',
                 reply_markup=_regen_tmpl_kb(row_id))

    # Template selected → re-run overlay only
    elif data.startswith('regen_set_tmpl:'):
        parts     = data.split(':', 2)
        row_id    = parts[1] if len(parts) > 1 else ''
        new_tmpl  = parts[2] if len(parts) > 2 else 'Gold Classic'
        post_type = state.get('post_type', 'carousel')
        _STATE[user_id] = {**state, 'step': 'generating_media', 'template': new_tmpl}
        edit_msg(chat_id, msg_id,
                 f'🎨 <b>Новый шаблон:</b> {new_tmpl}\n\n⏳ <i>Применяю и регенерирую слайды…</i>')
        _send_to_gas(chat_id, user_id, username, {
            'action':    'regen_overlay',
            'rowId':     row_id,
            'template':  new_tmpl,
            'postType':  post_type,
        })

    # New music picker
    elif data.startswith('regen_music_pick:'):
        row_id = data.split(':', 1)[1]
        _STATE[user_id] = {**state, 'row_id': row_id}
        edit_msg(chat_id, msg_id,
                 cq['message'].get('text', '').split('\n\nЧто регенерировать?')[0] +
                 '\n\n<b>Выбери новую музыку:</b>',
                 reply_markup=_regen_music_kb(row_id))

    # Music selected → re-submit reel to Render with new music
    elif data.startswith('regen_set_music:'):
        parts      = data.split(':', 2)
        row_id     = parts[1] if len(parts) > 1 else ''
        music_vibe = parts[2] if len(parts) > 2 else ''
        post_type  = state.get('post_type', 'reel')
        _STATE[user_id] = {**state, 'step': 'generating_media', 'music_vibe': music_vibe}
        lbl = MUSIC_VIBE_MAP.get(music_vibe, music_vibe)
        edit_msg(chat_id, msg_id,
                 f'🎵 <b>Новая музыка:</b> {lbl}\n\n⏳ <i>Пересобираю рилс…</i>')
        _send_to_gas(chat_id, user_id, username, {
            'action':    'regen_music',
            'rowId':     row_id,
            'musicVibe': music_vibe,
            'postType':  post_type,
        })

    # Full regen
    elif data.startswith('regen_all:'):
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
                 f'🔄 <b>Полная регенерация…</b>\n<b>Тема:</b> {idea}\n\n<i>Подожди 3–5 минут.</i>')
        _send_to_gas_content(chat_id, user_id, username, idea, post_type, tone, source, template, music_vibe, reel_mode=state.get('reel_mode', ''))

    # Cancel regen menu — restore review buttons
    elif data.startswith('regen_cancel:'):
        row_id = data.split(':', 1)[1]
        edit_msg(chat_id, msg_id,
                 cq['message'].get('text', '').split('\n\nЧто регенерировать?')[0],
                 reply_markup=_review_kb(row_id))

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
                         image_source, template='Gold Classic', music_vibe='', reel_mode=''):
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
            'reelMode':    reel_mode,
            'chatId':      chat_id,
            'userId':      user_id,
            'username':    username,
            'secret':      GAS_SECRET,
        }, timeout=30)
        data = resp.json()
        if resp.status_code == 409 or 'busy' in (data.get('error') or '').lower():
            # GAS still processing a previous request — reset state, ask user to retry
            _STATE.pop(user_id, None)
            send(chat_id,
                 '⏳ <b>GAS ещё занят предыдущей генерацией.</b>\n\n'
                 'Подожди 30–60 секунд и напиши идею снова.')
            return
        if data.get('status') != 'queued':
            raise Exception(data.get('error') or resp.text[:200])
    except Exception as e:
        log.exception('GAS content request failed')
        send(chat_id, f'❌ Ошибка запуска генерации:\n<code>{e}</code>')
        _STATE.get(user_id, {}).update(step='idle')


def _send_field_update(chat_id, user_id, row_id, field, new_value, post_type):
    """Send an edited field value to GAS, then re-display the content preview."""
    if not GAS_WEBAPP_URL:
        send(chat_id, '❌ GAS_WEBAPP_URL не настроен.')
        return
    try:
        resp = requests.post(GAS_WEBAPP_URL, json={
            'action'   : 'update_content',
            'rowId'    : row_id,
            'field'    : field,
            'value'    : new_value,
            'postType' : post_type,
            'chatId'   : chat_id,
            'userId'   : user_id,
            'secret'   : GAS_SECRET,
        }, timeout=30)
        data = resp.json()
        if data.get('status') == 'ok':
            # GAS returns updated content — refresh _LAST and re-show preview
            body = data.get('content', {})
            body['postType'] = post_type
            if not body.get('rowId'):
                body['rowId'] = row_id
            # Update _LAST so next edit shows the freshly saved value
            if user_id:
                _LAST[user_id] = {**(_LAST.get(user_id) or {}), **body}
            send(chat_id, f'✅ <b>{EDIT_FIELD_LABELS.get(field, field)}</b> обновлён.')
            _send_content_preview(chat_id, body, row_id)
        else:
            raise Exception(data.get('error') or resp.text[:200])
    except Exception as e:
        log.exception('GAS field update failed')
        send(chat_id, f'❌ Не удалось сохранить правки:\n<code>{e}</code>')
        if user_id in _STATE:
            _STATE[user_id]['step'] = 'awaiting_confirm'


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


def _send_to_gas(chat_id, user_id, username, payload: dict):
    """Generic GAS POST — for regen_overlay, regen_music, etc."""
    if not GAS_WEBAPP_URL:
        send(chat_id, '❌ GAS_WEBAPP_URL не настроен.')
        return
    payload.update({'chatId': chat_id, 'userId': user_id, 'username': username, 'secret': GAS_SECRET})
    try:
        resp = requests.post(GAS_WEBAPP_URL, json=payload, timeout=30)
        data = resp.json()
        if data.get('status') != 'queued':
            raise Exception(data.get('error') or resp.text[:200])
    except Exception as e:
        log.exception('GAS request failed [%s]', payload.get('action'))
        send(chat_id, f'❌ Ошибка:\n<code>{e}</code>')


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

    is_story = post_type == 'story'
    if is_story:
        # Story has no caption/hashtags — content lives on the images
        send(chat_id, msg1[:4090], reply_markup=_confirm_kb(row_id))
    else:
        send(chat_id, msg1[:4090])
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
        # Save post_type + reel_mode in state so regen works after delivery
        if user_id:
            st = _STATE.get(user_id, {})
            _STATE[user_id] = {
                **st,
                'step'      : 'awaiting_confirm',
                'row_id'    : row_id,
                'post_type' : st.get('post_type', 'reel'),   # keep 'reel'
                'reel_mode' : st.get('reel_mode', ''),
                'chat_id'   : chat_id,
            }

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
        send(chat_id, f'❌ <b>Ошибка:</b>\n<code>{msg}</code>\n\nНапиши новую идею чтобы начать заново.')
        if user_id and user_id in _STATE:
            _STATE[user_id]['step'] = 'idle'

    return jsonify(ok=True)


# ── Utility routes ────────────────────────────────────────────

@app.route('/health')
def health():
    backend = 'redis-py' if _redis else ('upstash-rest' if _upstash else 'memory')
    return jsonify(status='ok', states=len(_STATE), kv_backend=backend)


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


@app.route('/set_commands')
def set_commands():
    """Re-register bot commands (/ autocomplete menu). Call once after deploy."""
    _register_commands()
    return jsonify(ok=True, commands=[c for c, _ in BOT_COMMANDS])


# ── Startup ───────────────────────────────────────────────────
def _on_startup():
    _STATE.load()
    _LAST.load()
    _register_commands()

threading.Thread(target=_on_startup, daemon=True).start()


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)
