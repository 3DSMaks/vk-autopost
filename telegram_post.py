"""Telegram: RSS → Gemini → JPEG → канал. VK-токены не используются.

Темы чередуются через пост: игры, кино, игры, кино… (по числу уже опубликованных новостей).
Если по теме часа свежих новостей нет, берётся другая тема."""
import argparse
import base64
import hashlib
import html
import io
import json
import os
import random
import re
from pathlib import Path
import sys
import time
import urllib.error
import urllib.parse
import uuid
from datetime import datetime, timedelta, timezone

from PIL import Image
import post as news

SEEN_FILE = Path(__file__).with_name('seen_telegram.json')

GAME_FEEDS = [
    'https://stopgame.ru/rss/rss_news.xml',
    'https://www.igromania.ru/rss/news.rss',
    'https://www.playground.ru/rss/news.xml',
    'https://www.gamemag.ru/rss/feed',
    'https://feeds.feedburner.com/ign/games-all',
    'https://www.pcgamer.com/rss/',
]
GAME_PROMPT = (news.PROMPT
               .replace('сообщества ВКонтакте «СтопКадр» о кино и сериалах', 'Telegram-канала «СтопКадр» о кино, сериалах и видеоиграх')
               .replace('именно про кино или сериалы (не игры, не музыку, не политику)', 'именно про видеоигры (не кино, не железо, не киберспортивные результаты, не политику)')
               .replace('громкие премьеры, трейлеры, кастинг, продолжения, сборы и рейтинги', 'анонсы, трейлеры, даты выхода, продолжения и ремейки, оценки и продажи громких игр'))
MOVIE_PROMPT = news.PROMPT.replace('сообщества ВКонтакте', 'Telegram-канала')
TOPICS = {'games': (GAME_FEEDS, GAME_PROMPT), 'cinema': (news.FEEDS, MOVIE_PROMPT)}


def topic_order(published):
    """published — сколько новостей уже вышло в этом канале: чётное → игры, нечётное → кино."""
    return ['games', 'cinema'] if published % 2 == 0 else ['cinema', 'games']


def fresh_for(topic, seen, per_feed=5):
    """Свежие новости темы: не больше per_feed с каждой ленты, чтобы плодовитые сайты не забивали выбор."""
    feeds, _ = TOPICS[topic]
    saved = news.FEEDS
    items = []
    try:
        for feed in feeds:
            news.FEEDS = [feed]
            items += news.fresh_news(seen)[:per_feed]
    finally:
        news.FEEDS = saved
    items.sort(key=lambda n: datetime.fromisoformat(n['date']).timestamp() if n.get('date') else 0, reverse=True)
    items = items[:25]
    for i, n in enumerate(items):
        n['index'] = i
    return items


def multipart(fields, files):
    boundary = 'telegram' + uuid.uuid4().hex
    chunks = []
    for name, value in fields.items():
        chunks.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n').encode())
    for name, data in files.items():
        chunks.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{name}.jpg"\r\nContent-Type: image/jpeg\r\n\r\n').encode() + data + b'\r\n')
    chunks.append(f'--{boundary}--\r\n'.encode())
    return b''.join(chunks), {'Content-Type': f'multipart/form-data; boundary={boundary}'}


def api(method, fields=None, files=None):
    token = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
    if not token:
        raise RuntimeError('Добавьте секрет TELEGRAM_BOT_TOKEN')
    if files:
        body, headers = multipart(fields or {}, files)
    else:
        body = urllib.parse.urlencode(fields or {}).encode()
        headers = {'Content-Type': 'application/x-www-form-urlencoded'}
    # Не печатаем URL: он содержит токен. Не повторяем отправку при неопределённом результате.
    try:
        raw = news.http(f'https://api.telegram.org/bot{token}/{method}', body, headers, 60)
        response = json.loads(raw)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f'Telegram {method}: HTTP {exc.code}. Проверьте токен, канал и права бота.') from None
    except Exception:
        raise RuntimeError(f'Telegram {method}: ошибка сети или ответа. Перед повторной отправкой проверьте канал.') from None
    if not response.get('ok'):
        raise RuntimeError(f'Telegram {method}: код {response.get("error_code", "unknown")}. Проверьте настройки бота.')
    return response['result']


