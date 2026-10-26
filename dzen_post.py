"""Дзен: статьи-разборы «СтопКадра» 2 раза в день — заготовки для ручной публикации.

У Дзена нет API для авторов, а официальный перенос из Telegram (Синхроробот) теряет либо фото, либо длинный текст
(проверено 2026-10-26). Поэтому бот готовит статью целиком и присылает её автору в личные сообщения
(переменная DZEN_DRAFT_CHAT_ID): альбом фото по порядку (первое — обложка) и текст с пометками «📷 Фото N» там,
где вставить фото. Автор переносит это в Студию Дзена.

Свежие новости кино и игр → Gemini выбирает историю, о которой есть что сказать (AAA и большие франшизы — в
приоритете) → тексты 1–3 источников → статья 2500–3800 знаков в голосе канала (voice.md, раздел Long-form) →
2–5 фото (страницы источников, другие статьи об этой истории, скриншоты Steam) → Gemini расставляет фото по
разделам. История — seen_dzen.json.
"""
import html
import json
import os
from pathlib import Path
import re
import sys
import urllib.parse

import post as news
import telegram_post as tg

SEEN_FILE = Path(__file__).with_name('seen_dzen.json')
MIN_TEXT = 2200  # меньше — это уже не разбор
MAX_TEXT = 4500
MESSAGE = 4000  # сообщение Telegram — до 4096 знаков; длинный текст делим на части
WRITER_MODELS = ['gemini-3.8-flash', 'gemini-3.5-flash-lite']  # для длинного текста — модель посильнее

PICK_SCHEMA = {'type': 'OBJECT', 'properties': {
    'index': {'type': 'INTEGER'},
    'related': {'type': 'ARRAY', 'items': {'type': 'INTEGER'}},
    'angle': {'type': 'STRING'},
    'query': {'type': 'STRING'},
    'game': {'type': 'STRING'},
}, 'required': ['index', 'related', 'angle', 'query', 'game']}

ARTICLE_SCHEMA = {'type': 'OBJECT', 'properties': {
    'title': {'type': 'STRING'},
    'lead': {'type': 'STRING'},
    'sections': {'type': 'ARRAY', 'items': {'type': 'OBJECT', 'properties': {
        'heading': {'type': 'STRING'}, 'text': {'type': 'STRING'}}, 'required': ['heading', 'text']}},
    'verdict': {'type': 'STRING'},
}, 'required': ['title', 'lead', 'sections', 'verdict']}


def candidates(seen):
    used = tg.history_links(seen)
    items = tg.fresh_for('games', used) + tg.fresh_for('cinema', used)
    for i, c in enumerate(items):
        c['index'] = i
    return items


def choose(items, recent, exclude=()):
    pool = [c for c in items if c['link'] not in exclude]
    lines = [f"{c['index']}. {c['title']} — {c.get('summary', '')[:200]}" for c in pool]
    done = '\n'.join('- ' + t for t in recent) or '- (пока ничего)'
    prompt = ('Ты главный редактор Дзен-канала «СтопКадр» о кино, сериалах и видеоиграх. Дважды в день мы выпускаем '
              'статью-разбор на 4–5 минут чтения: событие + контекст + мнение редакции.\n'
              'Выбери из свежих новостей ОДНУ историю, о которой есть что сказать развёрнуто: громкий трейлер, '
              'премьера, первые отзывы критиков, анонс продолжения, перезапуск, кастинг, провал или рекорд. '
              'Приоритет — AAA-игры и большие франшизы (Marvel, DC, «Звёздные войны», GTA, The Witcher, '
              'Call of Duty, Assassin\'s Creed, «Гарри Поттер» и т.п.). Не бери мелкие скидки, патчи, слухи без '
              'источника и политику. Не бери темы, уже разобранные в канале (список ниже).\n'
              'index — номер выбранной новости; related — номера других новостей о ТОЙ ЖЕ истории (могут '
              'дополнить статью фактами), можно пустой список; angle — одной фразой, под каким углом писать разбор; '
              'query — 3–6 слов по-английски для поиска других статей об этой истории (название фильма/игры + суть); '
              'game — точное официальное название видеоигры, если история про игру, иначе пустая строка.\n\n'
              f'Уже разобрано:\n{done}\n\nНовости:\n' + '\n'.join(lines))
    data = tg.gemini_json([{'text': prompt}], PICK_SCHEMA)
    by_index = {c['index']: c for c in pool}
    pick = by_index.get(data.get('index'))
    if not pick:
        raise RuntimeError('Gemini не выбрал тему статьи')
    related = [by_index[i] for i in data.get('related') or [] if i in by_index and i != pick['index']][:2]
    pick = dict(pick, query=(data.get('query') or '').strip(), game=(data.get('game') or '').strip())
    return pick, related, data.get('angle', '')


