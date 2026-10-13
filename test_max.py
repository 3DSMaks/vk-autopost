import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import max_post as mx
import contextlib, io


def quiet():
    """Глушит печать в тестах, чтобы тестовые данные не путались с настоящими в логе GitHub."""
    return contextlib.redirect_stdout(io.StringIO())


class MaxTests(unittest.TestCase):
    @patch.dict(os.environ, {'MAX_BOT_TOKEN': 'secret-token'})
    def test_upload_returns_photos_payload(self):
        with patch.object(mx, 'api', return_value={'url': 'https://up.max.ru/x'}), \
             patch.object(mx, 'request', return_value=b'{"photos": {"abc": {"token": "T"}}}') as req:
            self.assertEqual(mx.upload_image(b'jpg'), {'photos': {'abc': {'token': 'T'}}})
        url, body, headers, method, _ = req.call_args.args
        self.assertEqual(url, 'https://up.max.ru/x')
        self.assertIn(b'name="data"', body)
        self.assertIn(b'jpg', body)

    def test_publish_retries_not_ready(self):
        not_ready = RuntimeError('not ready'); not_ready.code = 'attachment.not.ready'
        with patch.object(mx, 'upload_image', return_value={'photos': {'a': {'token': 't'}}}), \
             patch.object(mx, 'api', side_effect=[not_ready, {'message': {'url': 'u'}}]) as api, \
             patch.object(mx.time, 'sleep') as sleep, quiet():
            self.assertEqual(mx.publish('-1', 'A < B', 'https://e.com', [b'1']), {'message': {'url': 'u'}})
        method, path, params, body = api.call_args.args
        self.assertEqual((method, path, params), ('POST', '/messages', {'chat_id': '-1'}))
        self.assertEqual(body['format'], 'html')
        self.assertIn('A &lt; B', body['text'])
        self.assertEqual(body['attachments'][0]['type'], 'image')
        sleep.assert_called_once()

    def test_other_error_not_retried(self):
        boom = RuntimeError('boom'); boom.code = 'access.denied'
        with patch.object(mx, 'upload_image', return_value={'token': 't'}), \
             patch.object(mx, 'api', side_effect=boom) as api, patch.object(mx.time, 'sleep'):
            with self.assertRaises(RuntimeError):
                mx.publish('-1', 'T', 'l', [b'1'])
        self.assertEqual(api.call_count, 1)

    def test_missing_photos(self):
        with patch.object(mx, 'upload_image') as up:
            with self.assertRaises(RuntimeError):
                mx.publish('-1', 'T', 'l', [])
            up.assert_not_called()

    @patch.dict(os.environ, {'MAX_CHAT_ID': '-123'})
    def test_missing_rights(self):
        replies = [{'first_name': 'bot'}, {'type': 'channel', 'title': 'C'}, {'is_admin': True, 'permissions': ['read_all_messages']}]
        with patch.object(mx, 'api', side_effect=replies):
            with self.assertRaisesRegex(RuntimeError, 'администратором'):
                mx.check_channel()

    @patch.dict(os.environ, {'MAX_CHAT_ID': '@channel'})
    def test_chat_id_must_be_numeric(self):
        with self.assertRaisesRegex(RuntimeError, 'числовой'):
            mx.chat_id()

    @patch.dict(os.environ, {'MAX_BOT_TOKEN': 'secret-token'})
    def test_no_token_in_error(self):
        with patch.object(mx, 'request', side_effect=urllib.error.URLError('secret-token')):
            with self.assertRaises(RuntimeError) as err:
                mx.api('GET', '/me')
        self.assertNotIn('secret-token', str(err.exception))

    def test_ca_bundle_loads(self):
        self.assertTrue(mx.CA_FILE.exists())
        mx.ssl_context()

    def test_text_limit(self):
        self.assertLess(len(mx.message_text('я' * 6000, 'https://e.com')), 4000)

    def test_history_only_after_success(self):
        for success in (False, True):
            with tempfile.TemporaryDirectory() as tmp:
                state = Path(tmp) / 'seen.json'
                state.write_text('[]')
                pick = {'link': 'https://example.com/news'}
                with patch('sys.argv', ['max_post.py']), patch.object(mx, 'SEEN_FILE', state), \
                     patch.object(mx, 'check_channel', return_value='-1'), patch.object(mx.tg, 'drop_duplicates', side_effect=lambda c, r: (c, [])), \
                     patch.object(mx.tg, 'fresh_for', return_value=[pick]), \
                     patch.object(mx.news, 'write_post', return_value=(pick, 'Text')), \
                     patch.object(mx.tg, 'pictures', return_value=[b'jpg']), \
                     patch.object(mx, 'publish', side_effect=None if success else RuntimeError('failed'),
                                  return_value={'message': {'url': 'u'}}):
                    if success:
                        with quiet():
                            mx.main()
                    else:
                        with self.assertRaises(RuntimeError), quiet():
                            mx.main()
                self.assertEqual(json.loads(state.read_text()), [pick['link']] if success else [])


    def test_find_chats_from_updates(self):
        replies = [{'updates': [{'update_type': 'bot_added', 'chat_id': -77}, {'update_type': 'message_created',
                    'message': {'recipient': {'chat_id': -77}}}, {'update_type': 'bot_added', 'chat_id': -88}]},
                   {'type': 'channel', 'title': 'СтопКадр'}, {'type': 'chat', 'title': 'Другой'}]
        with patch.object(mx, 'api', side_effect=replies), quiet():
            self.assertEqual(mx.find_chats(), [-77, -88])

    def test_hashtags_removed(self):
        text = 'Заголовок\n\nТекст поста #не_трогать_в_середине? нет\n\n#Кино #Сериалы\n\nИсточник: https://e.com'
        out = mx.message_text(text, 'https://e.com')
        self.assertNotIn('#Кино', out)
        self.assertNotIn('#Сериалы', out)
        self.assertIn('Текст поста', out)
        self.assertEqual(mx.without_hashtags('Абзац про игру #Игры #PS5'), 'Абзац про игру')

    def test_closing_question_removed(self):
        text = 'Заголовок\n\nКонами анонсировала игру. Выход в 2027 году.\n\nЖдёте продолжения? 🎮\n\n#Игры'
        out = mx.message_text(text, 'https://e.com')
        self.assertNotIn('Ждёте', out)
        self.assertIn('Выход в 2027 году.', out)
        self.assertEqual(mx.without_closing_question('Факт один. А вы как думаете?'), 'Факт один.')
        self.assertEqual(mx.without_closing_question('Почему игру перенесли? Студия объяснила задержку.'),
                         'Почему игру перенесли? Студия объяснила задержку.')

    def test_prompt_has_no_question_rule(self):
        games = mx.tg.TOPICS['games'][1]
        self.assertIn('В конце — вопрос для обсуждения.', games)  # именно эту фразу заменяет max_post
        self.assertIn('комментарии в канале закрыты', games.replace('В конце — вопрос для обсуждения.', mx.NO_QUESTION))

if __name__ == '__main__':
    unittest.main()
