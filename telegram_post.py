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
import card

SEEN_FILE = Path(__file__).with_name('seen_telegram.json')

GAME_FEEDS = [
    'https://stopgame.ru/rss/rss_news.xml',
    'https://www.igromania.ru/rss/news.rss',
    'https://www.playground.ru/rss/news.xml',
    'https://www.gamemag.ru/rss/feed',
    'https://feeds.feedburner.com/ign/games-all',
    'https://www.pcgamer.com/rss/',
]
AAA_PRIORITY = (
    'ГЛАВНЫЙ ПРИОРИТЕТ — AAA-проекты: крупные игры больших студий и издателей с большим бюджетом '
    '(например GTA, Call of Duty, Battlefield, The Elder Scrolls, Fallout, Assassin\'s Creed, Far Cry, God of War, '
    'Spider-Man, The Last of Us, Horizon, Forza, Halo, Gears of War, Resident Evil, Final Fantasy, Metal Gear, '
    'Cyberpunk, The Witcher, Mass Effect, Elden Ring, EA Sports FC, Diablo, Silent Hill, Zelda, Mario). '
    'Если среди новостей есть хоть одна про AAA-проект — выбери её. Инди, мобильные и малоизвестные игры выбирай, '
    'только когда новостей об AAA нет. Среди AAA предпочитай анонсы, трейлеры, даты выхода, продолжения и ремейки, '
    'оценки и продажи.')
GAME_PROMPT = (news.PROMPT
               .replace('сообщества ВКонтакте «СтопКадр» о кино и сериалах', 'Telegram-канала «СтопКадр» о кино, сериалах и видеоиграх')
               .replace('именно про кино или сериалы (не игры, не музыку, не политику)', 'именно про видеоигры (не кино, не железо, не киберспортивные результаты, не политику)')
               .replace('Предпочитай громкие премьеры, трейлеры, кастинг, продолжения, сборы и рейтинги.', AAA_PRIORITY))
FRANCHISE_PRIORITY = (
    'ГЛАВНЫЙ ПРИОРИТЕТ — крупные франшизы, блокбастеры и громкие сериалы: Marvel, DC, «Звёздные войны», '
    '«Гарри Поттер», «Властелин колец», «Дюна», «Мир Юрского периода», «Миссия невыполнима», «Форсаж», «Джон Уик», '
    'Джеймс Бонд, «Аватар», «Чужой», «Хищник», «Терминатор», «Матрица», «Трансформеры», Pixar и Disney, '
    '«Игра престолов», «Ведьмак», «Очень странные дела», «Одни из нас», «Пацаны», экранизации больших игр и комиксов, '
    'новые фильмы Нолана, Вильнёва, Тарантино, Кэмерона, Спилберга, Скорсезе. Если среди новостей есть хоть одна про '
    'такую франшизу — выбери её. Фестивальное, нишевое и малоизвестное кино — только когда таких новостей нет. '
    'Среди крупных проектов предпочитай трейлеры, премьеры, кастинг, продолжения, сборы и рейтинги.')
MOVIE_PROMPT = (news.PROMPT.replace('сообщества ВКонтакте', 'Telegram-канала')
                .replace('Предпочитай громкие премьеры, трейлеры, кастинг, продолжения, сборы и рейтинги.', FRANCHISE_PRIORITY))
TOPICS = {'games': (GAME_FEEDS, GAME_PROMPT), 'cinema': (news.FEEDS, MOVIE_PROMPT)}


def topic_order(published):
    """published — сколько новостей уже вышло в этом канале: чётное → игры, нечётное → кино."""
    return ['games', 'cinema'] if published % 2 == 0 else ['cinema', 'games']


