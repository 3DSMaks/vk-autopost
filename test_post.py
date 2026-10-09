import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image
import post


class PhotoTests(unittest.TestCase):
    def test_metadata_order_lazy_srcset(self):
        page = b'''<meta content="/cover?id=1&amp;w=900" property="og:image">
        <article><img src="placeholder.svg" data-src="/real.webp">
        <source srcset="/small.jpg 400w, /large.jpg 1200w"></article>'''
        with patch.object(post, 'http', return_value=page):
            urls = post.page_images('https://example.com/news')
        self.assertEqual(urls, ['https://example.com/cover?id=1&w=900',
                               'https://example.com/real.webp',
                               'https://example.com/large.jpg', 'https://example.com/small.jpg'])

    def test_conversion(self):
        for fmt in ('PNG', 'WEBP', 'JPEG'):
            raw = io.BytesIO()
            Image.new('RGB', (1800, 900), 'red').save(raw, fmt)
            with patch('urllib.request.urlopen', return_value=io.BytesIO(raw.getvalue())):
                data = post.download_jpeg('https://example.com/image', 'https://example.com/news')
            with Image.open(io.BytesIO(data)) as im:
                self.assertEqual(im.format, 'JPEG')
                self.assertEqual(im.size, (1600, 800))

    def test_html_is_not_photo(self):
        with patch('urllib.request.urlopen', return_value=io.BytesIO(b'<html>Denied</html>')):
            with self.assertRaises(Exception):
                post.download_jpeg('https://example.com/image', '')

    @patch.dict(os.environ, {'VK_PHOTO_TOKEN': 'photo-secret', 'VK_TOKEN': 'wall-secret'})
    def test_upload_token_and_attachment(self):
        calls = []
        def vk(method, **kw):
            calls.append((method, kw))
            if method == 'photos.getWallUploadServer':
                return {'upload_url': 'https://example.com/upload'}
            return [{'owner_id': -123, 'id': 456, 'access_key': 'key'}]
        with patch.object(post, 'vk', side_effect=vk), patch.object(post, 'download_jpeg', return_value=b'jpeg'), patch.object(post, 'http', return_value=b'{"server":1,"photo":"[{}]","hash":"ok"}'):
            result = post.upload_photos(['https://example.com/photo'])
        self.assertEqual(result, ['photo-123_456_key'])
        self.assertTrue(all(kw['token'] == 'photo-secret' for _, kw in calls))

    def test_empty_upload_rejected(self):
        with patch.object(post, 'download_jpeg', return_value=b'jpeg'), patch.object(post, 'http', return_value=b'{"server":1,"photo":"[]","hash":"ok"}'), patch.object(post, 'vk') as vk:
            self.assertEqual(post.upload_photos(['https://example.com/photo'], server='https://example.com/upload'), [])
            vk.assert_not_called()

    @patch.dict(os.environ, {"VK_TOKEN": "test-token"})
    def test_failed_candidate_uses_next(self):
        with patch.object(post, 'download_jpeg', side_effect=[ValueError(), b'jpeg']), patch.object(post, 'http', return_value=b'{"server":1,"photo":"[{}]","hash":"ok"}'), patch.object(post, 'vk', return_value=[{'owner_id': -1, 'id': 2}]):
            self.assertEqual(post.upload_photos(['bad', 'good'], server='server'), ['photo-1_2'])

    def test_main_no_photo_no_post_no_seen(self):
        self.run_main_case([])

    def test_main_attaches_photo_and_marks_seen(self):
        self.run_main_case(['photo-1_2'])

    def run_main_case(self, photos):
        with tempfile.TemporaryDirectory() as temp:
            seen = Path(temp) / 'seen.json'
            seen.write_text('[]')
            pick = {'link': 'https://example.com/news', 'title': 'News', 'image': 'https://example.com/rss.jpg'}
            with patch.object(post, 'SEEN_FILE', str(seen)), patch('sys.argv', ['post.py']), patch.object(post, 'photo_server', return_value='server'), patch.object(post, 'fresh_news', return_value=[pick]), patch.object(post, 'write_post', return_value=(pick, 'Text')), patch.object(post, 'page_images', return_value=['https://example.com/page.jpg']), patch.object(post, 'upload_photos', return_value=photos) as upload, patch.object(post, 'vk', return_value={'post_id': 42}) as vk:
                if photos:
                    post.main()
                    self.assertEqual(vk.call_args.kwargs['attachments'], 'photo-1_2')
                    self.assertEqual(json.loads(seen.read_text()), [pick['link']])
                else:
                    with self.assertRaisesRegex(RuntimeError, 'НЕ опубликован'):
                        post.main()
                    vk.assert_not_called()
                    self.assertEqual(seen.read_text(), '[]')
                self.assertEqual(upload.call_args.args[0], ['https://example.com/page.jpg', pick['image']])

    def test_group_token_error_is_actionable(self):
        with patch.dict(os.environ, {'VK_TOKEN': 'secret'}), patch.object(post, 'http', return_value=b'{"error":{"error_code":27,"error_msg":"Group authorization failed"}}'):
            with self.assertRaisesRegex(post.VKError, 'VK_PHOTO_TOKEN'):
                post.photo_server()


if __name__ == '__main__':
    unittest.main()
