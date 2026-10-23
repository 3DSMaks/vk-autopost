"""Мировые новости во ВКонтакте: быстро и только подтверждённое.

Каждые 10 минут: ленты крупных агентств (российских и международных) за последние 3 часа → Gemini группирует
сообщения об одном событии → публикуем только события, о которых сообщили минимум ДВА разных агентства
(число источников считает код, а не Gemini) → текст строго по фактам из этих сообщений, с указанием агентств →
фото со страницы новости, если нашлось (без фото тоже публикуем: скорость важнее) → стена группы.
История — seen_world.json (ссылки всех сообщений опубликованного события + title:<заголовок>).
"""
import html
import json
import os
from pathlib import Path
import re
import sys
import urllib.parse
from datetime import datetime, timezone

import post as news
import telegram_post as tg
import vk_post

SEEN_FILE = Path(__file__).with_name('seen_world.json')
GOOGLE = 'https://news.google.com/rss/search?hl=en-US&gl=US&ceid=US:en&q=when:3h+site:'
FEEDS = {  # агентство: RSS. Reuters и AP не дают своих лент — берём их заголовки из Google News (только для подтверждения)
    'ТАСС': 'https://tass.ru/rss/v2.xml',
    'РИА Новости': 'https://ria.ru/export/rss2/archive/index.xml',
    'Интерфакс': 'https://www.interfax.ru/rss.asp',
    'Коммерсантъ': 'https://www.kommersant.ru/RSS/news.xml',
    'РБК': 'https://rssexport.rbc.ru/rbcnews/news/30/full.rss',
    'BBC': 'https://feeds.bbci.co.uk/news/world/rss.xml',
    'Al Jazeera': 'https://www.aljazeera.com/xml/rss/all.xml',
    'The Guardian': 'https://www.theguardian.com/world/rss',
    'France 24': 'https://www.france24.com/en/rss',
    'Reuters': GOOGLE + 'reuters.com',
    'AP': GOOGLE + 'apnews.com',
}
MAX_AGE_MINUTES = 180
PER_FEED = 40
MIN_SOURCES = 2
MIN_IMPORTANCE = 6   # из 10: ниже — местные происшествия, светская хроника и т.п.
MAX_PER_RUN = 2


def decode(raw):
    m = re.search(rb'encoding=["\']([\w-]+)', raw[:200])
    try:
        return raw.decode(m.group(1).decode() if m else 'utf-8', 'ignore')
    except LookupError:
        return raw.decode('utf-8', 'ignore')


def fetch_feed(source, url, now):
    try:
        xml = decode(news.http(url, timeout=15))
    except Exception as exc:
        print(f'  ! {source} не ответил: {type(exc).__name__}')
        return []
    items = []
    for block in re.findall(r'<item[\s>][\s\S]*?</item>', xml, re.I)[:PER_FEED]:
        link, title = news.tag(block, 'link'), news.tag(block, 'title')
        date = news.parse_date(news.tag(block, 'pubDate'))
        if not link or not title or not date or (now - date).total_seconds() > MAX_AGE_MINUTES * 60:
            continue
        if url.startswith(GOOGLE):
            title = re.sub(r'\s+-\s+[^-]+$', '', title)  # «Заголовок - Reuters»
        img = re.search(r'<(?:enclosure|media:content|media:thumbnail)[^>]*url=["\']([^"\']+)["\']', block, re.I)
        items.append({'source': source, 'title': title, 'link': link, 'date': date,
                      'summary': '' if url.startswith(GOOGLE) else news.tag(block, 'description')[:300],
                      'image': html.unescape(img.group(1)) if img else ''})
    return items


def fresh_items(seen):
    now = datetime.now(timezone.utc)
    used = tg.history_links(seen)
    items = []
    for source, url in FEEDS.items():
        got = [i for i in fetch_feed(source, url, now) if i['link'] not in used]
        print(f'{source}: {len(got)}')
        items += got
    items.sort(key=lambda i: i['date'], reverse=True)
    return items


def can_open(item):
    return not item['link'].startswith('https://news.google.com/')


SELECT_SCHEMA = {'type': 'OBJECT', 'properties': {'events': {'type': 'ARRAY', 'items': {
    'type': 'OBJECT', 'properties': {
        'items': {'type': 'ARRAY', 'items': {'type': 'INTEGER'}},
        'title': {'type': 'STRING'},
        'importance': {'type': 'INTEGER'},
    }, 'required': ['items', 'title', 'importance']}}}, 'required': ['events']}


