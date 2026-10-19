import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import vk_post as vp


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


class VKPostTests(unittest.TestCase):
    def run_main(self, photos, uploaded):
        calls = []

        def fake_vk(method, token=None, **params):
            calls.append((method, params))
            if method == 'photos.saveWallPhoto':
                return [{'owner_id': -237783609, 'id': len(calls)}]
            return {'post_id': 77}
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / 'seen.json'
            state.write_text('[]', encoding='utf-8')
            pick = {'link': 'https://e.com/n', 'index': 0, 'title': 'Новость'}
            with patch.object(vp, 'SEEN_FILE', state), patch.object(vp.news, 'photo_server', return_value='https://up'), \
                 patch.object(vp.tg, 'fresh_for', return_value=[pick]), patch.object(vp.tg, 'drop_duplicates', side_effect=lambda c, r: (c, [])), \
                 patch.object(vp.tg, 'pick_with_photos', return_value=(pick, 'Текст\n\nИсточник: x', photos, [])), \
                 patch.object(vp.tg, 'tg_format', return_value='<b>Заголовок</b>\n\n<blockquote>— пункт</blockquote>\n\nФакт.'), \
                 patch.object(vp.news, 'http', return_value=json.dumps(uploaded).encode()), \
                 patch.object(vp.news, 'vk', side_effect=fake_vk), patch.object(vp.news, 'photo_token', return_value='T'), quiet():
                try:
                    vp.main()
                except RuntimeError as exc:
                    return calls, json.loads(state.read_text(encoding='utf-8')), exc
            return calls, json.loads(state.read_text(encoding='utf-8')), None

    def test_posts_carousel_with_plain_text(self):
        calls, seen, err = self.run_main([b'a', b'b'], {'server': 1, 'photo': '[{"x":1}]', 'hash': 'h'})
        self.assertIsNone(err)
        method, params = calls[-1]
        self.assertEqual(method, 'wall.post')
        self.assertEqual(params['primary_attachments_mode'], 'carousel')
        self.assertEqual(params['attachments'].count('photo-237783609_'), 2)
        self.assertNotIn('<', params['message'])
        self.assertIn('Заголовок', params['message'])
        self.assertNotIn('Источник', params['message'])
        self.assertIn('https://e.com/n', seen)
        self.assertIn('title:Новость', seen)

    def test_no_post_when_vk_rejects_photos(self):
        calls, seen, err = self.run_main([b'a'], {'error': 'bad'})
        self.assertIsNotNone(err)
        self.assertNotIn('wall.post', [c[0] for c in calls])
        self.assertEqual(seen, [])

    def test_plain(self):
        self.assertEqual(vp.plain('<b>A &amp; B</b>\n\n<blockquote>— раз</blockquote>'), 'A & B\n\n— раз')


if __name__ == '__main__':
    unittest.main()
