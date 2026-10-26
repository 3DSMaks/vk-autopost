import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import dzen_post as dz


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


RAW = {'title': '**«Новый Бэтмен» выглядит как лучший фильм DC**', 'lead': 'Вышел трейлер.',
       'sections': [{'heading': 'Что показали.', 'text': 'Абзац один.\n\n\n\nАбзац два.'},
                    {'heading': 'Что смущает', 'text': 'Хронометраж.'}],
       'verdict': 'Ждём.'}


class DzenTests(unittest.TestCase):
    def test_title_gets_period_for_dzen_headline(self):
        art = dz.clean_article(RAW)
        self.assertEqual(art['title'], '«Новый Бэтмен» выглядит как лучший фильм DC.')
        self.assertEqual(dz.clean_article(dict(RAW, title='«Бэтмен вернулся»'))['title'], 'Бэтмен вернулся.')
        self.assertEqual(art['sections'][0]['heading'], 'Что показали')
        self.assertEqual(art['sections'][0]['text'], 'Абзац один.\n\nАбзац два.')

    def test_text_starts_with_title_sentence(self):
        text = dz.as_text(dz.clean_article(RAW))
        first = text.split('\n\n')[0]
        self.assertTrue(first.endswith('.'))
        self.assertTrue(text.endswith('Вердикт: Ждём.'))

    def test_incomplete_article_rejected(self):
        with self.assertRaises(RuntimeError):
            dz.clean_article({'title': 'Т', 'lead': '', 'sections': [], 'verdict': ''})

    def test_fit_drops_middle_sections(self):
        art = dz.clean_article(dict(RAW, sections=[{'heading': f'Блок {i}', 'text': 'х' * 1500} for i in range(5)]))
        art = dz.fit(art)
        self.assertLessEqual(len(dz.as_text(art)), dz.MAX_TEXT)
        self.assertEqual(art['sections'][0]['heading'], 'Блок 0')
        self.assertEqual(art['sections'][-1]['heading'], 'Блок 4')

    def test_rich_page_escapes_and_has_slideshow(self):
        art = dz.clean_article(dict(RAW, lead='A < B & C'))
        page = dz.as_rich(art, 2)
        self.assertTrue(page.startswith('<tg-slideshow><img src="tg://photo?id=p0"/><img src="tg://photo?id=p1"/></tg-slideshow>'))
        self.assertIn('<p>A &lt; B &amp; C</p>', page)

    def test_album_splits_article_between_photos_in_order(self):
        art = dz.clean_article(dict(RAW, sections=[{'heading': f'Блок {i}', 'text': 'х' * 600} for i in range(4)]))
        caps = dz.album_captions(art, 3)
        self.assertEqual(len(caps), 3)
        self.assertTrue(caps[0].startswith('<b>«Новый Бэтмен»'))
        self.assertTrue(all(len(c) <= dz.CAPTION for c in caps))
        joined = '\n'.join(caps)
        self.assertLess(joined.index('Блок 0'), joined.index('Блок 3'))
        self.assertIn('Вердикт', caps[-1])

    def test_album_published_with_caption_per_photo(self):
        art = dz.clean_article(RAW)
        with patch.object(dz.tg, 'api', return_value={}) as api:
            dz.publish('@c', art, [b'a', b'b', b'c'])
        method, fields, files = api.call_args.args
        self.assertEqual(method, 'sendMediaGroup')
        media = json.loads(fields['media'])
        self.assertEqual(len(media), 3)
        self.assertTrue(all(m['caption'] for m in media))
        self.assertEqual(set(files), {'p0', 'p1', 'p2'})

    def test_rich_rejected_falls_back_to_text_message(self):
        art = dz.clean_article(RAW)
        os.environ['DZEN_FORMAT'] = 'rich'
        self.addCleanup(os.environ.pop, 'DZEN_FORMAT')
        calls = []
        with patch.object(dz.tg, 'publish_rich', side_effect=RuntimeError('Telegram sendRichMessage: HTTP 400.')), \
             patch.object(dz.tg, 'api', side_effect=lambda m, f=None, files=None: calls.append((m, f)) or {}), quiet():
            dz.publish('@c', art, [b'jpg'])
        self.assertEqual(calls[0][0], 'sendMessage')
        self.assertTrue(calls[0][1]['text'].startswith('<b>'))

    def test_network_error_not_retried_to_avoid_duplicate(self):
        art = dz.clean_article(RAW)
        os.environ['DZEN_FORMAT'] = 'rich'
        self.addCleanup(os.environ.pop, 'DZEN_FORMAT')
        with patch.object(dz.tg, 'publish_rich', side_effect=RuntimeError('Telegram sendRichMessage: ошибка сети')), \
             patch.object(dz.tg, 'api') as api:
            with self.assertRaises(RuntimeError):
                dz.publish('@c', art, [b'jpg'])
        api.assert_not_called()

    def test_main_publishes_and_remembers(self):
        items = [{'index': 0, 'link': 'https://a/1', 'title': 'Тема', 'summary': ''},
                 {'index': 1, 'link': 'https://a/2', 'title': 'Тема 2', 'summary': ''}]
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / 'seen_dzen.json'
            with patch.object(dz, 'SEEN_FILE', state), patch.dict(os.environ, {'DZEN_CHAT_ID': '@c'}), \
                 patch.object(dz, 'candidates', return_value=items), \
                 patch.object(dz, 'choose', return_value=(items[0], [items[1]], 'угол')), \
                 patch.object(dz, 'gather_photos', return_value=[b'a', b'b', b'c']), \
                 patch.object(dz, 'write', return_value=dz.clean_article(RAW)), \
                 patch.object(dz, 'publish') as pub, quiet():
                dz.main()
            seen = json.loads(state.read_text(encoding='utf-8'))
        pub.assert_called_once()
        self.assertIn('https://a/1', seen)
        self.assertIn('skip:https://a/2', seen)
        self.assertIn('title:Тема', seen)


if __name__ == '__main__':
    unittest.main()
