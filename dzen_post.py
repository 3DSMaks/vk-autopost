"""Дзен: статьи-разборы «СтопКадра» 2 раза в день.

Своего API для авторов у Дзена нет, поэтому статьи идут через официальный кросспостинг: бот публикует статью в
отдельный публичный Telegram-канал (переменная DZEN_CHAT_ID, например @stopkadr_reviews), а Синхроробот Дзена
(@zen_sync_bot) переносит её в Дзен-канал. Пост «текст + фото» Дзен публикует как статью: заголовок — первое
предложение (до 140 знаков), обложка — первая картинка, форматирование Telegram не переносится.

Свежие новости кино и игр → Gemini выбирает историю, о которой есть что сказать (AAA и большие франшизы — в
приоритете) → тексты 1–3 источников → статья 2500–3800 знаков в голосе канала (voice.md, раздел Long-form) →
фото лучшего качества → одно сообщение «фото + текст» (Rich Message). История — seen_dzen.json.
"""
import html
import json
import os
from pathlib import Path
import re
import sys

import post as news
import telegram_post as tg

SEEN_FILE = Path(__file__).with_name('seen_dzen.json')
MIN_TEXT = 2200  # меньше — это уже не разбор
MAX_TEXT = 4000  # обычное сообщение Telegram — до 4096 знаков; держим запас для запасного формата
WRITER_MODELS = ['gemini-3.8-flash', 'gemini-3.5-flash-lite']  # для длинного текста — модель посильнее

PICK_SCHEMA = {'type': 'OBJECT', 'properties': {
    'index': {'type': 'INTEGER'},
    'related': {'type': 'ARRAY', 'items': {'type': 'INTEGER'}},
    'angle': {'type': 'STRING'},
}, 'required': ['index', 'related', 'angle']}

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
              'дополнить статью фактами), можно пустой список; angle — одной фразой, под каким углом писать разбор.\n\n'
              f'Уже разобрано:\n{done}\n\nНовости:\n' + '\n'.join(lines))
    data = tg.gemini_json([{'text': prompt}], PICK_SCHEMA)
    by_index = {c['index']: c for c in pool}
    pick = by_index.get(data.get('index'))
    if not pick:
        raise RuntimeError('Gemini не выбрал тему статьи')
    related = [by_index[i] for i in data.get('related') or [] if i in by_index and i != pick['index']][:2]
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
              'всего заголовка; заканчивается точкой.\n'
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
    if title and title[-1] not in '.!?…':
        title += '.'  # Дзен берёт заголовком первое предложение — точка отделяет его от лида
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


def as_rich(article, photo_count):
    esc = lambda v: html.escape(v, quote=False)
    paras = lambda text: ''.join(f'<p>{esc(p)}</p>' for p in text.split('\n\n') if p.strip())
    imgs = ''.join(f'<img src="tg://photo?id=p{i}"/>' for i in range(photo_count))
    page = (f'<tg-slideshow>{imgs}</tg-slideshow>' if photo_count > 1 else imgs)
    page += f'<p><b>{esc(article["title"])}</b></p>' + paras(article['lead'])
    for s in article['sections']:
        page += f'<p><b>{esc(s["heading"])}</b></p>' + paras(s['text'])
    if article['verdict']:
        page += f'<p><b>Вердикт:</b> {esc(article["verdict"])}</p>'
    return page


def as_html_message(article):
    esc = lambda v: html.escape(v, quote=False)
    parts = [f'<b>{esc(article["title"])}</b>', esc(article['lead'])]
    for s in article['sections']:
        parts += [f'<b>{esc(s["heading"])}</b>', esc(s['text'])]
    if article['verdict']:
        parts.append(f'<b>Вердикт:</b> {esc(article["verdict"])}')
    return '\n\n'.join(parts)


CAPTION = 1000  # подпись к фото в Telegram — до 1024 знаков


def blocks(article):
    """Смысловые куски статьи по порядку: заголовок с лидом, разделы, вердикт."""
    out = [f"<b>{html.escape(article['title'], quote=False)}</b>\n\n{html.escape(article['lead'], quote=False)}"]
    for sec in article['sections']:
        out.append(f"<b>{html.escape(sec['heading'], quote=False)}</b>\n\n{html.escape(sec['text'], quote=False)}")
    if article['verdict']:
        out.append(f"<b>Вердикт:</b> {html.escape(article['verdict'], quote=False)}")
    return out


