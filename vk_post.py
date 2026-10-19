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

import post as news
import telegram_post as tg

SEEN_FILE = Path(__file__).with_name('seen.json')
TOPIC = 'cinema'


def plain(formatted):
    """ВК не понимает HTML: убираем теги, врезки становятся обычными строками."""
    text = re.sub(r'</?blockquote>', '\n', formatted)
    text = html.unescape(re.sub(r'<[^>]+>', '', text))
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def upload(images):
    """Загружает готовые JPEG на стену группы; возвращает вложения photo<owner>_<id>.
    Адрес загрузки берём свежий: выбор новости и проверка фото Gemini могут занять несколько минут."""
    out = []
    server = news.photo_server()
    for i, data in enumerate(images):
        try:
            body, headers = news.multipart('photo', f'photo{i + 1}.jpg', 'image/jpeg', data)
            up = json.loads(news.http(server, body, headers, 60))
            if up.get('error') or not up.get('photo') or up['photo'] == '[]':
                w, h = tg.image_size(data)
                print(f'  ! ВК отклонил фото {i + 1} ({w}x{h}, {len(data) // 1024} КБ): '
                      f'{json.dumps({k: v for k, v in up.items() if k != "hash"}, ensure_ascii=False)[:200]}')
                raise ValueError('Сервер ВК не принял изображение')
            ph = news.vk('photos.saveWallPhoto', token=news.photo_token(), group_id=news.GROUP_ID,
                         server=up['server'], photo=up['photo'], hash=up['hash'])[0]
            out.append(f"photo{ph['owner_id']}_{ph['id']}" + (f"_{ph['access_key']}" if ph.get('access_key') else ''))
        except news.VKError:
            raise
        except Exception as exc:
            print(f'  ! фото {i + 1} не загрузилось: {type(exc).__name__}')
    return out


def inspect(count=4):
    """Диагностика: последние посты группы, их фото и доступность ссылок на фото (ничего не публикует)."""
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


def main():
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
