import contextlib
import io
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
                with patch('sys.argv', ['telegram_post.py']), patch.object(tg, 'SEEN_FILE', state), patch.object(tg, 'check_channel', return_value='@channel'), patch.object(tg.news, 'fresh_news', return_value=[pick]), patch.object(tg.news, 'write_post', return_value=(pick, 'Text')), patch.object(tg, 'pictures', return_value=[b'jpg']), patch.object(tg, 'trailer_url', return_value=None), patch.object(tg, 'tg_format', return_value=None), patch.object(tg, 'publish', side_effect=None if success else RuntimeError('failed'), return_value={'message_id':1}):
                    if success:
                        tg.main()
                    else:
                        with self.assertRaises(RuntimeError):
                            tg.main()
                self.assertEqual(json.loads(state.read_text()), [pick['link']] if success else [])

    def test_topics_alternate(self):
        self.assertEqual(tg.topic_order(14), ['games', 'cinema'])
        self.assertEqual(tg.topic_order(15), ['cinema', 'games'])
        self.assertIn('видеоигры', tg.TOPICS['games'][1])
        self.assertNotIn('не игры', tg.TOPICS['games'][1])
        self.assertIn('AAA', tg.TOPICS['games'][1])
        self.assertNotIn('Предпочитай громкие премьеры', tg.TOPICS['games'][1])

    def test_per_feed_cap_and_order(self):
        def fake(seen):
            feed = tg.news.FEEDS[0]
            hour = 10 if 'a' in feed else 11
            return [{'link': f'{feed}/{i}', 'date': f'2026-10-09T{hour}:{i:02d}:00+00:00'} for i in range(9)]
        with patch.dict(tg.TOPICS, {'games': (['https://a', 'https://b'], 'P')}), patch.object(tg.news, 'fresh_news', side_effect=fake):
            items = tg.fresh_for('games', set(), per_feed=3)
        self.assertEqual(len(items), 6)
        self.assertTrue(items[0]['link'].startswith('https://b'))
        self.assertEqual([n['index'] for n in items], list(range(6)))

    def test_same_picture_in_two_sizes_skipped(self):
        import io as _io
        from PIL import Image as _Image, ImageDraw
        def jpeg(size, color):
            im = _Image.new('RGB', size, 'white')
            ImageDraw.Draw(im).rectangle([0, 0, size[0] // 2, size[1]], fill=color)
            buf = _io.BytesIO(); im.save(buf, 'JPEG'); return buf.getvalue()
        big, small, other = jpeg((800, 450), 'black'), jpeg((400, 225), 'black'), jpeg((800, 450), 'white')
        with patch.object(tg.news, 'page_images', return_value=[]), patch.object(tg.news, 'image_urls', return_value=['1', '2', '3']), patch.object(tg.news, 'download_jpeg', side_effect=[big, small, other]), patch.object(tg, 'vet_images', side_effect=lambda t, imgs: imgs):
            self.assertEqual(tg.pictures({'link': 'x'}), [big, other])

    def test_no_photo_picks_another(self):
        a, b = {'link': 'A', 'index': 0}, {'link': 'B', 'index': 1}
        calls = []
        def write(pool):
            calls.append([c['link'] for c in pool])
            return pool[0], 'Text ' + pool[0]['link']
        with patch.object(tg.news, 'write_post', side_effect=write),              patch.object(tg, 'pictures', side_effect=lambda p: [] if p['link'] == 'A' else [b'jpg']),              contextlib.redirect_stdout(io.StringIO()):
            pick, text, photos, skipped = tg.pick_with_photos([a, b])
        self.assertEqual((pick['link'], photos, skipped), ('B', [b'jpg'], ['A']))
        self.assertEqual(calls, [['A', 'B'], ['B']])

    def test_skipped_saved_and_not_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / 'seen.json'
            tg.save_history(state, ['X'], 'B', ['A'])
            seen = json.loads(state.read_text())
        self.assertEqual(seen, ['X', 'skip:A', 'B'])
        self.assertEqual(tg.history_links(seen), {'X', 'A', 'B'})
        self.assertEqual(tg.published_count(seen), 2)

    def test_all_without_photos_saves_skips_and_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / 'seen.json'
            state.write_text('[]')
            cands = [{'link': 'A', 'index': 0}]
            with patch('sys.argv', ['telegram_post.py']), patch.object(tg, 'SEEN_FILE', state),                  patch.object(tg, 'check_channel', return_value='@c'), patch.object(tg, 'fresh_for', return_value=cands),                  patch.object(tg.news, 'write_post', return_value=(cands[0], 'T')), patch.object(tg, 'pictures', return_value=[]),                  patch.object(tg, 'publish') as pub, contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, 'фото'):
                    tg.main()
            pub.assert_not_called()
            self.assertEqual(json.loads(state.read_text()), ['skip:A'])

    def test_trailer_prefers_embedded_player(self):
        page = b'<a href="https://youtu.be/AAAAAAAAAAA">x</a><iframe src="https://www.youtube.com/embed/BBBBBBBBBBB?rel=0"></iframe>'
        with patch.object(tg.news, 'http', return_value=page):
            self.assertEqual(tg.trailer_url('l'), 'https://www.youtube.com/watch?v=BBBBBBBBBBB')
        with patch.object(tg.news, 'http', return_value=b'<p>no video</p>'):
            self.assertIsNone(tg.trailer_url('l'))

    def test_publish_video_message(self):
        with patch.object(tg, 'api', return_value={'message_id': 5}) as api:
            tg.publish_video('@c', 'A < B', 'https://e.com', 'https://www.youtube.com/watch?v=BBBBBBBBBBB')
        method, fields = api.call_args.args
        self.assertEqual(method, 'sendMessage')
        self.assertIn('Смотреть трейлер', fields['text'])
        self.assertIn('A &lt; B', fields['text'])
        preview = json.loads(fields['link_preview_options'])
        self.assertTrue(preview['prefer_large_media'] and preview['show_above_text'])

    def run_main(self, seen, trailer):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / 'seen.json'
            state.write_text(json.dumps(seen))
            pick = {'link': 'https://e.com/n', 'index': 0}
            with patch('sys.argv', ['telegram_post.py']), patch.object(tg, 'SEEN_FILE', state),                  patch.object(tg, 'check_channel', return_value='@c'), patch.object(tg, 'fresh_for', return_value=[pick]),                  patch.object(tg.news, 'write_post', return_value=(pick, 'T')), patch.object(tg, 'pictures', return_value=[b'jpg']),                  patch.object(tg, 'trailer_url', return_value=trailer), patch.object(tg, 'tg_format', return_value=None),                  patch.object(tg, 'publish', return_value={'message_id': 1}) as photo,                  patch.object(tg, 'publish_video', return_value={'message_id': 2}) as video,                  contextlib.redirect_stdout(io.StringIO()):
                tg.main()
            return photo.called, video.called, json.loads(state.read_text())

    def test_main_posts_trailer_and_marks_it(self):
        photo, video, seen = self.run_main([], 'https://www.youtube.com/watch?v=BBBBBBBBBBB')
        self.assertEqual((photo, video), (False, True))
        self.assertIn(f'video:{tg.moscow_today()}:https://e.com/n', seen)
        self.assertEqual(tg.published_count(seen), 1)

    def test_main_respects_daily_video_limit(self):
        today = tg.moscow_today()
        full = [f'video:{today}:{i}' for i in range(tg.VIDEO_PER_DAY)]
        photo, video, _ = self.run_main(full, 'https://www.youtube.com/watch?v=BBBBBBBBBBB')
        self.assertEqual((photo, video), (True, False))

    def test_main_without_trailer_posts_photos(self):
        photo, video, _ = self.run_main([], None)
        self.assertEqual((photo, video), (True, False))

    def test_sanitize_keeps_only_safe_tags(self):
        self.assertEqual(tg.sanitize_tg_html('<b>Заг</b>\n<script>x</script> A &amp; B <blockquote>«Ц»</blockquote>'),
                         '<b>Заг</b>\n&lt;script&gt;x&lt;/script&gt; A &amp; B <blockquote>«Ц»</blockquote>')
        self.assertIsNone(tg.sanitize_tg_html('<b>не закрыт'))
        self.assertIsNone(tg.sanitize_tg_html('<b><i>криво</b></i>'))

    def test_tg_format_uses_gemini_html_or_falls_back(self):
        pick = {'link': 'https://e.com', 'summary': 's'}
        with patch.object(tg, 'article_text', return_value='текст'), \
             patch.object(tg, 'gemini_json', return_value={'post': '<b>Заголовок</b>\n\n• пункт\n\n#Игры'}):
            self.assertEqual(tg.tg_format(pick, 'draft'), '<b>Заголовок</b>\n\n• пункт\n\n#Игры')
        with patch.object(tg, 'article_text', return_value=''), \
             patch.object(tg, 'gemini_json', return_value={'post': '<b>' + 'я' * 2000 + '</b>'}), quiet():
            self.assertIsNone(tg.tg_format(pick, 'draft'))
        with patch.object(tg, 'article_text', return_value=''), \
             patch.object(tg, 'gemini_json', side_effect=RuntimeError('down')), quiet():
            self.assertIsNone(tg.tg_format(pick, 'draft'))

    def test_caption_with_formatted_html(self):
        self.assertEqual(tg.caption('raw', 'https://e.com', '<b>X</b>'), '<b>X</b>\n\n<a href="https://e.com">Источник</a>')

    def test_vet_images_keeps_relevant_in_gemini_order(self):
        imgs = [self.jpeg('red'), self.jpeg('green'), self.jpeg('blue')]
        with patch.object(tg, 'gemini_json', return_value={'keep': [2, 0, 2, 9]}), quiet():
            self.assertEqual(tg.vet_images('t', imgs), [imgs[2], imgs[0]])
        with patch.object(tg, 'gemini_json', return_value={'keep': []}), quiet():
            self.assertEqual(tg.vet_images('t', imgs), [imgs[0]])
        with patch.object(tg, 'gemini_json', side_effect=RuntimeError('down')), quiet():
            self.assertEqual(tg.vet_images('t', imgs), imgs)

    def jpeg(self, color):
        from PIL import Image as _Image
        buf = io.BytesIO(); _Image.new('RGB', (300, 200), color).save(buf, 'JPEG'); return buf.getvalue()


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


if __name__ == '__main__':
    unittest.main()
