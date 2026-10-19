import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

from cryptography.fernet import Fernet

import vk_auth


class VKAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.file = Path(self.tmp.name) / 'vk_token.enc'
        self.key = Fernet.generate_key().decode()
        self.patcher = patch.object(vk_auth, 'TOKEN_FILE', self.file)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def test_refresh_rotates_and_saves_before_returning(self):
        vk_auth.save(self.key, {'refresh_token': 'R1', 'device_id': 'D'})
        reply = json.dumps({'access_token': 'A2', 'refresh_token': 'R2', 'scope': 'wall photos groups'}).encode()
        with patch.object(vk_auth.news, 'http', return_value=reply) as http:
            access, scope = vk_auth.refresh(self.key)
        self.assertEqual((access, scope), ('A2', 'wall photos groups'))
        body = http.call_args.args[1].decode()
        self.assertIn('grant_type=refresh_token', body)
        self.assertIn('refresh_token=R1', body)
        self.assertIn('client_id=54813069', body)
        self.assertEqual(vk_auth.load(self.key)['refresh_token'], 'R2')
        self.assertNotIn(b'R2', self.file.read_bytes())  # на диске только шифр

    def test_error_does_not_leak_token(self):
        vk_auth.save(self.key, {'refresh_token': 'SECRET-R', 'device_id': 'D'})
        err = urllib.error.HTTPError('u', 400, 'bad', {}, io.BytesIO(b'{"error":"invalid_grant"}'))
        with patch.object(vk_auth.news, 'http', side_effect=err):
            with self.assertRaises(RuntimeError) as ctx:
                vk_auth.refresh(self.key)
        self.assertIn('invalid_grant', str(ctx.exception))
        self.assertNotIn('SECRET-R', str(ctx.exception))
        self.assertEqual(vk_auth.load(self.key)['refresh_token'], 'SECRET-R')  # при ошибке старый ключ не трогаем

    def test_wrong_key(self):
        vk_auth.save(self.key, {'refresh_token': 'R', 'device_id': 'D'})
        with self.assertRaisesRegex(RuntimeError, 'VK_TOKEN_KEY'):
            vk_auth.load(Fernet.generate_key().decode())

    def test_main_exports_masked_token(self):
        vk_auth.save(self.key, {'refresh_token': 'R1', 'device_id': 'D'})
        env_file = Path(self.tmp.name) / 'env'
        reply = json.dumps({'access_token': 'A2', 'refresh_token': 'R2'}).encode()
        out = io.StringIO()
        with patch.dict(os.environ, {'VK_TOKEN_KEY': self.key, 'GITHUB_ENV': str(env_file)}), \
             patch.object(vk_auth.news, 'http', return_value=reply), contextlib.redirect_stdout(out):
            vk_auth.main()
        self.assertIn('::add-mask::A2', out.getvalue())
        self.assertEqual(env_file.read_text(encoding='utf-8'), 'VK_PHOTO_TOKEN=A2\n')

    def test_main_without_setup_is_noop(self):
        with patch.dict(os.environ, {'VK_TOKEN_KEY': ''}), contextlib.redirect_stdout(io.StringIO()):
            vk_auth.main()


if __name__ == '__main__':
    unittest.main()