def fresh_for(topic, seen, per_feed=None):
    """Свежие новости темы: не больше per_feed с каждой ленты, чтобы плодовитые сайты не забивали выбор."""
    feeds, _ = TOPICS[topic]
    per_feed = per_feed or (8 if topic == 'games' else 5)  # игровые сайты публикуют много — шире выбор для AAA
    saved = news.FEEDS
    items = []
    try:
        for feed in feeds:
            news.FEEDS = [feed]
            items += news.fresh_news(seen)[:per_feed]
    finally:
        news.FEEDS = saved
    items.sort(key=lambda n: datetime.fromisoformat(n['date']).timestamp() if n.get('date') else 0, reverse=True)
    items = items[:40]
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
    for attempt, model in enumerate(m for m in news.GEMINI_MODELS for _ in range(3)):
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
                time.sleep(20 * (attempt % 3 + 1))
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
# Форматы — из образцов голоса канала (voice.md); выбираются случайно для разнообразия
STYLES = [
    'АБЗАЦЫ: 1–3 коротких абзаца по 1–2 предложения.',
    'ИРОНИЯ: первая строка текста начинается с короткого ироничного ярлыка с двоеточием (как «Теперь идеально:» '
    'или «Работают на ЦА:»), если новость это позволяет без натяжки; иначе используй формат АБЗАЦЫ.',
    'СПИСОК: строка «Что известно:» или «Внутри:», затем 2–3 коротких пункта с номерами «1.», «2.», «3.», '
    'ключевые фразы в пунктах — <b>жирным</b>.',
    'ВРЕЗКА: одна строка с главным фактом, затем 2–3 коротких пункта внутри <blockquote>…</blockquote>, '
    'каждый с новой строки и начинается с «— », ключевые фразы <b>жирным</b>.',
    'ЦИТАТА: заголовок — прямая цитата героя новости в «ёлочках» и после неё имя через « — »; затем 1–2 коротких факта. '
    'Только если в тексте источника есть прямая речь, иначе используй формат АБЗАЦЫ.',
]
VOICE_DIR = Path(__file__).parent
VOICE = (VOICE_DIR / 'voice.md').read_text(encoding='utf-8') if (VOICE_DIR / 'voice.md').exists() else ''
ABOUT_ME = (VOICE_DIR / 'about-me.md').read_text(encoding='utf-8') if (VOICE_DIR / 'about-me.md').exists() else ''
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
    raw = html.unescape(value or '')
    # Gemini иногда ставит HTML-переносы — превращаем их в обычные (Telegram и MAX не понимают <br> и <p>)
    raw = re.sub(r'<br\s*/?>', '\n', raw, flags=re.I)
    raw = re.sub(r'</?p\b[^>]*>', '\n', raw, flags=re.I)
    raw = re.sub(r'\n{3,}', '\n\n', raw)
    text = html.escape(raw.strip(), quote=False)
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