def check_channel():
    chat_id = os.environ.get('TELEGRAM_CHAT_ID', '').strip()
    if not chat_id:
        raise RuntimeError('Добавьте секрет TELEGRAM_CHAT_ID: @имя_канала или числовой ID')
    bot = api('getMe')
    chat = api('getChat', {'chat_id': chat_id})
    if chat.get('type') != 'channel':
        raise RuntimeError('TELEGRAM_CHAT_ID должен указывать на канал')
    member = api('getChatMember', {'chat_id': chat_id, 'user_id': bot['id']})
    if member.get('status') != 'creator' and not (member.get('status') == 'administrator' and member.get('can_post_messages')):
        raise RuntimeError('Добавьте бота администратором канала с правом публикации сообщений')
    print(f'Доступ подтверждён: бот @{bot.get("username", "")} → канал {chat.get("title", "")}')
    return chat_id


# ---------- Gemini: общий вызов с JSON-ответом ----------
def gemini_json(parts, schema, timeout=90):
    """Запрос к Gemini (текст и картинки) со строгой схемой ответа. Возвращает dict или бросает RuntimeError."""
    body = json.dumps({'contents': [{'parts': parts}],
                       'generationConfig': {'responseMimeType': 'application/json', 'responseSchema': schema}}).encode()
    last = None
    for model in [m for m in news.GEMINI_MODELS for _ in range(2)]:
        try:
            raw = news.http(f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent', body,
                            {'Content-Type': 'application/json', 'x-goog-api-key': os.environ['GEMINI_API_KEY']}, timeout)
            res = json.loads(raw)
            got = (res.get('candidates') or [{}])[0].get('content', {}).get('parts', [])
            text = ''.join(p.get('text', '') for p in got if not p.get('thought'))
            return json.loads(re.search(r'\{[\s\S]*\}', text).group(0))
        except urllib.error.HTTPError as exc:
            last = f'{model}: HTTP {exc.code}'
            if exc.code in (429, 500, 503):
                time.sleep(15)
        except Exception as exc:
            last = f'{model}: {type(exc).__name__}'
    raise RuntimeError('Gemini не ответил: ' + str(last))


# ---------- Проверка картинок ----------
def thumb_b64(data, size=512):
    with Image.open(io.BytesIO(data)) as im:
        im = im.convert('RGB')
        im.thumbnail((size, size))
        out = io.BytesIO()
        im.save(out, 'JPEG', quality=80)
    return base64.b64encode(out.getvalue()).decode()


def vet_images(title, images, limit=3):
    """Gemini смотрит картинки и оставляет только относящиеся к новости, без повторов; лучшая — первой.
    Если Gemini недоступен — возвращаем первые картинки как есть."""
    if len(images) <= 1:
        return images[:limit]
    parts = [{'text': (
        f'Заголовок новости: «{title}».\n'
        f'Ниже {len(images)} изображений, по порядку с номерами 0..{len(images) - 1}. '
        'Верни в keep номера только тех, что явно относятся к ЭТОЙ новости: кадры, постеры, скриншоты, обложки, '
        'фото людей из новости. Убери: повторы одного и того же изображения (даже с другим кадрированием, размером '
        'или надписями), логотипы сайтов, рекламу, баннеры, превью других новостей, аватарки, иконки. '
        'Упорядочь от лучшей картинки для обложки поста к худшей.')}]
    for data in images:
        parts.append({'inline_data': {'mime_type': 'image/jpeg', 'data': thumb_b64(data)}})
    schema = {'type': 'OBJECT', 'properties': {'keep': {'type': 'ARRAY', 'items': {'type': 'INTEGER'}}}, 'required': ['keep']}
    try:
        keep = gemini_json(parts, schema, 60).get('keep') or []
    except RuntimeError as exc:
        print('Проверка картинок пропущена:', exc)
        return images[:limit]
    chosen = [images[i] for i in dict.fromkeys(keep) if isinstance(i, int) and 0 <= i < len(images)]
    print(f'Картинки: Gemini оставил {len(chosen)} из {len(images)}')
    return chosen[:limit] or images[:1]


