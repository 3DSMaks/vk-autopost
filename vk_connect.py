"""Одноразовое подключение ключа профиля ВК (запускать на своём компьютере, из папки репозитория).

1. Открывает вход VK ID приложения 54813069 с правами wall photos groups.
2. Вы подтверждаете доступ и вставляете адрес страницы, на которую вас перебросило.
3. Ключ проверяется (загрузка фото в группу), шифруется новым ключом шифрования и сохраняется в vk_token.enc;
   ключ шифрования записывается в секрет GitHub VK_TOKEN_KEY; vk_token.enc отправляется в репозиторий.
Сами ключи на экран не выводятся. Нужны: pip install cryptography pynacl, вход в GitHub через git.
"""
import base64
import hashlib
import json
import secrets
import subprocess
import sys
import urllib.parse
import urllib.request
import webbrowser

from cryptography.fernet import Fernet
from nacl import encoding, public

import post as news
import vk_auth

sys.stdout.reconfigure(encoding='utf-8')
REDIRECT = 'https://maxstopcadr.app.n8n.cloud/rest/oauth2-credential/callback'
SCOPE = 'wall photos groups'
REPO = '3DSMaks/vk-autopost'


def github_token():
    out = subprocess.run(['git', 'credential', 'fill'], input='protocol=https\nhost=github.com\n\n',
                         capture_output=True, text=True).stdout
    return [l.split('=', 1)[1] for l in out.splitlines() if l.startswith('password=')][0]


def set_secret(name, value):
    headers = {'Authorization': 'Bearer ' + github_token(), 'Accept': 'application/vnd.github+json'}
    base = f'https://api.github.com/repos/{REPO}/actions/secrets'
    key = json.load(urllib.request.urlopen(urllib.request.Request(base + '/public-key', headers=headers)))
    sealed = public.SealedBox(public.PublicKey(key['key'].encode(), encoding.Base64Encoder())).encrypt(value.encode())
    body = json.dumps({'encrypted_value': base64.b64encode(sealed).decode(), 'key_id': key['key_id']}).encode()
    urllib.request.urlopen(urllib.request.Request(f'{base}/{name}', data=body, headers=headers, method='PUT'))


def main():
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    state = secrets.token_urlsafe(32)
    url = 'https://id.vk.ru/authorize?' + urllib.parse.urlencode({
        'response_type': 'code', 'client_id': vk_auth.CLIENT_ID, 'redirect_uri': REDIRECT, 'scope': SCOPE,
        'state': state, 'code_challenge': challenge, 'code_challenge_method': 'S256', 'prompt': 'consent'})
    print('Откроется вход ВКонтакте для приложения «автопостинг» с правами: стена, фото, группы.')
    print('1) Подтвердите доступ.  2) Скопируйте ВЕСЬ адрес открывшейся страницы и вставьте сюда, затем Enter.\n')
    webbrowser.open(url)
    back = input('Адрес страницы: ').strip()
    q = urllib.parse.parse_qs(urllib.parse.urlparse(back).query)
    if q.get('state', [''])[0] != state or 'code' not in q:
        sys.exit('В адресе нет кода подтверждения или он не от этого входа. Запустите заново.')
    res = vk_auth.token_request({'grant_type': 'authorization_code', 'code': q['code'][0], 'code_verifier': verifier,
                                 'device_id': q.get('device_id', [''])[0], 'redirect_uri': REDIRECT})
    print('Права, которые выдал ВК:', res.get('scope'))
    try:
        news.vk('photos.getWallUploadServer', token=res['access_token'], group_id=news.GROUP_ID)
        print('Проверка загрузки фото в группу: ✅ разрешено')
    except Exception as exc:
        sys.exit(f'Проверка загрузки фото не прошла: {exc}. Ключ не сохранён.')
    key = Fernet.generate_key().decode()
    vk_auth.save(key, {'refresh_token': res['refresh_token'], 'device_id': q.get('device_id', [''])[0],
                       'user_id': res.get('user_id'), 'scope': res.get('scope', '')})
    set_secret('VK_TOKEN_KEY', key)
    print('Секрет VK_TOKEN_KEY записан в GitHub.')
    subprocess.run(['git', 'add', 'vk_token.enc'], check=True)
    subprocess.run(['git', '-c', 'user.name=3DSMaks', '-c', 'user.email=3DSMaks@users.noreply.github.com',
                    'commit', '-qm', 'Connect VK profile token (encrypted)'], check=True)
    subprocess.run(['git', 'pull', '-q', '--rebase', 'origin', 'main'], check=True)
    subprocess.run(['git', 'push', '-q', 'origin', 'HEAD:main'], check=True)
    print('Готово: зашифрованный ключ отправлен в репозиторий. Можно закрыть окно.')


if __name__ == '__main__':
    main()
    input('\nEnter — закрыть')
