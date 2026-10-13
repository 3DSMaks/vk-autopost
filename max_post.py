"""MAX: RSS → Gemini → JPEG → канал в MAX. Новости и темы — как в Telegram (игры и кино чередуются через пост).

Нужны секреты MAX_BOT_TOKEN (токен бота с платформы MAX для партнёров) и MAX_CHAT_ID (числовой ID канала).
Сервер MAX использует сертификат Минцифры — он лежит рядом в russian_trusted_ca.pem."""
import argparse
import html
import json
import os
import re
from pathlib import Path
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import post as news
import telegram_post as tg

API = 'https://platform-api2.max.ru'
SEEN_FILE = Path(__file__).with_name('seen_max.json')
CA_FILE = Path(__file__).with_name('russian_trusted_ca.pem')
TEXT_LIMIT = 3800  # у MAX предел 4000 символов, оставляем запас на ссылку и разметку


def ssl_context():
    ctx = ssl.create_default_context()
    ctx.load_verify_locations(CA_FILE)
    return ctx


def request(url, data=None, headers=None, method=None, timeout=60):
    req = urllib.request.Request(url, data=data, headers={'User-Agent': news.UA, **(headers or {})}, method=method)
    with urllib.request.urlopen(req, timeout=timeout, context=ssl_context()) as r:
        return r.read()


def api(method, path, params=None, body=None):
    token = os.environ.get('MAX_BOT_TOKEN', '').strip()
    if not token:
        raise RuntimeError('Добавьте секрет MAX_BOT_TOKEN')
    url = API + path + ('?' + urllib.parse.urlencode(params) if params else '')
    headers = {'Authorization': token}
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode()
        headers['Content-Type'] = 'application/json'
    # Токен не печатаем; при неопределённом результате отправки не повторяем вслепую.
    try:
        return json.loads(request(url, data, headers, method) or b'{}')
    except urllib.error.HTTPError as exc:
        try:
            info = json.loads(exc.read() or b'{}')
        except Exception:
            info = {}
        err = RuntimeError(f'MAX {method} {path}: HTTP {exc.code} {info.get("code", "")} {info.get("message", "")}'.strip())
        err.code = info.get('code')
        raise err from None
    except Exception:
        raise RuntimeError(f'MAX {method} {path}: ошибка сети или ответа. Перед повторной отправкой проверьте канал.') from None


def chat_id():
    value = os.environ.get('MAX_CHAT_ID', '').strip()
    if not value.lstrip('-').isdigit():
        raise RuntimeError('Добавьте секрет MAX_CHAT_ID: числовой ID канала, например -71234567890123')
    return value


def check_channel():
    cid = chat_id()
    bot = api('GET', '/me')
    chat = api('GET', f'/chats/{cid}')
    if chat.get('type') != 'channel':
        raise RuntimeError('MAX_CHAT_ID должен указывать на канал')
    me = api('GET', f'/chats/{cid}/members/me')
    if not (me.get('is_owner') or (me.get('is_admin') and 'write' in (me.get('permissions') or []))):
        raise RuntimeError('Сделайте бота администратором канала MAX с правом публикации (write)')
    print(f'Доступ подтверждён: бот {bot.get("first_name", "")} → канал {chat.get("title", "")}')
    return cid


def find_chats():
    """Показывает ID каналов и чатов из недавних событий бота (bot_added и др.). ID канала — не секрет."""
    res = api('GET', '/updates', {'limit': 100, 'timeout': 5})
    ids = []
    for u in res.get('updates', []):
        cid = u.get('chat_id') or ((u.get('message') or {}).get('recipient') or {}).get('chat_id')
        if cid and cid not in ids:
            ids.append(cid)
    if not ids:
        print('Событий нет. Удалите бота из канала и добавьте снова (подписчиком, затем администратором), '
              'и сразу запустите find ещё раз.')
        return []
    for cid in ids:
        try:
            chat = api('GET', f'/chats/{cid}')
            print(f'ID: {cid}  |  {chat.get("type", "?")}  |  {chat.get("title", "")}')
        except RuntimeError:
            print(f'ID: {cid}  |  (нет доступа к информации)')
    return ids


def upload_image(data):
    """POST /uploads → загрузка файла по выданному адресу → {"photos": {...}} для вложения."""
    target = api('POST', '/uploads', {'type': 'image'})
    boundary = 'max' + uuid.uuid4().hex
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="data"; filename="photo.jpg"\r\n'
            f'Content-Type: image/jpeg\r\n\r\n').encode() + data + f'\r\n--{boundary}--\r\n'.encode()
    headers = {'Content-Type': f'multipart/form-data; boundary={boundary}',
               'Authorization': os.environ.get('MAX_BOT_TOKEN', '').strip()}
    try:
        res = json.loads(request(target['url'], body, headers, 'POST', 90) or b'{}')
    except Exception:
        raise RuntimeError('MAX: не удалось загрузить фото') from None
    if res.get('photos'):
        return {'photos': res['photos']}
    if res.get('token') or target.get('token'):
        return {'token': res.get('token') or target['token']}
    raise RuntimeError('MAX: после загрузки фото не пришёл токен')