# ---------- Оформление поста для Telegram ----------
STYLES = [
    'СПИСОК: один короткий вводный абзац; затем строка-подзаголовок вроде «Что изменилось:», «Главное:» или «Что известно:»; '
    'затем 3–5 пунктов, каждый с новой строки и начинается с «• ».',
    'ЦИТАТА: вводный абзац; затем короткая прямая цитата из текста источника в <blockquote>«…»</blockquote> '
    '(переведи на русский; только если в источнике есть прямая речь — иначе используй формат АБЗАЦЫ); затем один абзац контекста.',
    'АБЗАЦЫ: 2–3 коротких абзаца; ключевые имена, названия и цифры выдели <b>жирным</b>.',
    'КАРТОЧКА: одна вводная фраза; затем 3–4 строки вида «<b>Дата выхода:</b> …», «<b>Платформы:</b> …», «<b>Режиссёр:</b> …» '
    '(только факты из источника); затем короткий вывод одной фразой.',
]
ALLOWED_TAGS = ('b', 'i', 'u', 's', 'blockquote', 'code')


CONTENT = re.compile(r'''(?:class|itemprop)=["'][^"']*\b(?:content-text|article[-_]?(?:body|text|content)|entry-content|'''
                     r'''news[-_]?text|post[-_]?(?:content|body)|text-content|material-text|articleBody|b-text)\b''', re.I)


def paragraph_groups(page, gap=800):
    """Абзацы <p>, сгруппированные в блоки: соседние абзацы ближе gap символов разметки — один блок."""
    groups, cur, last_end = [], [], None
    for m in re.finditer(r'<p\b[^>]*>([\s\S]*?)</p>', page, re.I):
        text = news.strip(m.group(1))
        if last_end is not None and m.start() - last_end > gap:
            groups.append(cur)
            cur = []
        if len(text) > 40:
            cur.append(text)
        last_end = m.end()
    groups.append(cur)
    return [g for g in groups if g]


def article_text(link, limit=3500):
    """Текст статьи: блок с типичным классом тела статьи (content-text, articleBody…);
    иначе самый «плотный» блок подряд идущих абзацев — как режим чтения в браузере."""
    try:
        page = news.http(link, timeout=15).decode('utf-8', 'ignore')
    except Exception:
        return ''
    page = re.sub(r'<(script|style|noscript)\b[\s\S]*?</\1>', ' ', page, flags=re.I)
    marked = CONTENT.search(page)
    if marked:
        groups = paragraph_groups(page[marked.start():])
        if groups and sum(len(t) for t in groups[0]) > 150:
            return '\n'.join(groups[0])[:limit]
    best = max(paragraph_groups(page), key=lambda g: sum(len(t) for t in g), default=[])
    return '\n'.join(best)[:limit]


def sanitize_tg_html(value):
    """Оставляет только безопасные теги Telegram (b, i, u, s, blockquote, code); при несбалансированных тегах — None."""
    text = html.escape(html.unescape(value or '').strip(), quote=False)
    text = re.sub(r'&lt;(/?)(%s)&gt;' % '|'.join(ALLOWED_TAGS), r'<\1\2>', text)
    stack = []
    for closing, name in re.findall(r'<(/?)(\w+)>', text):
        if not closing:
            stack.append(name)
        elif not stack or stack.pop() != name:
            return None
    return None if stack else text


def visible_len(value):
    return len(html.unescape(re.sub(r'<[^>]+>', '', value)).encode('utf-16-le')) // 2