def write(pick, related, angle):
    sources = []
    for item in [pick] + related:
        text = tg.article_text(item['link'], 6000) or item.get('summary', '')
        if text:
            sources.append(f"### {item['title']}\n{text}")
    prompt = (f'Ты — голос канала «СтопКадр» о кино, сериалах и видеоиграх. Напиши статью-разбор для Дзена на русском.\n\n'
              f'О канале (about-me.md):\n{tg.ABOUT_ME}\n\nГолос канала (voice.md) — для статьи главный раздел '
              f'«Long-form», остальное — общий характер:\n{tg.VOICE}\n\n'
              f'Тема: {pick["title"]}\nУгол разбора: {angle}\n\n'
              'Поля ответа:\n'
              '- title — заголовок до 100 знаков: утверждение с позицией автора, без вопроса и без кавычек вокруг '
              'всего заголовка.\n'
              '- lead — 2–3 предложения: что случилось и почему об этом стоит говорить.\n'
              '- sections — 3–5 блоков: heading — короткий подзаголовок-утверждение (до 60 знаков, без точки в конце), '
              'text — 1–3 абзаца, между абзацами пустая строка.\n'
              '- verdict — вердикт редакции одной-двумя фразами.\n'
              'Общий объём 2500–3800 знаков. Факты, даты, имена, цифры, цитаты — только из источников ниже, '
              'ничего не выдумывай; оценки и мнение — открыто от редакции. Без HTML, markdown, эмодзи, хэштегов, '
              'вопросов к читателю, политики и спойлеров.\n\n'
              'Источники:\n' + '\n\n'.join(sources))
    saved = news.GEMINI_MODELS
    news.GEMINI_MODELS = WRITER_MODELS
    try:
        data = tg.gemini_json([{'text': prompt}], ARTICLE_SCHEMA, 180)
    finally:
        news.GEMINI_MODELS = saved
    return clean_article(data)


def plain(value):
    value = re.sub(r'<[^>]+>|[*_#`]', '', html.unescape(value or ''))
    return re.sub(r'\n{3,}', '\n\n', value).strip()


def clean_article(data):
    title = plain(data.get('title'))
    if re.fullmatch(r'«[^«»]*»|"[^"]*"', title):  # кавычки вокруг всего заголовка убираем, внутри — оставляем
        title = title[1:-1].strip()
    title = title.rstrip('.')  # в Студии Дзена заголовок — отдельное поле, точка не нужна
    sections = [{'heading': plain(s.get('heading')).rstrip('.'), 'text': plain(s.get('text'))}
                for s in data.get('sections') or [] if plain(s.get('text'))]
    article = {'title': title, 'lead': plain(data.get('lead')), 'sections': sections, 'verdict': plain(data.get('verdict'))}
    if not title or not article['lead'] or not sections:
        raise RuntimeError('Gemini вернул неполную статью')
    return article