def without_hashtags(text):
    """Убирает строки, состоящие только из хэштегов, и хэштеги в конце строк."""
    lines = []
    for line in text.split('\n'):
        words = line.split()
        if words and all(w.startswith('#') for w in words):
            continue
        lines.append(re.sub(r'(\s+#[\w-]+)+\s*$', '', line))
    return re.sub(r'\n{3,}', '\n\n', '\n'.join(lines)).strip()


NO_QUESTION = ('Не задавай вопросов читателям и не призывай к обсуждению — комментарии в канале закрыты. '
               'Закончи пост фактом или коротким выводом.')


def without_closing_question(text):
    """Убирает вопросы к читателям в конце поста (комментарии в MAX закрыты)."""
    paragraphs = [p for p in text.strip().split('\n\n') if p.strip()]
    while paragraphs:
        sentences = re.split(r'(?<=[.!?…])\s+', paragraphs[-1].strip())
        # с конца убираем «хвосты» из одних эмодзи/знаков и предложения-вопросы
        while sentences and (not re.search(r'\w', sentences[-1]) or re.search(r'\?\W*$', sentences[-1])):
            sentences.pop()
        if sentences:
            paragraphs[-1] = ' '.join(sentences)
            break
        paragraphs.pop()
    return '\n\n'.join(paragraphs)


def message_text(text, link):
    text = without_closing_question(without_hashtags(text.rsplit('\n\nИсточник:', 1)[0]))[:TEXT_LIMIT].rstrip()
    return html.escape(text) + '\n\n<a href="' + html.escape(link, quote=True) + '">Источник</a>'


def publish(cid, text, link, photos):
    if not photos:
        raise RuntimeError('Фото не найдены: публикация отменена')
    attachments = [{'type': 'image', 'payload': upload_image(p)} for p in photos]
    body = {'text': message_text(text, link), 'format': 'html', 'attachments': attachments}
    # Сразу после загрузки MAX может ответить attachment.not.ready — ждём и пробуем снова.
    for wait in (2, 4, 8, 16, 30, None):
        try:
            return api('POST', '/messages', {'chat_id': cid}, body)
        except RuntimeError as exc:
            if getattr(exc, 'code', None) != 'attachment.not.ready' or wait is None:
                raise
            print(f'Фото ещё обрабатывается, жду {wait} с')
            time.sleep(wait)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--find-chat', action='store_true')
    args = parser.parse_args()
    if args.find_chat:
        find_chats()
        return
    cid = check_channel()
    if args.check:
        print('Проверка завершена. Ничего не опубликовано.')
        return
    seen = json.loads(SEEN_FILE.read_text(encoding='utf-8')) if SEEN_FILE.exists() else []
    candidates, topic = [], None
    for topic in tg.topic_order(tg.published_count(seen)):
        candidates = tg.fresh_for(topic, tg.history_links(seen))
        if candidates:
            break
    if not candidates:
        print('Свежих неопубликованных новостей нет')
        return
    print('Тема:', 'видеоигры' if topic == 'games' else 'кино и сериалы', '| свежих новостей:', len(candidates))
    candidates, repeats = tg.drop_duplicates(candidates, tg.recent_topics(seen))
    if not candidates:
        tg.save_history(SEEN_FILE, seen, skipped=repeats)
        print('Все свежие новости — повторы уже опубликованных')
        return
    news.PROMPT = (tg.selection_prompt(topic, seen).replace('Telegram-канала', 'канала в MAX')
                   .replace('В конце — вопрос для обсуждения.', NO_QUESTION))
    pick, text, photos, skipped = tg.pick_with_photos(candidates)
    skipped = repeats + skipped
    if not photos:
        tg.save_history(SEEN_FILE, seen, skipped=skipped)
        raise RuntimeError('Ни к одной из выбранных новостей не нашлось фото — публикация отменена; '
                           'эти новости больше не будут предлагаться')
    result = publish(cid, text, pick['link'], photos)
    tg.save_history(SEEN_FILE, seen, pick['link'], skipped, title=pick.get('title'))
    message = (result or {}).get('message') or {}
    print('Опубликовано в MAX:', message.get('url') or (message.get('body') or {}).get('mid') or 'ок')


if __name__ == '__main__':
    try:
        main()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
