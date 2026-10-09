"""Telegram: RSS → Gemini → JPEG → канал. VK-токены не используются.

Темы чередуются через пост: игры, кино, игры, кино… (по числу уже опубликованных новостей).
Если по теме часа свежих новостей нет, берётся другая тема."""
import argparse
import hashlib
import html
import io
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.parse
import uuid
from datetime import datetime, timezone

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


def caption(text, link):
    # Ссылка скрыта под словом «Источник»; лимит считаем консервативно в UTF-16.
    text = text.rsplit('\n\nИсточник:', 1)[0]
    text = text.encode('utf-16-le')[:980 * 2].decode('utf-16-le', errors='ignore').rstrip()
    return html.escape(text) + '\n\n<a href="' + html.escape(link, quote=True) + '">Источник</a>'


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
            same = any(bin(fp ^ other).count('1') < 26 for other in prints)  # < 10% различий из 256
            if digest not in hashes and not same and len(data) <= 10 * 1024 * 1024:
                hashes.add(digest)
                prints.append(fp)
                out.append(data)
            if len(out) == 3:
                break
        except Exception:
            print('Кандидат изображения недоступен, пробую следующий')
    return out


def publish(chat_id, text, link, photos):
    if not photos:
        raise RuntimeError('Фото не найдены: публикация отменена')
    fields = {'chat_id': chat_id}
    if len(photos) == 1:
        fields.update(caption=caption(text, link), parse_mode='HTML')
        return api('sendPhoto', fields, {'photo': photos[0]})
    media = [{'type': 'photo', 'media': f'attach://photo{i}'} for i in range(len(photos))]
    media[0].update(caption=caption(text, link), parse_mode='HTML')
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
    for topic in topic_order(len(seen)):
        candidates = fresh_for(topic, set(seen))
        if candidates:
            break
    if not candidates:
        print('Свежих неопубликованных новостей нет')
        return
    print('Тема:', 'видеоигры' if topic == 'games' else 'кино и сериалы', '| свежих новостей:', len(candidates))
    news.PROMPT = TOPICS[topic][1]
    pick, text = news.write_post(candidates)
    result = publish(chat_id, text, pick['link'], pictures(pick))
    updated = list(dict.fromkeys(seen + [pick['link']]))[-1000:]
    temporary = SEEN_FILE.with_suffix('.tmp')
    temporary.write_text(json.dumps(updated, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(SEEN_FILE)
    messages = result if isinstance(result, list) else [result]
    print('Опубликовано; ID сообщений:', ', '.join(str(m['message_id']) for m in messages))


if __name__ == '__main__':
    try:
        main()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
