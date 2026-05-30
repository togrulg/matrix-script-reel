"""
ai.py — Groq-powered topic expansion for the Matrix Script Telegram bot.
"""
import os
import json
import requests

GROQ_API_KEY  = os.environ.get('GROQ_API_KEY', '')
GROQ_ENDPOINT = 'https://api.groq.com/openai/v1/chat/completions'
GROQ_MODEL    = 'llama-3.3-70b-versatile'

SYSTEM_PROMPT = """Ты — эксперт по контент-стратегии для Instagram-аккаунта в нише \
«Матрица Судьбы», нумерология, эзотерика, личностный рост. \
Аудитория — русскоязычные женщины 25–45 лет, серьёзно занимающиеся саморазвитием. \
По заданной грубой идее придумай ровно 5 конкретных, цепляющих тем для Instagram-поста или рилса. \
Каждая тема — одно завершённое предложение (максимум 15 слов), которое само по себе уже является \
интригующим заголовком. Темы должны быть разными по углу подачи: вопрос, провокация, история, факт, \
практика. Верни ТОЛЬКО JSON-массив из 5 строк — без пояснений, без markdown, без нумерации."""


def expand_topics(idea: str) -> list:
    """Return 5 specific Instagram topic ideas for the given rough idea."""
    import time
    last_err = None
    for attempt in range(3):
        resp = requests.post(
            GROQ_ENDPOINT,
            json={
                'model':       GROQ_MODEL,
                'messages': [
                    {'role': 'system', 'content': SYSTEM_PROMPT},
                    {'role': 'user',   'content': f'Идея: {idea}'},
                ],
                'max_tokens':  600,
                'temperature': 0.9,
                'stream':      False,
            },
            headers={
                'Authorization': f'Bearer {GROQ_API_KEY}',
                'Content-Type':  'application/json',
            },
            timeout=30,
        )
        if resp.status_code == 429:
            wait = 15 * (attempt + 1)
            print(f"Groq 429 — waiting {wait}s (attempt {attempt+1}/3)", flush=True)
            time.sleep(wait)
            last_err = 'Groq rate limit — подожди минуту и попробуй снова'
            continue
        resp.raise_for_status()
        last_err = None
        break
    if last_err:
        raise RuntimeError(last_err)

    raw = resp.json()['choices'][0]['message']['content'].strip()

    # Strip markdown code fences if the model adds them
    if '```' in raw:
        parts = raw.split('```')
        raw = parts[1] if len(parts) > 1 else parts[0]
        if raw.lower().startswith('json'):
            raw = raw[4:]
    raw = raw.strip()

    topics = json.loads(raw)
    if not isinstance(topics, list):
        raise ValueError('Expected a JSON array from Groq')
    return [str(t).strip() for t in topics[:5]]