def tg_format(pick, draft, style=None):
    """Переписывает пост в оформлении Telegram-канала. Возвращает безопасный HTML или None (тогда — обычный текст)."""
    style = style or random.choice(STYLES)
    source = article_text(pick['link']) or pick.get('summary', '')
    prompt = (
        'Ты редактор Telegram-канала «СтопКадр» о кино, сериалах и видеоиграх. Оформи пост на русском по новости.\n'
        f'Формат этого поста — {style}\n'
        'Общие правила:\n'
        '1. Первая строка — заголовок в <b>…</b>: цепляющий, до 80 символов.\n'
        '2. Факты, имена, даты, цифры и цитаты — только из текста источника, ничего не выдумывай. Переводи на естественный русский.\n'
        '3. Весь пост — не длиннее 850 символов. Между блоками — пустая строка.\n'
        '4. Можно 1–2 уместных эмодзи, не больше.\n'
        '5. Последняя строка — 1–2 хэштега на русском или по названию игры/фильма (например #CallOfDuty).\n'
        '6. Разметка — только теги <b>, <i>, <blockquote>. Без ссылок, без HTML-сущностей вроде &amp;.\n\n'
        f'Черновик поста:\n{draft}\n\nТекст источника:\n{source}'
    )
    schema = {'type': 'OBJECT', 'properties': {'post': {'type': 'STRING'}}, 'required': ['post']}
    try:
        post = sanitize_tg_html(gemini_json([{'text': prompt}], schema).get('post', ''))
    except RuntimeError as exc:
        print('Оформление пропущено:', exc)
        return None
    if not post or visible_len(post) > 960:
        print('Оформление не подошло (разметка или длина) — публикую обычный текст')
        return None
    return post


def caption(text, link, formatted=None):
    # Ссылка скрыта под словом «Источник»; лимит считаем консервативно в UTF-16.
    source = '\n\n<a href="' + html.escape(link, quote=True) + '">Источник</a>'
    if formatted:
        return formatted + source
    text = text.rsplit('\n\nИсточник:', 1)[0]
    text = text.encode('utf-16-le')[:980 * 2].decode('utf-16-le', errors='ignore').rstrip()
    return html.escape(text) + source


def looks_like(data):
    """Отпечаток картинки 16×16 (средний хэш): одинаковые фото в разных размерах дают почти один отпечаток."""
    with Image.open(io.BytesIO(data)) as im:
        small = im.convert('L').resize((16, 16))
    pixels = list(small.getdata())
    avg = sum(pixels) / len(pixels)
    return sum(1 << i for i, v in enumerate(pixels) if v > avg)


def pictures(pick):
    urls = news.image_urls(news.page_images(pick['link']) + [pick.get('image')], pick['link'])
    out, hashes, prints = [], set(), []
    for url in urls:
        try:
            data = news.download_jpeg(url, pick['link'])
            with Image.open(io.BytesIO(data)) as im:
                if min(im.size) < 100 or max(im.size) / min(im.size) > 20:
                    continue
            digest = hashlib.sha256(data).digest()
            fp = looks_like(data)
            same = any(bin(fp ^ other).count('1') < 40 for other in prints)  # < 15% различий из 256
            if digest not in hashes and not same and len(data) <= 10 * 1024 * 1024:
                hashes.add(digest)
                prints.append(fp)
                out.append(data)
            if len(out) == 6:
                break
        except Exception:
            print('Кандидат изображения недоступен, пробую следующий')
    return vet_images(pick.get('title', ''), out)


SKIP = 'skip:'  # так в истории помечены новости, к которым не нашлось фото (их больше не предлагаем)


def history_links(seen):
    """Все ссылки из истории — и опубликованные, и пропущенные без фото."""
    return {s[len(SKIP):] if s.startswith(SKIP) else s for s in seen}


def published_count(seen):
    return sum(1 for s in seen if not s.startswith((SKIP, VIDEO)))


# ---------- Трейлеры (только Telegram) ----------
VIDEO = 'video:'          # в истории: video:<дата по Москве>:<ссылка> — пост вышел с трейлером
VIDEO_PER_DAY = 3
YOUTUBE = re.compile(r'(?:youtube(?:-nocookie)?\.com/(?:embed/|watch\?v=|shorts/)|youtu\.be/)([\w-]{11})')
EMBED = re.compile(r'''<iframe[^>]+src=["'][^"']*youtube(?:-nocookie)?\.com/embed/([\w-]{11})''', re.I)


def moscow_today():
    return (datetime.now(timezone.utc) + timedelta(hours=3)).date().isoformat()


def videos_today(seen):
    return sum(1 for s in seen if s.startswith(f'{VIDEO}{moscow_today()}:'))


