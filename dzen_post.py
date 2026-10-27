"""Дзен: мини-разборы «СтопКадра» 2 раза в день — полностью автоматически.

У Дзена нет API для авторов, поэтому статьи идут через официальный кросспостинг: бот публикует пост в отдельный
публичный Telegram-канал (переменная DZEN_CHAT_ID, @stopkadr_reviews), а Синхроробот Дзена (@zen_sync_bot) переносит
его в Дзен-канал как статью. Проверено 2026-10-26: из альбома переносятся все фото, но текст — только из подписи
к первому фото (до 1024 знаков); форматирование (жирный) теряется, абзацы и эмодзи сохраняются. Заголовок статьи
в Дзене — первое предложение поста.

Свежие новости кино и игр → Gemini выбирает историю (AAA и большие франшизы — в приоритете) → тексты 1–3
источников → мини-разбор до 1000 знаков в голосе канала (voice.md, раздел Long-form) → 1–2 фото → альбом с
подписью. История — seen_dzen.json.
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
CAPTION = 1000  # Синхроробот переносит в Дзен текст только из подписи к фото, а она — до 1024 знаков
MAX_PHOTOS = 2
WRITER_MODELS = ['gemini-3.8-flash', 'gemini-3.5-flash-lite']  # для статьи — модель посильнее

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
        'emoji': {'type': 'STRING'}, 'heading': {'type': 'STRING'}, 'text': {'type': 'STRING'}},
        'required': ['emoji', 'heading', 'text']}},
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
              'мини-разбор на минуту чтения: событие + контекст + мнение редакции.\n'
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
        text = tg.article_text(item['link'], 5000) or item.get('summary', '')
        if text:
            sources.append(f"### {item['title']}\n{text}")
    prompt = (f'Ты — голос канала «СтопКадр» о кино, сериалах и видеоиграх. Напиши мини-разбор для Дзена на русском.\n\n'
              f'О канале (about-me.md):\n{tg.ABOUT_ME}\n\nГолос канала (voice.md) — для разбора главный раздел '
              f'«Long-form», остальное — общий характер:\n{tg.VOICE}\n\n'
              f'Тема: {pick["title"]}\nУгол разбора: {angle}\n\n'
              'Поля ответа:\n'
              '- title — заголовок до 90 знаков: утверждение с позицией автора, без вопроса и без кавычек вокруг '
              'всего заголовка.\n'
              '- lead — 1–2 предложения: что случилось и почему это важно.\n'
              '- sections — 2 или 3 коротких блока: emoji — один тематический эмодзи (🎬 🎮 📺 🔥 💰 ⭐ 🗓 и т.п.), '
              'heading — подзаголовок-утверждение до 40 знаков без точки, text — 1–2 предложения.\n'
              '- verdict — вердикт редакции одним предложением.\n'
              'ВЕСЬ текст вместе — не больше 850 знаков, это жёсткий предел. Факты, даты, имена, цифры, цитаты — только '
              'из источников ниже, ничего не выдумывай; мнение — открыто от редакции. Без HTML, markdown, хэштегов, '
              'вопросов к читателю, политики и спойлеров.\n\n'
              'Источники:\n' + '\n\n'.join(sources))
    saved = news.GEMINI_MODELS
    news.GEMINI_MODELS = WRITER_MODELS
    try:
        data = tg.gemini_json([{'text': prompt}], ARTICLE_SCHEMA, 120)
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
    if title and title[-1] not in '.!?…':
        title += '.'  # Дзен берёт заголовком первое предложение — точка отделяет его от лида
    sections = [{'emoji': plain(s.get('emoji'))[:4] or '▪️', 'heading': plain(s.get('heading')).rstrip('.'),
                 'text': plain(s.get('text'))}
                for s in data.get('sections') or [] if plain(s.get('text'))]
    article = {'title': title, 'lead': plain(data.get('lead')), 'sections': sections, 'verdict': plain(data.get('verdict'))}
    if not title or not article['lead'] or not sections:
        raise RuntimeError('Gemini вернул неполную статью')
    return article


def caption(article, bold=True):
    """Подпись к фото: заголовок, лид, блоки «эмодзи + подзаголовок», вердикт. В Telegram подзаголовки жирные,
    в Дзене жирный пропадает, но эмодзи и пустые строки сохраняют структуру."""
    esc = lambda v: html.escape(v, quote=False)
    b = (lambda v: f'<b>{esc(v)}</b>') if bold else esc
    parts = [b(article['title']), esc(article['lead'])]
    parts += [f"{s['emoji']} {b(s['heading'])}\n{esc(s['text'])}" for s in article['sections']]
    if article['verdict']:
        parts.append(f"🎯 {b('Вердикт:')} {esc(article['verdict'])}")
    return '\n\n'.join(parts)


def visible(value):
    return len(html.unescape(re.sub(r'<[^>]+>', '', value)))


def fit(article):
    """Не длиннее CAPTION видимых знаков: сначала убираем средние блоки, потом укорачиваем самый длинный текст."""
    article = dict(article, sections=list(article['sections']))
    while visible(caption(article)) > CAPTION and len(article['sections']) > 1:
        article['sections'].pop(len(article['sections']) // 2)
    for _ in range(10):
        extra = visible(caption(article)) - CAPTION
        if extra <= 0:
            break
        field = max([('lead', article)] + [('text', sec) for sec in article['sections']], key=lambda f: len(f[1][f[0]]))
        name, holder = field
        cut = holder[name][:max(20, len(holder[name]) - extra - 1)]
        holder[name] = (cut.rsplit(' ', 1)[0] if ' ' in cut else cut).rstrip(',;:—- ') + '…'
    return article


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
    """1–2 фото: страницы источников → другие статьи об этой истории (Bing News) → скриншоты Steam.
    Дальше по списку идём, только пока не нашлось ни одного — чтобы пост выходил быстро."""
    found = []
    pick = items[0]
    for item in items:
        try:
            add_unique(found, tg.pictures(item))
        except Exception as exc:
            print('  ! фото не получены:', type(exc).__name__)
        if found:
            break
    if not found:
        for item in bing_links(pick.get('query'), 3):
            try:
                add_unique(found, tg.pictures(item))
            except Exception:
                continue
            if found:
                break
    if not found:
        add_unique(found, steam_shots(pick.get('game'), 2))
    print(f'Фото к статье: {len(found)}')
    return found[:MAX_PHOTOS]


def publish(chat_id, article, photos):
    """Одно фото — sendPhoto, два — альбом; весь текст в подписи к первому фото (только её переносит Дзен)."""
    text = caption(article)
    if len(photos) == 1:
        return tg.api('sendPhoto', {'chat_id': chat_id, 'caption': text, 'parse_mode': 'HTML'}, {'photo': photos[0]})
    media = [{'type': 'photo', 'media': f'attach://p{i}'} for i in range(len(photos))]
    media[0].update(caption=text, parse_mode='HTML')
    return tg.api('sendMediaGroup', {'chat_id': chat_id, 'media': json.dumps(media, ensure_ascii=False)},
                  {f'p{i}': data for i, data in enumerate(photos)})


def chat():
    chat_id = os.environ.get('DZEN_CHAT_ID', '').strip()
    if not chat_id:
        raise RuntimeError('Задайте переменную DZEN_CHAT_ID: @имя Telegram-канала, связанного с Синхророботом Дзена')
    return chat_id


def check():
    """Бот может публиковать в канал. Синхроробот так не проверить: Telegram не показывает ботам других ботов
    среди администраторов — его подключение видно только в Студии Дзена."""
    os.environ['TELEGRAM_CHAT_ID'] = chat()
    tg.check_channel()


def main():
    if '--check' in sys.argv:
        check()
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
    for _ in range(3):  # без фото статья не годится — берём другую тему
        pick, related, angle = choose(items, recent, tried)
        print('Тема:', pick['title'], '|', angle)
        photos = gather_photos([pick] + related)
        if photos:
            break
        print('Фото не нашлось — беру другую тему')
        tried.append(pick['link'])
    else:
        tg.save_history(SEEN_FILE, seen, skipped=tried)
        raise RuntimeError('Ни к одной теме не нашлось фото — статья не опубликована')
    article = fit(write(pick, related, angle))
    print(f'Статья: {visible(caption(article))} знаков, фото: {len(photos)}\n\n{caption(article, bold=False)}\n')
    if preview:
        return
    publish(chat_id, article, photos)
    links = [pick['link']] + [r['link'] for r in related]
    tg.save_history(SEEN_FILE, seen, links[0], tried + links[1:], title=pick['title'])
    print('Опубликовано в канал', chat_id)


if __name__ == '__main__':
    try:
        main()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