def select_prompt(items, recent):
    lines = [f"{n}. [{i['source']}, {i['date'].strftime('%H:%M')} UTC] {i['title']}" + (f" — {i['summary'][:200]}" if i['summary'] else '')
             for n, i in enumerate(items)]
    done = '\n'.join('- ' + t for t in recent) or '- (пока ничего)'
    return ('Ты выпускающий редактор новостного сообщества о главных событиях в мире. Ниже свежие сообщения '
            'информационных агентств (номер, агентство, время, заголовок).\n'
            'Сгруппируй сообщения об ОДНОМ И ТОМ ЖЕ конкретном событии (разные агентства, разные языки) и верни '
            'до 5 событий для публикации, самые важные и свежие первыми. Для каждого: items — номера ВСЕХ сообщений '
            'об этом событии, title — короткий фактический заголовок на русском, importance — важность для '
            'читателя со всего мира от 1 до 10 (10 — событие мирового масштаба; 6 — заметное событие в крупной '
            'стране; 1–4 — местные происшествия, курьёзы, спорт без рекордов, шоу-бизнес, реклама).\n'
            'Правила:\n'
            '- Только события, о которых сообщили минимум два РАЗНЫХ агентства. Если сообщение лишь пересказывает '
            'другое агентство («как сообщает ТАСС», «according to Reuters») — его не считай отдельным источником '
            'и не включай.\n'
            '- Не бери слухи, анонимные «источники в СМИ», заявления без подтверждения, прогнозы, мнения, '
            'колонки, аналитику, обзоры, расписания и прямые трансляции.\n'
            '- Не бери события, которые уже опубликованы (список ниже), если нет существенно нового развития; '
            'если оно есть — заголовок должен говорить именно о новом.\n\n'
            f'Уже опубликовано:\n{done}\n\nСообщения:\n' + '\n'.join(lines))


def confirmed_events(items, recent):
    """События, подтверждённые 2+ разными агентствами, по важности. Число агентств проверяет код."""
    if not items:
        return []
    data = tg.gemini_json([{'text': select_prompt(items, recent)}], SELECT_SCHEMA)
    events = []
    for ev in data.get('events') or []:
        group = [items[n] for n in dict.fromkeys(ev.get('items') or []) if isinstance(n, int) and 0 <= n < len(items)]
        sources = list(dict.fromkeys(i['source'] for i in group))
        verdict = 'ok'
        if len(sources) < MIN_SOURCES:
            verdict = 'один источник — ждём подтверждения'
        elif (ev.get('importance') or 0) < MIN_IMPORTANCE:
            verdict = 'неважно'
        print(f"  [{ev.get('importance')}] {ev.get('title')} — {', '.join(sources)}: {verdict}")
        if verdict == 'ok':
            events.append({'title': ev.get('title') or group[0]['title'], 'importance': ev['importance'],
                           'items': group, 'sources': sources})
    return events


WRITE_SCHEMA = {'type': 'OBJECT', 'properties': {'title': {'type': 'STRING'}, 'body': {'type': 'STRING'}},
                'required': ['title', 'body']}


def write(event):
    reports = []
    for item in event['items'][:6]:
        text = tg.article_text(item['link'], 2500) if can_open(item) and len(reports) < 3 else ''
        reports.append(f"### {item['source']}: {item['title']}\n{text or item['summary']}")
    prompt = ('Напиши новость для сообщества ВКонтакте о главных событиях в мире. Язык — русский.\n'
              'Пиши ТОЛЬКО факты, которые есть в сообщениях агентств ниже. Ничего не добавляй от себя: никаких '
              'оценок, прогнозов, предположений, эмоций, вопросов читателям и хэштегов. Цифры, имена, должности и '
              'места — точно как в сообщениях. Если агентства расходятся в деталях — укажи обе версии со ссылкой '
              'на агентство («по данным ТАСС…», «Reuters пишет…»). Заявления сторон — только с указанием, кто '
              'заявил. Нейтральный тон информационного агентства.\n'
              'title — заголовок до 90 знаков: что произошло, без кликбейта и кавычек-ёлочек вокруг всего заголовка.\n'
              'body — 2–4 коротких абзаца (всего 300–900 знаков), между абзацами пустая строка: первый абзац — '
              'главное (кто, что, где, когда), дальше — подробности и контекст из сообщений. Без HTML и markdown.\n\n'
              f'Событие: {event["title"]}\n\n' + '\n\n'.join(reports))
    data = tg.gemini_json([{'text': prompt}], WRITE_SCHEMA)
    title = re.sub(r'[*_#<>]', '', data['title']).strip()
    body = re.sub(r'[*_#<>]', '', data['body']).strip()
    body = re.sub(r'\n{3,}', '\n\n', body)
    mark = '⚡' if event['importance'] >= 8 else '🌍'
    return f"{mark} {title}\n\n{body}\n\nИсточники: {', '.join(event['sources'])}"


