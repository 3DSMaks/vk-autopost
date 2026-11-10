"""ВКонтакте: тот же механизм, что у Telegram и MAX, но тема группы — кино и сериалы.

Свежие новости → отсев повторов (Gemini) → выбор и текст (Gemini, голос канала) → фото лучшего качества
(проверка Gemini) → загрузка на стену ключом профиля (VK_PHOTO_TOKEN из vk_auth.py) → пост от имени группы,
несколько фото — каруселью. История — seen.json.
"""
import html
import json
from pathlib import Path
import re
import sys
import time

import post as news
import telegram_post as tg

SEEN_FILE = Path(__file__).with_name('seen.json')
TOPIC = 'cinema'


def plain(formatted):
    """ВК не понимает HTML: убираем теги, врезки становятся обычными строками."""
    text = re.sub(r'</?blockquote>', '\n', formatted)
    text = html.unescape(re.sub(r'<[^>]+>', '', text))
    return re.sub(r'\n{3,}', '\n\n', text).strip()


UPLOAD_ATTEMPTS = 4


def upload_one(data, n):
    """Одно фото на стену группы. Серверы загрузки ВК отвечают нестабильно (иногда отдают пустое «photo»
    даже на обычный JPEG) — поэтому при отказе берём свежий адрес загрузки и пробуем снова."""
    body, headers = news.multipart('photo', f'photo{n}.jpg', 'image/jpeg', data)
    for attempt in range(1, UPLOAD_ATTEMPTS + 1):
        up = json.loads(news.http(news.photo_server(), body, headers, 60))
        if not up.get('error') and up.get('photo') not in (None, '', '[]'):
            ph = news.vk('photos.saveWallPhoto', token=news.photo_token(), group_id=news.GROUP_ID,
                         server=up['server'], photo=up['photo'], hash=up['hash'])[0]
            return f"photo{ph['owner_id']}_{ph['id']}" + (f"_{ph['access_key']}" if ph.get('access_key') else '')
        w, h = tg.image_size(data)
        print(f'  ! ВК отклонил фото {n} ({w}x{h}, {len(data) // 1024} КБ), попытка {attempt}/{UPLOAD_ATTEMPTS}')
        time.sleep(3 * attempt)
    raise ValueError('Сервер ВК не принял изображение')


def upload(images):
    """Загружает готовые JPEG на стену группы; возвращает вложения photo<owner>_<id>."""
    out = []
    for i, data in enumerate(images, 1):
        try:
            out.append(upload_one(data, i))
        except news.VKError:
            raise
        except Exception as exc:
            print(f'  ! фото {i} не загрузилось: {type(exc).__name__}')
    return out


def inspect(count=4):
    """Диагностика: последние посты группы, их фото и доступность ссылок на фото (ничего не публикует)."""
    # эксперимент: какие варианты одного настоящего фото принимает сервер загрузки ВК (без сохранения и без поста)
    from PIL import Image
    import io

    def try_upload(label, data):
        body, headers = news.multipart('photo', 'test.jpg', 'image/jpeg', data)
        up = json.loads(news.http(news.photo_server(), body, headers, 60))
        ok = up.get('photo') not in (None, '', '[]')
        w, h = tg.image_size(data)
        print(f'Пробная загрузка [{label}] {w}x{h} {len(data) // 1024} КБ:', 'принята' if ok else 'ОТКЛОНЕНА')

    def jpeg(im, size=None, quality=90, optimize=False):
        im = im.convert('RGB')
        if size:
            im.thumbnail((size, size))
        buf = io.BytesIO()
        im.save(buf, 'JPEG', quality=quality, optimize=optimize)
        return buf.getvalue()

    try_upload('однотонная', jpeg(Image.new('RGB', (800, 450), (40, 80, 160))))
    real = news.download_jpeg('https://variety.com/wp-content/uploads/2026/10/jeremy-jessica.jpg', 'https://variety.com')
    try_upload('как сейчас в боте', real)
    with Image.open(io.BytesIO(real)) as im:
        try_upload('1280 q90', jpeg(im, 1280))
        try_upload('1280 q85 optimize', jpeg(im, 1280, 85, True))
        try_upload('800 q90', jpeg(im, 800))
    posts = news.vk('wall.get', token=news.photo_token(), owner_id='-' + news.GROUP_ID, count=count)['items']
    for p in posts:
        print(f"Пост {p['id']}: {p.get('text', '')[:60]!r}")
        for a in p.get('attachments', []):
            if a.get('type') != 'photo':
                print('   вложение', a.get('type'))
                continue
            ph = a['photo']
            sizes = ph.get('sizes') or []
            best = max(sizes, key=lambda z: z.get('width', 0) * z.get('height', 0)) if sizes else {}
            status = '—'
            if best.get('url'):
                try:
                    data = news.http(best['url'], timeout=20)
                    status = f'{len(data) // 1024} КБ'
                except Exception as exc:
                    status = f'ошибка {type(exc).__name__}'
            print(f"   фото {ph['owner_id']}_{ph['id']}: размеров {len(sizes)}, лучший {best.get('width')}x{best.get('height')}, загрузка: {status}")


