"""Telegram: RSS → Gemini → JPEG → канал. VK-токены не используются."""
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

from PIL import Image
import post as news

SEEN_FILE = Path(__file__).with_name('seen_telegram.json')


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


def pictures(pick):
    urls = news.image_urls(news.page_images(pick['link']) + [pick.get('image')], pick['link'])
    out, hashes = [], set()
    for url in urls:
        try:
            data = news.download_jpeg(url, pick['link'])
            with Image.open(io.BytesIO(data)) as im:
                if min(im.size) < 100 or max(im.size) / min(im.size) > 20:
                    continue
            digest = hashlib.sha256(data).digest()
            if digest not in hashes and len(data) <= 10 * 1024 * 1024:
                hashes.add(digest)
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
    candidates = news.fresh_news(set(seen))
    if not candidates:
        print('Свежих неопубликованных новостей нет')
        return
    news.PROMPT = news.PROMPT.replace('сообщества ВКонтакте', 'Telegram-канала')
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