def photos_for(event):
    for item in [i for i in event['items'] if can_open(i)][:2]:
        try:
            found = tg.pictures(item)
        except Exception as exc:
            print('  ! фото не получены:', type(exc).__name__)
            continue
        if found:
            return found[:2]
    return []


def post_token():
    return os.environ.get('VK_WORLD_TOKEN') or news.photo_token()


def group_id():
    """WORLD_GROUP_ID: число, короткое имя или ссылка на группу."""
    raw = os.environ.get('WORLD_GROUP_ID', '').strip()
    if not raw:
        raise RuntimeError('Не задан WORLD_GROUP_ID (адрес группы мировых новостей)')
    url = raw if '//' in raw else ('https://' if '/' in raw else 'https://vk.ru/') + raw
    name = urllib.parse.urlsplit(url).path.strip('/')
    name = re.sub(r'^(club|public|event)(\d+)$', r'\2', name)
    if name.isdigit():
        return name
    return str(news.vk('groups.getById', token=news.photo_token(), group_id=name)['groups'][0]['id'])


def publish(event):
    message = write(event)
    photos = photos_for(event)
    attachments = vk_post.upload(photos) if photos else []
    params = dict(owner_id='-' + news.GROUP_ID, from_group=1, message=message)
    if attachments:
        params.update(attachments=','.join(attachments),
                      primary_attachments_mode='carousel' if len(attachments) > 1 else 'grid')
    res = news.vk('wall.post', token=post_token(), **params)
    print(f"Опубликовано: https://vk.ru/wall-{news.GROUP_ID}_{res['post_id']} (фото: {len(attachments)})\n{message}\n")


def check():
    now = datetime.now(timezone.utc)
    for source, url in FEEDS.items():
        got = fetch_feed(source, url, now)
        newest = f"{int((now - max(i['date'] for i in got)).total_seconds() // 60)} мин назад" if got else '—'
        print(f'{source}: {len(got)} за 3 ч, последняя {newest}')
    try:
        mine = news.vk('groups.get', token=news.photo_token(), filter='admin', extended=1)['items']
        print('Группы, где профиль — администратор:', ', '.join(f"{g['name']} (id {g['id']})" for g in mine) or 'нет')
    except Exception as exc:
        print('Список групп недоступен:', exc)
    if os.environ.get('WORLD_GROUP_ID'):
        news.GROUP_ID = group_id()
        print('Группа:', news.vk('groups.getById', token=news.photo_token(), group_id=news.GROUP_ID)['groups'][0]['name'])
        news.photo_server()
        print('Загрузка фото в группу: доступна')


def main():
    if '--check' in sys.argv:
        check()
        return
    dry = '--dry' in sys.argv
    news.GROUP_ID = group_id()
    seen = json.loads(SEEN_FILE.read_text(encoding='utf-8')) if SEEN_FILE.exists() else []
    items = fresh_items(seen)
    print('Новых сообщений:', len(items))
    recent = [s[len(tg.TITLE):] for s in seen if s.startswith(tg.TITLE)][-40:]
    events = confirmed_events(items[:150], recent)
    if not events:
        print('Подтверждённых новых событий нет — ждём следующего запуска.')
        return
    for event in events[:MAX_PER_RUN]:
        if dry:
            print(write(event))
            continue
        publish(event)
        links = [i['link'] for i in event['items']]
        tg.save_history(SEEN_FILE, seen, links[0], links[1:], title=event['title'])
        seen = json.loads(SEEN_FILE.read_text(encoding='utf-8'))


if __name__ == '__main__':
    try:
        main()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