def as_text(article):
    parts = [article['title'], article['lead']]
    for s in article['sections']:
        parts += [s['heading'], s['text']]
    parts.append('Вердикт: ' + article['verdict'] if article['verdict'] else '')
    return '\n\n'.join(p for p in parts if p)


def fit(article):
    """Не длиннее MAX_TEXT: если Gemini расписался — убираем средние блоки, сохраняя первый и последний."""
    while len(as_text(article)) > MAX_TEXT and len(article['sections']) > 2:
        article['sections'].pop(-2)
    return article


MIN_PHOTOS = 1   # хотя бы одно фото — обложка
WANT_PHOTOS = 3  # но стараемся найти минимум три: ищем ещё в других статьях об этой истории и в Steam
MAX_PHOTOS = 5


def add_unique(found, images):
    for data in images:
        fp = tg.looks_like(data)
        if all(bin(fp ^ tg.looks_like(old)).count('1') >= 40 for old in found):  # та же картинка другого размера — нет
            found.append(data)


def bing_links(query, limit=5):
    """Другие статьи об этой же истории (Bing News RSS отдаёт прямые ссылки в параметре url)."""
    if not query:
        return []
    url = 'https://www.bing.com/news/search?' + urllib.parse.urlencode({'q': query, 'format': 'rss', 'setlang': 'en'})
    try:
        xml = news.http(url, timeout=20).decode('utf-8', 'ignore')
    except Exception as exc:
        print('  ! Bing News недоступен:', type(exc).__name__)
        return []
    out = []
    for block in re.findall(r'<item>[\s\S]*?</item>', xml):
        link = html.unescape(news.tag(block, 'link'))
        link = (urllib.parse.parse_qs(urllib.parse.urlsplit(link).query).get('url') or [link])[0]
        if link.startswith('http') and 'bing.com' not in link:
            out.append({'link': link, 'title': news.tag(block, 'title'), 'image': ''})
    return out[:limit]


def steam_shots(game, limit=4):
    """Официальные скриншоты игры из магазина Steam (Full HD)."""
    if not game:
        return []
    try:
        res = json.loads(news.http('https://store.steampowered.com/api/storesearch/?' + urllib.parse.urlencode(
            {'term': game, 'cc': 'us', 'l': 'english'}), timeout=20))
        app = next((i for i in res.get('items', []) if i['name'].lower() == game.lower()), (res.get('items') or [None])[0])
        if not app:
            return []
        info = json.loads(news.http(f"https://store.steampowered.com/api/appdetails?appids={app['id']}", timeout=20))
        shots = (info.get(str(app['id']), {}).get('data') or {}).get('screenshots') or []
    except Exception as exc:
        print('  ! Steam недоступен:', type(exc).__name__)
        return []
    out = []
    for shot in shots[:limit]:
        try:
            out.append(news.download_jpeg(shot['path_full'], 'https://store.steampowered.com/'))
        except Exception:
            continue
    print(f"  Steam «{app['name']}»: скриншотов {len(out)}")
    return out


def gather_photos(items):
    """Фото к статье: страницы источников → другие статьи об этой истории (Bing News) → скриншоты Steam.
    Одинаковые картинки (даже другого размера) — один раз."""
    found = []
    pick = items[0]
    for item in items + bing_links(pick.get('query')):
        if len(found) >= WANT_PHOTOS:
            break
        try:
            add_unique(found, tg.pictures(item))
        except Exception as exc:
            print('  ! фото не получены:', type(exc).__name__)
    if len(found) < WANT_PHOTOS:
        add_unique(found, steam_shots(pick.get('game')))
    print(f'Фото к статье: {len(found)}')
    return found[:MAX_PHOTOS]