def stats(pages=3):
    """Статистика группы для оценки (подписчики, частота постов, просмотры, реакции). Ничего не публикует."""
    from statistics import mean, median
    from datetime import datetime, timezone
    group = news.vk('groups.getById', token=news.photo_token(), group_id=news.GROUP_ID, fields='members_count')['groups'][0]
    posts = []
    for page in range(pages):
        items = news.vk('wall.get', token=news.photo_token(), owner_id='-' + news.GROUP_ID, count=100, offset=page * 100)['items']
        posts += [p for p in items if not p.get('is_pinned')]
        if len(items) < 100:
            break
    if not posts:
        print('Постов нет')
        return
    get = lambda p, k: (p.get(k) or {}).get('count', 0)
    days = max(1, (posts[0]['date'] - posts[-1]['date']) / 86400)
    views = [get(p, 'views') for p in posts]
    react = [get(p, 'likes') + get(p, 'comments') + get(p, 'reposts') for p in posts]
    first = datetime.fromtimestamp(posts[-1]['date'], timezone.utc).date()
    print(f"Группа: {group['name']}, подписчиков: {group.get('members_count')}")
    print(f'Постов: {len(posts)} с {first} ({len(posts) / days:.1f} в день)')
    print(f'Просмотры: среднее {mean(views):.0f}, медиана {median(views):.0f}, макс {max(views)}')
    for k in ('likes', 'comments', 'reposts'):
        vals = [get(p, k) for p in posts]
        print(f'{k}: среднее {mean(vals):.1f}, медиана {median(vals):.0f}, всего {sum(vals)}')
    print(f'ER (реакции / просмотры): {sum(react) / max(1, sum(views)) * 100:.2f}%')
    print(f"ERR (реакции на пост / подписчики): {mean(react) / max(1, group.get('members_count') or 1) * 100:.2f}%")
    for label, chunk in (('последние 30 постов', posts[:30]), ('первые 30 постов', posts[-30:])):
        print(f"  {label}: просмотры в среднем {mean(get(p, 'views') for p in chunk):.0f}, "
              f"лайки {mean(get(p, 'likes') for p in chunk):.1f}")
    print('Топ-5 по просмотрам:')
    for p in sorted(posts, key=lambda p: get(p, 'views'), reverse=True)[:5]:
        print(f"  {get(p, 'views')} просм., {get(p, 'likes')} лайков: {p.get('text', '')[:70]!r}")


def main():
    if '--stats' in sys.argv:
        stats()
        return
    if '--inspect' in sys.argv:
        inspect()
        return
    news.photo_server()  # сначала проверяем право загрузки фото, чтобы не тратить запросы к Gemini
    seen = json.loads(SEEN_FILE.read_text(encoding='utf-8')) if SEEN_FILE.exists() else []
    candidates = tg.fresh_for(TOPIC, tg.history_links(seen))
    print('Свежих новостей:', len(candidates))
    if not candidates:
        print('Публиковать нечего — ждём следующего запуска.')
        return
    candidates, repeats = tg.drop_duplicates(candidates, tg.recent_topics(seen))
    if not candidates:
        tg.save_history(SEEN_FILE, seen, skipped=repeats)
        print('Все свежие новости — повторы уже опубликованных')
        return
    news.PROMPT = tg.selection_prompt(TOPIC, seen).replace('Telegram-канала', 'сообщества ВКонтакте')
    skipped = list(repeats)
    for _ in range(2):  # если ВК не принял фото новости — пропускаем её и берём другую
        pick, text, photos, missing = tg.pick_with_photos(candidates)
        skipped += missing
        if not photos:
            tg.save_history(SEEN_FILE, seen, skipped=skipped)
            raise RuntimeError('Ни к одной из выбранных новостей не нашлось фото — пост не опубликован')
        attachments = upload(photos)
        if attachments:
            break
        print('ВК не принял фото этой новости, беру другую:', pick['link'])
        skipped.append(pick['link'])
        candidates = [c for c in candidates if c['link'] not in skipped]
        for i, c in enumerate(candidates):
            c['index'] = i
    else:
        tg.save_history(SEEN_FILE, seen, skipped=skipped)
        raise RuntimeError('ВК не принял фото ни одной из выбранных новостей — пост не опубликован; они пропущены')
    formatted = tg.tg_format(pick, text, channel='ВКонтакте')
    body = plain(formatted) if formatted else text.rsplit('\n\nИсточник:', 1)[0]
    message = f"{body}\n\nИсточник: {pick['link']}" if tg.SHOW_SOURCE else body
    res = news.vk('wall.post', owner_id='-' + news.GROUP_ID, from_group=1, message=message,
                  attachments=','.join(attachments),
                  primary_attachments_mode='carousel' if len(attachments) > 1 else 'grid')
    tg.save_history(SEEN_FILE, seen, pick['link'], skipped, title=pick.get('title'))
    print(f"Опубликовано: https://vk.ru/wall-{news.GROUP_ID}_{res['post_id']} (фото: {len(attachments)})")


if __name__ == '__main__':
    try:
        main()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