def tg_format(pick, draft, style=None, channel='Telegram'):
    """Переписывает пост в оформлении Telegram-канала. Возвращает безопасный HTML или None (тогда — обычный текст)."""
    style = style or random.choice(STYLES)
    source = article_text(pick['link']) or pick.get('summary', '')
    prompt = (
        f'Ты — голос канала «СтопКадр» ({channel}) о кино, сериалах и видеоиграх. Напиши пост на русском по новости.\n\n'
        f'О канале (about-me.md):\n{ABOUT_ME}\n\nГолос канала — следуй ему строго (voice.md):\n{VOICE}\n\n'
        f'Формат этого поста — {style}\n\n'
        'Обязательные правила:\n'
        '1. Первая строка — заголовок в <b>…</b>, до 80 символов: утверждение с сутью новости (не вопрос). '
        'В начале заголовка можно один тематический эмодзи.\n'
        '2. Текст после заголовка — 150–450 символов. Короткие предложения, между абзацами пустая строка.\n'
        '3. Названия игр, фильмов, сериалов и ключевые факты — <b>жирным</b>. Цитаты — в «ёлочках» с указанием, кто сказал.\n'
        '4. Факты, имена, даты, цифры и цитаты — только из текста источника, ничего не выдумывай. '
        'Без политики, без кликбейта, без спойлеров.\n'
        '5. Концовка — факт (когда и где выйдет) или практичный призыв. Без вопросов к читателю, без вывода-резюме.\n'
        '6. Хэштег — не больше одного и только по названию франшизы (например #Warzone), можно без хэштега.\n'
        '7. Разметка — только теги <b>, <i>, <blockquote>. Без ссылок, без HTML-сущностей вроде &amp;.\n\n'
        f'Черновик (только как источник фактов, стиль бери из голоса канала):\n{draft}\n\nТекст источника:\n{source}'
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


def nearby_images(link, span=9000, limit=6):
    """Запасной поиск: картинки сразу под заголовком статьи (<h1>) — для сайтов без <article> (например film.ru).
    Лишнее (превью соседних новостей и т.п.) потом отсеивает проверка Gemini."""
    try:
        page = news.http(link, timeout=15).decode('utf-8', 'ignore')
    except Exception:
        return []
    start = page.find('<h1')
    if start < 0:
        return []
    found = []
    for tag in re.findall(r'<img\b[^>]+>', page[start:start + span], re.I):
        src = re.search(r'''(?:data-src|data-lazy-src|src)=["']([^"']+)["']''', tag)
        if src and not src.group(1).startswith('data:'):
            found.append(src.group(1))
    return news.image_urls(found, link)[:limit]


SIZE_PARAMS = {'w', 'h', 'width', 'height', 'crop', 'resize', 'fit', 'quality', 'q', 'strip', 'zoom', 'ssl'}


def bigger_versions(url):
    """Адреса той же картинки в большем размере (сайты часто отдают уменьшенные копии). Сначала — лучший вариант."""
    parts = urllib.parse.urlsplit(url)
    out = []
    if parts.netloc.endswith('playground.ru'):
        # i.playground.ru/p/<id>.webp?420xauto -> ?1920xauto (размер задаётся в query)
        out.append(urllib.parse.urlunsplit(parts._replace(path=parts.path.replace('/p/', '/e/', 1), query='1920xauto')))
    if parts.query:
        query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
                 if k.lower() not in SIZE_PARAMS and not re.fullmatch(r'\d+x(\d+|auto)', k)]
        if len(query) != len(urllib.parse.parse_qsl(parts.query, keep_blank_values=True)) or re.fullmatch(r'\d+x(\d+|auto)', parts.query):
            out.append(urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query))))
    path = parts.path
    if parts.netloc.endswith('futurecdn.net'):
        # cdn.mos.cms.futurecdn.net/<id>-1200-80.jpg -> -1920-80 (PC Gamer и др. сайты Future)
        out.append(urllib.parse.urlunsplit(parts._replace(path=re.sub(r'-\d{3,4}-(\d+)(\.\w+)', r'-1920-\1\2', path))))
    for small, big in (('/middle/', '/big/'), ('/main.jpg', '/main_2x.jpg')):
        if small in path:
            out.append(urllib.parse.urlunsplit(parts._replace(path=path.replace(small, big))))
    return [u for u in dict.fromkeys(out) if u != url]


def image_size(data):
    with Image.open(io.BytesIO(data)) as im:
        return im.size


def fetch_best(url, referer):
    """Скачивает самую большую доступную версию картинки (пробует адреса без уменьшения)."""
    for candidate in bigger_versions(url) + [url]:
        try:
            data = news.download_jpeg(candidate, referer)
            if min(image_size(data)) >= 100:
                return data
        except Exception:
            continue
    return None