def order_photos(article, photos):
    """Gemini расставляет фото по кускам статьи: обложка — к заголовку, остальные — к подходящим разделам."""
    captions = [' '.join(part)[:300] for part in photo_groups(article, len(photos))]
    parts = [{'text': ('Ниже фрагменты статьи по порядку и фото (номера 0..%d). Подбери к каждому фрагменту своё фото, '
                       'которое лучше всего его иллюстрирует; каждое фото — ровно один раз. Первому фрагменту '
                       '(заголовок) — самое эффектное фото-обложку. Верни order: номера фото по порядку фрагментов.\n\n'
                       % (len(photos) - 1)) + '\n\n'.join(f'Фрагмент {i}: {c}' for i, c in enumerate(captions))}]
    parts += [{'inline_data': {'mime_type': 'image/jpeg', 'data': tg.thumb_b64(d)}} for d in photos]
    schema = {'type': 'OBJECT', 'properties': {'order': {'type': 'ARRAY', 'items': {'type': 'INTEGER'}}}, 'required': ['order']}
    try:
        order = tg.gemini_json(parts, schema, 60).get('order') or []
    except RuntimeError as exc:
        print('  ! расстановка фото пропущена:', exc)
        return photos
    order = [i for i in dict.fromkeys(order) if isinstance(i, int) and 0 <= i < len(photos)]
    order += [i for i in range(len(photos)) if i not in order]
    return [photos[i] for i in order]


def blocks(article):
    """Смысловые куски статьи по порядку: заголовок с лидом, разделы, вердикт (обычный текст)."""
    out = [f"{article['title']}\n\n{article['lead']}"]
    out += [f"{sec['heading']}\n\n{sec['text']}" for sec in article['sections']]
    if article['verdict']:
        out.append('Вердикт: ' + article['verdict'])
    return out


