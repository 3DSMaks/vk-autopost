import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import telegram_post as tg

class TelegramTests(unittest.TestCase):
    def test_single_photo(self):
        with patch.object(tg, 'api', return_value={'message_id': 1}) as api:
            tg.publish('@channel', 'A < B', 'https://example.com/', [b'jpg'])
        self.assertEqual(api.call_args.args[0], 'sendPhoto')
        self.assertIn('A &lt; B', api.call_args.args[1]['caption'])
        self.assertEqual(api.call_args.args[2], {'photo': b'jpg'})

    def test_album(self):
        with patch.object(tg, 'api') as api:
            tg.publish('@channel', 'Text', 'https://example.com', [b'1', b'2'])
        method, fields, files = api.call_args.args
        self.assertEqual(method, 'sendMediaGroup')
        media = json.loads(fields['media'])
        self.assertEqual(media[1]['media'], 'attach://photo1')
        self.assertNotIn('caption', media[1])
        self.assertEqual(files['photo1'], b'2')

    def test_caption_limit(self):
        result = tg.caption('😀' * 1500, 'https://example.com')
        self.assertLessEqual(len(result.split('\n\n')[0].encode('utf-16-le')) // 2 + len('Источник'), 1024)

    def test_missing_photos(self):
        with patch.object(tg, 'api') as api:
            with self.assertRaises(RuntimeError):
                tg.publish('@channel', 'Text', 'link', [])
            api.assert_not_called()

    @patch.dict(os.environ, {'TELEGRAM_CHAT_ID': '@channel'})
    def test_missing_rights(self):
        with patch.object(tg, 'api', side_effect=[{'id':1}, {'type':'channel'}, {'status':'member'}]):
            with self.assertRaisesRegex(RuntimeError, 'администратором'):
                tg.check_channel()

    @patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': 'secret-token'})
    def test_no_token_in_error(self):
        with patch.object(tg.news, 'http', side_effect=urllib.error.URLError('secret-token')):
            with self.assertRaises(RuntimeError) as err:
                tg.api('getMe')
            self.assertNotIn('secret-token', str(err.exception))

    def test_history_only_after_success(self):
        for success in (False, True):
            with tempfile.TemporaryDirectory() as tmp:
                state = Path(tmp) / 'seen.json'
                state.write_text('[]')
                pick = {'link':'https://example.com/news'}
                with patch('sys.argv', ['telegram_post.py']), patch.object(tg, 'SEEN_FILE', state), patch.object(tg, 'check_channel', return_value='@channel'), patch.object(tg.news, 'fresh_news', return_value=[pick]), patch.object(tg.news, 'write_post', return_value=(pick, 'Text')), patch.object(tg, 'pictures', return_value=[b'jpg']), patch.object(tg, 'publish', side_effect=None if success else RuntimeError('failed'), return_value={'message_id':1}):
                    if success:
                        tg.main()
                    else:
                        with self.assertRaises(RuntimeError):
                            tg.main()
                self.assertEqual(json.loads(state.read_text()), [pick['link']] if success else [])

if __name__ == '__main__':
    unittest.main()