def pictures(pick):
    """До 3 фото к новости в лучшем доступном качестве: из одинаковых картинок разного размера берём самую крупную,
    мелкие (меньше 400 px по короткой стороне) отбрасываем, если есть крупнее; затем отбор Gemini."""
    urls = news.image_urls(news.page_images(pick['link']) + [pick.get('image')] + nearby_images(pick['link']), pick['link'])
    groups = []  # [отпечаток, данные, площадь]
    for url in urls[:10]:
        data = fetch_best(url, pick['link'])
        if not data:
            print('Кандидат изображения недоступен, пробую следующий')
            continue
        w, h = image_size(data)
        if max(w, h) / min(w, h) > 20 or len(data) > 10 * 1024 * 1024:
            continue
        fp = looks_like(data)
        same = next((g for g in groups if bin(fp ^ g[0]).count('1') < 40), None)  # < 15% различий из 256
        if same is None:
            groups.append([fp, data, w * h])
        elif w * h > same[2]:
            same[1], same[2] = data, w * h  # та же картинка, но крупнее — заменяем
        if len(groups) == 6:
            break
    big = [g for g in groups if min(image_size(g[1])) >= 400]
    out = [g[1] for g in (big or groups)]
    return vet_images(pick.get('title', ''), out)


SKIP = 'skip:'  # так в истории помечены новости, к которым не нашлось фото (их больше не предлагаем)


TITLE = 'title:'  # в истории: title:<заголовок опубликованной новости> — чтобы не повторять тему из другого источника


def history_links(seen):
    """Все ссылки из истории — и опубликованные, и пропущенные без фото."""
    return {s[len(SKIP):] if s.startswith(SKIP) else s for s in seen if not s.startswith((TITLE, VIDEO, 'card:'))}


def published_count(seen):
    return sum(1 for s in seen if not s.startswith((SKIP, VIDEO, TITLE, 'card:')))


def recent_topics(seen, limit=25):
    """Заголовки недавно опубликованных новостей; для старых записей без заголовка — «слаг» из адреса."""
    titles = [s[len(TITLE):] for s in seen if s.startswith(TITLE)]
    if len(titles) < limit:
        links = [s for s in seen if s.startswith('http')][-(limit - len(titles)):]
        titles = [urllib.parse.urlsplit(l).path.strip('/').split('/')[-1].replace('-', ' ').replace('_', ' ')
                  for l in links] + titles
    return [t for t in titles if t][-limit:]


def key_words(title):
    """Значимые слова заголовка (для запасной проверки повторов без Gemini)."""
    stop = {'новый', 'новая', 'новое', 'новые', 'фильм', 'сериал', 'игра', 'трейлер', 'первый', 'будет', 'получит',
            'стал', 'стала', 'после', 'about', 'with', 'from', 'will', 'says', 'first', 'trailer', 'movie', 'series'}
    words = re.findall(r'[A-Za-zА-Яа-яЁё0-9]{4,}', title.lower())
    return {w[:6] for w in words if w not in stop}


def drop_duplicates(candidates, recent):
    """Отдельный шаг анализа: убирает свежие новости о тех же событиях, что уже вышли в канале
    (даже из другого источника и на другом языке). Возвращает (оставшиеся, ссылки_повторов)."""
    if not candidates or not recent:
        return candidates, []
    prompt = (
        'Ты проверяешь новостной канал на повторы. Ниже список УЖЕ опубликованных новостей и список кандидатов.\n'
        'Верни в duplicates номера кандидатов, которые рассказывают о ТОМ ЖЕ событии, заявлении, анонсе, трейлере, '
        'релизе или обзоре, что и любая уже опубликованная новость — даже если источник другой, язык другой '
        '(английский/русский), а заголовок сформулирован иначе. Пример повтора: «Jeremy Renner says Avengers will lose '
        'without Hawkeye» и «Мстители без Соколиного глаза». Новость про ту же игру или фильм, но о ДРУГОМ событии '
        '(например, новый трейлер после новости о дате выхода) — не повтор.\n\n'
        'Уже опубликовано:\n' + '\n'.join(f'- {t}' for t in recent) + '\n\nКандидаты:\n' +
        '\n'.join(f'{c["index"]}. {c["title"]} — {str(c.get("summary", ""))[:160]}' for c in candidates))
    schema = {'type': 'OBJECT', 'properties': {'duplicates': {'type': 'ARRAY', 'items': {'type': 'INTEGER'}}},
              'required': ['duplicates']}
    try:
        dup_index = set(gemini_json([{'text': prompt}], schema).get('duplicates') or [])
    except RuntimeError as exc:
        print('Анализ повторов через Gemini недоступен, проверяю по ключевым словам:', exc)
        recent_keys = [key_words(t) for t in recent]
        dup_index = {c['index'] for c in candidates
                     if any(len(key_words(c['title']) & keys) >= 3 for keys in recent_keys)}
    dups = [c for c in candidates if c['index'] in dup_index]
    rest = [c for c in candidates if c['index'] not in dup_index]
    for c in dups:
        print('Повтор уже опубликованной новости, пропускаю:', c['title'][:100])
    for i, c in enumerate(rest):
        c['index'] = i
    return rest, [c['link'] for c in dups]


