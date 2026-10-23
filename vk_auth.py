"""Ключ профиля ВК (VK ID, приложение 54813069) для загрузки фото на стену группы.

Access token VK ID живёт 1 час и привязан к IP-адресу сервера, поэтому в каждом запуске обмениваем refresh_token
на новую пару. Оба бота ВК (кино и мир) стоят в одной очереди concurrency vk-profile, чтобы не обменивать пару одновременно.
После обмена старая пара недействительна — новый refresh_token сразу сохраняется в vk_token.enc
(зашифрован ключом из секрета VK_TOKEN_KEY: репозиторий публичный, в нём лежит только шифр).
Свежий access token передаётся следующим шагам workflow как VK_PHOTO_TOKEN (замаскирован в логах).
"""
import json
import os
from pathlib import Path
import secrets
import sys
import time
import urllib.error
import urllib.parse
from datetime import datetime, timezone

from cryptography.fernet import Fernet, InvalidToken

import post as news

CLIENT_ID = '54813069'
TOKEN_FILE = Path(__file__).with_name('vk_token.enc')
AUTH_URL = 'https://id.vk.ru/oauth2/auth'


def load(key):
    try:
        return json.loads(Fernet(key).decrypt(TOKEN_FILE.read_bytes()))
    except (InvalidToken, ValueError):
        raise RuntimeError('Не удалось расшифровать vk_token.enc: проверьте секрет VK_TOKEN_KEY') from None


def save(key, data):
    data = dict(data, updated=datetime.now(timezone.utc).isoformat(timespec='seconds'))
    temporary = TOKEN_FILE.with_suffix('.tmp')
    temporary.write_bytes(Fernet(key).encrypt(json.dumps(data).encode()))
    temporary.replace(TOKEN_FILE)


def token_request(fields):
    """POST в VK ID. Ключи в ошибках не печатаем."""
    body = urllib.parse.urlencode(dict(fields, client_id=CLIENT_ID, state=secrets.token_urlsafe(32))).encode()
    try:
        res = json.loads(news.http(AUTH_URL, body, {'Content-Type': 'application/x-www-form-urlencoded'}, 30))
    except urllib.error.HTTPError as exc:
        try:
            res = json.loads(exc.read() or b'{}')
        except Exception:
            res = {}
        res.setdefault('error', f'HTTP {exc.code}')
    if 'access_token' not in res or 'refresh_token' not in res:
        raise RuntimeError(f'VK ID не выдал ключ: {res.get("error", "")} {res.get("error_description", "")}'.strip())
    return res


def refresh(key):
    data = load(key)
    res = token_request({'grant_type': 'refresh_token', 'refresh_token': data['refresh_token'],
                         'device_id': data['device_id']})
    data.update(refresh_token=res['refresh_token'], scope=res.get('scope', data.get('scope', '')),
                access_token=res['access_token'], expires_at=int(time.time()) + int(res.get('expires_in') or 3600))
    save(key, data)  # сразу: прежний refresh_token уже недействителен
    return res['access_token'], data.get('scope', '')


def main():
    key = os.environ.get('VK_TOKEN_KEY', '').strip()
    if not key or not TOKEN_FILE.exists():
        print('Ключ профиля ВК не подключён — фото загружаться не будут (см. vk_connect.py)')
        return
    access, scope = refresh(key)
    print('::add-mask::' + access)  # GitHub скрывает значение во всех логах
    env_file = os.environ.get('GITHUB_ENV')
    if env_file:
        with open(env_file, 'a', encoding='utf-8') as env:
            env.write(f'VK_PHOTO_TOKEN={access}\n')
    print('Ключ профиля ВК обновлён; права:', scope)


if __name__ == '__main__':
    try:
        main()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