def photo_groups(article, count):
    """Делит куски статьи на count групп по порядку: группа N иллюстрируется фото N (группа 0 — обложка)."""
    parts = blocks(article)
    groups = [[] for _ in range(count)]
    for i, part in enumerate(parts):
        groups[min(count - 1, i * count // len(parts))].append(part)
    return [g for g in groups if g] or [parts]


def draft_text(article, count):
    """Текст для Студии Дзена (HTML Telegram): заголовок отдельно, пометки «📷 Фото N» там, куда вставить фото."""
    esc = lambda v: html.escape(v, quote=False)
    out = [f'<b>Заголовок:</b> {esc(article["title"])}', '📷 <b>Фото 1 — обложка</b>', esc(article['lead'])]
    groups = photo_groups(article, count)
    starts = {}
    pos = 0
    for n, group in enumerate(groups):
        starts[pos] = n
        pos += len(group)
    parts = [('section', sec) for sec in article['sections']] + ([('verdict', article['verdict'])] if article['verdict'] else [])
    for i, (kind, value) in enumerate(parts, 1):  # куски после заголовка с лидом: i = номер куска в blocks()
        if starts.get(i):
            out.append(f'📷 <b>Фото {starts[i] + 1}</b>')
        if kind == 'section':
            out += [f'<b>{esc(value["heading"])}</b>', esc(value['text'])]
        else:
            out.append(f'<b>Вердикт:</b> {esc(value)}')
    return '\n\n'.join(out)


def split_message(text, limit=MESSAGE):
    """Делит длинный текст на сообщения по абзацам."""
    chunks, cur = [], ''
    for para in text.split('\n\n'):
        if cur and len(cur) + 2 + len(para) > limit:
            chunks.append(cur)
            cur = para
        else:
            cur = f'{cur}\n\n{para}' if cur else para
    return chunks + ([cur] if cur else [])


def send_draft(chat_id, article, photos, pick):
    """Альбом фото по порядку + текст статьи с пометками, куда вставить фото."""
    tg.api('sendMessage', {'chat_id': chat_id, 'parse_mode': 'HTML', 'text': (
        '📝 <b>Статья для Дзена готова</b>\n'
        'Сохраните фото из альбома ниже и вставьте их в статью там, где стоит пометка «📷 Фото N». '
        'Фото 1 — обложка. Заголовок вставьте в поле заголовка.')})
    if len(photos) == 1:
        tg.api('sendPhoto', {'chat_id': chat_id, 'caption': 'Фото 1 — обложка'}, {'photo': photos[0]})
    else:
        media = [{'type': 'photo', 'media': f'attach://p{i}', 'caption': 'Фото 1 — обложка' if i == 0 else f'Фото {i + 1}'}
                 for i in range(len(photos))]
        tg.api('sendMediaGroup', {'chat_id': chat_id, 'media': json.dumps(media, ensure_ascii=False)},
               {f'p{i}': data for i, data in enumerate(photos)})
    for chunk in split_message(draft_text(article, len(photos))):
        tg.api('sendMessage', {'chat_id': chat_id, 'text': chunk, 'parse_mode': 'HTML',
                               'link_preview_options': json.dumps({'is_disabled': True})})
    tg.api('sendMessage', {'chat_id': chat_id, 'text': 'Источник: ' + pick['link'],
                           'link_preview_options': json.dumps({'is_disabled': True})})


def chat():
    chat_id = os.environ.get('DZEN_DRAFT_CHAT_ID', '').strip()
    if not chat_id:
        raise RuntimeError('Задайте переменную DZEN_DRAFT_CHAT_ID (личный чат автора с ботом, см. --find)')
    return chat_id


def find():
    """Личные чаты, где автору написали боту /start — чтобы узнать DZEN_DRAFT_CHAT_ID."""
    chats = {}
    for upd in tg.api('getUpdates', {'allowed_updates': json.dumps(['message'])}):
        c = (upd.get('message') or {}).get('chat') or {}
        if c.get('type') == 'private':
            chats[c['id']] = ' '.join(filter(None, [c.get('first_name'), c.get('last_name')])) + \
                (f" (@{c['username']})" if c.get('username') else '')
    print('Личные чаты с ботом:', '; '.join(f'{name}: {cid}' for cid, name in chats.items()) or
          'нет — напишите боту /start и запустите ещё раз')


def main():
    if '--find' in sys.argv:
        find()
        return
    preview = '--preview' in sys.argv
    chat_id = None if preview else chat()
    seen = json.loads(SEEN_FILE.read_text(encoding='utf-8')) if SEEN_FILE.exists() else []
    items = candidates(seen)
    print('Свежих новостей кино и игр:', len(items))
    if not items:
        print('Тем нет — ждём следующего запуска.')
        return
    recent = [s[len(tg.TITLE):] for s in seen if s.startswith(tg.TITLE)][-30:]
    tried = []
    for _ in range(4):  # без единого фото статья не годится — берём другую тему
        pick, related, angle = choose(items, recent, tried)
        print('Тема:', pick['title'], '|', angle)
        photos = gather_photos([pick] + related)
        if len(photos) >= MIN_PHOTOS:
            break
        print('Фото не нашлось — беру другую тему')
        tried.append(pick['link'])
    else:
        tg.save_history(SEEN_FILE, seen, skipped=tried)
        raise RuntimeError('Ни к одной теме не нашлось фото — статья не подготовлена')
    article = write(pick, related, angle)
    if len(as_text(article)) < MIN_TEXT * 0.8:  # Gemini иногда сокращает — просим ещё раз, берём более длинный вариант
        print(f'Статья короткая ({len(as_text(article))} знаков), пишу заново')
        article = max(article, write(pick, related, angle), key=lambda a: len(as_text(a)))
    article = fit(article)
    photos = order_photos(article, photos) if len(photos) > 1 else photos
    print(f'Статья: {len(as_text(article))} знаков, фото: {len(photos)}\n\n{draft_text(article, len(photos))}\n')
    if preview:
        return
    send_draft(chat_id, article, photos, pick)
    links = [pick['link']] + [r['link'] for r in related]
    tg.save_history(SEEN_FILE, seen, links[0], tried + links[1:], title=pick['title'])
    print('Заготовка отправлена автору')


if __name__ == '__main__':
    try:
        main()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