def selection_prompt(topic, seen):
    """Инструкция для выбора новости + список недавних тем, чтобы одна новость из разных источников не выходила дважды."""
    prompt = TOPICS[topic][1]
    recent = recent_topics(seen)
    if not recent:
        return prompt
    block = ('Эти новости УЖЕ опубликованы в канале. НЕ выбирай новость о том же событии или анонсе, '
             'даже если она из другого источника, на другом языке или с другим заголовком:\n'
             + '\n'.join('- ' + t for t in recent) + '\n\n')
    return prompt.replace('Новости:\n', block + 'Новости:\n')


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


# ---------- Инфографика-карточка (скилл graphic-designer, путь A) ----------
CARD = 'card:'           # в истории: card:<дата по Москве>:<ссылка> — пост вышел с карточкой
CARDS_PER_DAY = 4
CARD_STYLE = ('КАРТОЧКА: детали новости уже разложены на картинке-инфографике, поэтому текст — 1–2 коротких '
              'предложения: суть новости и почему это интересно. Не перечисляй пункты из карточки.')


def cards_today(seen):
    return sum(1 for s in seen if s.startswith(f'{CARD}{moscow_today()}:'))


def card_data(pick):
    """Gemini решает, подходит ли новость для карточки, и заполняет её фактами из статьи. None — карточка не нужна."""
    source = article_text(pick['link'])
    if len(source) < 300:
        return None
    prompt = (
        'Ты дизайнер Telegram-канала «СтопКадр». Реши, нужна ли к новости картинка-инфографика с таблицей пунктов.\n'
        'use_card = true ТОЛЬКО если в тексте источника есть 3–5 конкретных однотипных пунктов: способности героя, '
        'изменения патча, новые функции, режимы, состав актёров, характеристики, несколько фактов о релизе '
        '(дата, платформы, цена и т.п.). Для простой новости из одного факта — use_card = false.\n'
        'Если use_card = true:\n'
        '- title: до 40 символов, имя героя/игры/фильма и суть («Violet: первый взгляд на способности»)\n'
        '- tags: 2–3 короткие плашки до 16 символов (жанр, игра, тип новости)\n'
        '- rows: 3–5 пунктов; icon — один подходящий эмодзи; text — до 170 символов, ключевые слова в <b>…</b>\n'
        'Всё на русском, только факты из источника, ничего не выдумывай.\n\n'
        f'Заголовок новости: {pick.get("title", "")}\n\nТекст источника:\n{source}')
    schema = {'type': 'OBJECT', 'properties': {
        'use_card': {'type': 'BOOLEAN'}, 'title': {'type': 'STRING'},
        'tags': {'type': 'ARRAY', 'items': {'type': 'STRING'}},
        'rows': {'type': 'ARRAY', 'items': {'type': 'OBJECT', 'properties': {
            'icon': {'type': 'STRING'}, 'text': {'type': 'STRING'}}, 'required': ['icon', 'text']}}},
        'required': ['use_card']}
    try:
        data = gemini_json([{'text': prompt}], schema)
    except RuntimeError as exc:
        print('Карточка пропущена:', exc)
        return None
    rows = [r for r in data.get('rows') or [] if str(r.get('text', '')).strip()]
    if not data.get('use_card') or len(rows) < 3 or not str(data.get('title', '')).strip():
        return None
    return {'title': data['title'][:60], 'tags': [t[:20] for t in data.get('tags') or []][:3],
            'rows': [{'icon': str(r.get('icon', ''))[:4], 'text': str(r['text'])[:220]} for r in rows[:5]]}