def album_captions(article, count):
    """Делит статью на count подписей (≤CAPTION каждая), не разрывая разделы: у каждого фото — свой кусок текста,
    поэтому в Дзене фото встанут между разделами. Если текст не влезает — убираем средние разделы."""
    article = dict(article, sections=list(article['sections']))
    while True:
        parts = blocks(article)
        groups = [[] for _ in range(count)]
        # раскладываем куски по фото пропорционально, сохраняя порядок
        for i, part in enumerate(parts):
            groups[min(count - 1, i * count // len(parts))].append(part)
        captions = ['\n\n'.join(g) for g in groups]
        if all(len(html.unescape(re.sub(r'<[^>]+>', '', c))) <= CAPTION for c in captions):
            return captions
        if len(article['sections']) <= 1:  # крайний случай: без разметки, обрезано по длине
            return [html.escape(html.unescape(re.sub(r'<[^>]+>', '', c))[:CAPTION], quote=False) for c in captions]
        article['sections'].pop(len(article['sections']) // 2)


def publish_album(chat_id, article, photos):
    captions = album_captions(article, len(photos))
    media = [{'type': 'photo', 'media': f'attach://p{i}', 'caption': c, 'parse_mode': 'HTML'}
             for i, c in enumerate(captions)]
    return tg.api('sendMediaGroup', {'chat_id': chat_id, 'media': json.dumps(media, ensure_ascii=False)},
                  {f'p{i}': data for i, data in enumerate(photos)})


def publish(chat_id, article, photos):
    """Альбом «фото + свой кусок текста у каждого фото» — Дзен делает из поста с текстом и картинками статью.
    DZEN_FORMAT=rich — одно Rich Message (Синхроробот Дзена теряет из него фото), text — только текст."""
    fmt = os.environ.get('DZEN_FORMAT') or 'album'
    if fmt == 'album' and len(photos) >= 2:
        return publish_album(chat_id, article, photos)
    if fmt == 'rich' and photos:
        try:
            return tg.publish_rich(chat_id, as_rich(article, len(photos)), photos)
        except RuntimeError as exc:
            if '400' not in str(exc):  # сеть/неизвестно — не повторяем, чтобы не было дубля
                raise
            print('Rich Message не принят, публикую обычным сообщением:', exc)
    return tg.api('sendMessage', {'chat_id': chat_id, 'text': as_html_message(article), 'parse_mode': 'HTML',
                                  'link_preview_options': json.dumps({'is_disabled': True})})


MIN_PHOTOS = 3
MAX_PHOTOS = 5


def gather_photos(items):
    """Фото со страниц всех источников истории; одинаковые картинки (даже другого размера) — один раз."""
    found = []
    for item in items:
        try:
            got = tg.pictures(item)
        except Exception as exc:
            print('  ! фото не получены:', type(exc).__name__)
            continue
        for data in got:
            fp = tg.looks_like(data)
            if all(bin(fp ^ tg.looks_like(old)).count('1') >= 40 for old in found):
                found.append(data)
        if len(found) >= MAX_PHOTOS:
            break
    return found[:MAX_PHOTOS]


def chat():
    chat_id = os.environ.get('DZEN_CHAT_ID', '').strip()
    if not chat_id:
        raise RuntimeError('Задайте переменную DZEN_CHAT_ID: @имя Telegram-канала для статей Дзена')
    return chat_id


def check():
    """Бот может публиковать в канал статей. Синхроробот Дзена так не проверить: Telegram не показывает ботам
    других ботов среди администраторов — его подключение видно только в Студии Дзена."""
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
    for _ in range(4):  # статье нужно минимум MIN_PHOTOS разных фото, иначе берём другую тему
        pick, related, angle = choose(items, recent, tried)
        print('Тема:', pick['title'], '|', angle)
        photos = gather_photos([pick] + related)
        if len(photos) >= MIN_PHOTOS:
            break
        print(f'Фото только {len(photos)}, нужно {MIN_PHOTOS} — беру другую тему')
        tried.append(pick['link'])
    else:
        tg.save_history(SEEN_FILE, seen, skipped=tried)
        raise RuntimeError(f'Ни к одной теме не нашлось {MIN_PHOTOS} фото — статья не опубликована')
    article = write(pick, related, angle)
    if len(as_text(article)) < MIN_TEXT:  # Gemini иногда сокращает — просим ещё раз, берём более длинный вариант
        print(f'Статья короткая ({len(as_text(article))} знаков), пишу заново')
        article = max(article, write(pick, related, angle), key=lambda a: len(as_text(a)))
    article = fit(article)
    text = as_text(article)
    print(f'Статья: {len(text)} знаков, фото: {len(photos)}\n\n{text}\n')
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