def trailer_url(link):
    """Первый ролик YouTube со страницы новости (встроенный плеер — в приоритете). None, если ролика нет."""
    try:
        page = news.http(link, timeout=15).decode('utf-8', 'ignore')
    except Exception:
        return None
    m = EMBED.search(page) or YOUTUBE.search(page)
    return f'https://www.youtube.com/watch?v={m.group(1)}' if m else None


def publish_video(chat_id, text, link, video, formatted=None):
    """Пост с большим превью трейлера над текстом (плеер YouTube прямо в Telegram)."""
    body = caption(text, link, formatted) + '\n\n▶️ <a href="' + html.escape(video, quote=True) + '">Смотреть трейлер</a>'
    preview = {'url': video, 'prefer_large_media': True, 'show_above_text': True}
    return api('sendMessage', {'chat_id': chat_id, 'text': body, 'parse_mode': 'HTML',
                               'link_preview_options': json.dumps(preview)})


def pick_with_photos(candidates, attempts=3):
    """Gemini выбирает новость; если к ней нет фото — убираем её и просим выбрать другую.
    Возвращает (pick, text, photos, skipped_links); если фото не нашлось ни разу — photos пустой."""
    pool, skipped = list(candidates), []
    for _ in range(attempts):
        if not pool:
            break
        pick, text = news.write_post(pool)
        photos = pictures(pick)
        if photos:
            return pick, text, photos, skipped
        print('Нет фото к новости, выбираю другую:', pick['link'])
        skipped.append(pick['link'])
        pool = [c for c in pool if c['link'] != pick['link']]
        for i, c in enumerate(pool):
            c['index'] = i
    return None, None, [], skipped


def save_history(path, seen, published=None, skipped=(), video=False):
    marks = [f'{VIDEO}{moscow_today()}:{published}'] if published and video else []
    updated = list(dict.fromkeys(seen + [SKIP + s for s in skipped] + ([published] if published else []) + marks))[-1000:]
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(updated, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def publish(chat_id, text, link, photos, formatted=None):
    if not photos:
        raise RuntimeError('Фото не найдены: публикация отменена')
    fields = {'chat_id': chat_id}
    if len(photos) == 1:
        fields.update(caption=caption(text, link, formatted), parse_mode='HTML')
        return api('sendPhoto', fields, {'photo': photos[0]})
    media = [{'type': 'photo', 'media': f'attach://photo{i}'} for i in range(len(photos))]
    media[0].update(caption=caption(text, link, formatted), parse_mode='HTML')
    fields['media'] = json.dumps(media, ensure_ascii=False)
    return api('sendMediaGroup', fields, {f'photo{i}': data for i, data in enumerate(photos)})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    chat_id = check_channel()
    if args.check:
        print('Проверка завершена. Ничего не опубликовано.')
        return
    seen = json.loads(SEEN_FILE.read_text()) if SEEN_FILE.exists() else []
    candidates = []
    for topic in topic_order(published_count(seen)):
        candidates = fresh_for(topic, history_links(seen))
        if candidates:
            break
    if not candidates:
        print('Свежих неопубликованных новостей нет')
        return
    print('Тема:', 'видеоигры' if topic == 'games' else 'кино и сериалы', '| свежих новостей:', len(candidates))
    news.PROMPT = TOPICS[topic][1]
    pick, text, photos, skipped = pick_with_photos(candidates)
    if not photos:
        save_history(SEEN_FILE, seen, skipped=skipped)
        raise RuntimeError('Ни к одной из выбранных новостей не нашлось фото — публикация отменена; '
                           'эти новости больше не будут предлагаться')
    formatted = tg_format(pick, text)
    video = trailer_url(pick['link']) if videos_today(seen) < VIDEO_PER_DAY else None
    if video:
        print('Пост с трейлером:', video)
        result = publish_video(chat_id, text, pick['link'], video, formatted)
    else:
        result = publish(chat_id, text, pick['link'], photos, formatted)
    save_history(SEEN_FILE, seen, pick['link'], skipped, video=bool(video))
    messages = result if isinstance(result, list) else [result]
    print('Опубликовано; ID сообщений:', ', '.join(str(m['message_id']) for m in messages))


if __name__ == '__main__':
    try:
        main()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