def make_card(pick, photos):
    """Карточка (JPEG) или None. Кадр для шапки — первое фото новости."""
    data = card_data(pick)
    if not data:
        return None
    image = card.render_card(data, photos[0] if photos else None)
    if image:
        print('Карточка нарисована:', data['title'])
    return image


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


def save_history(path, seen, published=None, skipped=(), video=False, title=None, with_card=False):
    marks = [f'{VIDEO}{moscow_today()}:{published}'] if published and video else []
    if published and with_card:
        marks.append(f'card:{moscow_today()}:{published}')
    if published and title:
        marks.append(TITLE + ' '.join(title.split())[:150])
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
        sample = {'title': 'Проверка карточки', 'tags': ['Тест'], 'rows': [{'icon': '🎮', 'text': '<b>Пункт</b> карточки'}] * 3}
        image = card.render_card(sample)
        print('Карточки:', f'рендер работает ({len(image) // 1024} КБ)' if image else 'браузер недоступен — посты пойдут без карточек')
        print('Проверка завершена. Ничего не опубликовано.')
        return
    seen = json.loads(SEEN_FILE.read_text(encoding='utf-8')) if SEEN_FILE.exists() else []
    candidates = []
    for topic in topic_order(published_count(seen)):
        candidates = fresh_for(topic, history_links(seen))
        if candidates:
            break
    if not candidates:
        print('Свежих неопубликованных новостей нет')
        return
    print('Тема:', 'видеоигры' if topic == 'games' else 'кино и сериалы', '| свежих новостей:', len(candidates))
    candidates, repeats = drop_duplicates(candidates, recent_topics(seen))
    if not candidates:
        save_history(SEEN_FILE, seen, skipped=repeats)
        print('Все свежие новости — повторы уже опубликованных')
        return
    news.PROMPT = selection_prompt(topic, seen)
    pick, text, photos, skipped = pick_with_photos(candidates)
    skipped = repeats + skipped
    if not photos:
        save_history(SEEN_FILE, seen, skipped=skipped)
        raise RuntimeError('Ни к одной из выбранных новостей не нашлось фото — публикация отменена; '
                           'эти новости больше не будут предлагаться')
    video = trailer_url(pick['link']) if videos_today(seen) < VIDEO_PER_DAY else None
    infographic = None if video or cards_today(seen) >= CARDS_PER_DAY else make_card(pick, photos)
    if infographic:
        photos = [photos[0], infographic] + photos[1:2]  # как в примере: кадр новости, затем карточка
    formatted = tg_format(pick, text, style=CARD_STYLE if infographic else None)
    if video:
        print('Пост с трейлером:', video)
        result = publish_video(chat_id, text, pick['link'], video, formatted)
    else:
        result = publish(chat_id, text, pick['link'], photos, formatted)
    save_history(SEEN_FILE, seen, pick['link'], skipped, video=bool(video), title=pick.get('title'), with_card=bool(infographic))
    messages = result if isinstance(result, list) else [result]
    print('Опубликовано; ID сообщений:', ', '.join(str(m['message_id']) for m in messages))


if __name__ == '__main__':
    try:
        main()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
