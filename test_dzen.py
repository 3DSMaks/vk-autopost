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


RAW = {'title': '**«Новый Бэтмен» выглядит как лучший фильм DC.**', 'lead': 'Вышел трейлер.',
       'sections': [{'heading': 'Что показали.', 'text': 'Абзац один.\n\n\n\nАбзац два.'},
                    {'heading': 'Что смущает', 'text': 'Хронометраж.'}],
       'verdict': 'Ждём.'}


def sections(n, size=10):
    return dict(RAW, sections=[{'heading': f'Блок {i}', 'text': 'х' * size} for i in range(n)])


class DzenTests(unittest.TestCase):
    def test_clean_article(self):
        art = dz.clean_article(RAW)
        self.assertEqual(art['title'], '«Новый Бэтмен» выглядит как лучший фильм DC')
        self.assertEqual(dz.clean_article(dict(RAW, title='«Бэтмен вернулся»'))['title'], 'Бэтмен вернулся')
        self.assertEqual(art['sections'][0]['heading'], 'Что показали')
        self.assertEqual(art['sections'][0]['text'], 'Абзац один.\n\nАбзац два.')

    def test_incomplete_article_rejected(self):
        with self.assertRaises(RuntimeError):
            dz.clean_article({'title': 'Т', 'lead': '', 'sections': [], 'verdict': ''})

    def test_fit_drops_middle_sections(self):
        art = dz.fit(dz.clean_article(sections(5, 1500)))
        self.assertLessEqual(len(dz.as_text(art)), dz.MAX_TEXT)
        self.assertEqual(art['sections'][0]['heading'], 'Блок 0')
        self.assertEqual(art['sections'][-1]['heading'], 'Блок 4')

    def test_draft_marks_where_each_photo_goes_in_order(self):
        text = dz.draft_text(dz.clean_article(sections(4)), 3)
        self.assertTrue(text.startswith('<b>Заголовок:</b> «Новый Бэтмен»'))
        marks = [text.index(m) for m in ('Фото 1 — обложка', 'Фото 2', 'Фото 3')]
        self.assertEqual(marks, sorted(marks))
        self.assertLess(text.index('Блок 0'), text.index('Фото 2'))   # фото стоят между разделами
        self.assertLess(text.index('Фото 3'), text.index('Вердикт'))
        self.assertEqual(text.count('📷'), 3)

    def test_single_photo_is_only_cover(self):
        text = dz.draft_text(dz.clean_article(RAW), 1)
        self.assertEqual(text.count('📷'), 1)
        self.assertIn('Вердикт:</b> Ждём.', text)

    def test_draft_escapes_html(self):
        text = dz.draft_text(dz.clean_article(dict(RAW, lead='A < B & C')), 1)
        self.assertIn('A &lt; B &amp; C', text)

    def test_split_long_text_by_paragraphs(self):
        chunks = dz.split_message('\n\n'.join(['а' * 1500] * 5), 4000)
        self.assertEqual(len(chunks), 3)
        self.assertTrue(all(len(c) <= 4000 for c in chunks))

    def test_send_draft_album_then_text(self):
        art = dz.clean_article(sections(3))
        calls = []
        with patch.object(dz.tg, 'api', side_effect=lambda m, f=None, files=None: calls.append((m, f, files)) or {}):
            dz.send_draft('42', art, [b'a', b'b', b'c'], {'link': 'https://src'})
        methods = [c[0] for c in calls]
        self.assertEqual(methods[:2], ['sendMessage', 'sendMediaGroup'])
        media = json.loads(calls[1][1]['media'])
        self.assertEqual([m['caption'] for m in media], ['Фото 1 — обложка', 'Фото 2', 'Фото 3'])
        self.assertIn('Фото 2', calls[2][1]['text'])
        self.assertEqual(calls[-1][1]['text'], 'Источник: https://src')

    def test_more_photos_from_bing_and_steam_when_sources_have_few(self):
        pick = {'link': 'https://a/1', 'title': 'T', 'query': 'q', 'game': 'G'}
        seen_items = []
        def pictures(item):
            seen_items.append(item['link'])
            return [b'x'] if item['link'] == 'https://a/1' else []
        with patch.object(dz.tg, 'pictures', side_effect=pictures), \
             patch.object(dz, 'bing_links', return_value=[{'link': 'https://b/2', 'title': 't', 'image': ''}]), \
             patch.object(dz, 'steam_shots', return_value=[b'y', b'z']), \
             patch.object(dz.tg, 'looks_like', side_effect=lambda d: {b'x': 0, b'y': 2**200 - 1, b'z': 2**100 - 1}[d]), quiet():
            photos = dz.gather_photos([pick])
        self.assertEqual(photos, [b'x', b'y', b'z'])
        self.assertEqual(seen_items, ['https://a/1', 'https://b/2'])

    def test_order_photos_keeps_every_photo_once(self):
        art = dz.clean_article(RAW)
        with patch.object(dz.tg, 'thumb_b64', return_value='x'), \
             patch.object(dz.tg, 'gemini_json', return_value={'order': [2, 2, 0]}):
            self.assertEqual(dz.order_photos(art, [b'a', b'b', b'c']), [b'c', b'a', b'b'])

    def test_main_sends_draft_and_remembers(self):
        items = [{'index': 0, 'link': 'https://a/1', 'title': 'Тема', 'summary': ''},
                 {'index': 1, 'link': 'https://a/2', 'title': 'Тема 2', 'summary': ''}]
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / 'seen_dzen.json'
            with patch.object(dz, 'SEEN_FILE', state), patch.dict(os.environ, {'DZEN_DRAFT_CHAT_ID': '42'}), \
                 patch.object(dz, 'candidates', return_value=items), \
                 patch.object(dz, 'choose', return_value=(items[0], [items[1]], 'угол')), \
                 patch.object(dz, 'gather_photos', return_value=[b'a', b'b']), \
                 patch.object(dz, 'write', return_value=dz.clean_article(sections(3, 900))), \
                 patch.object(dz, 'order_photos', side_effect=lambda a, p: p), \
                 patch.object(dz, 'send_draft') as send, quiet():
                dz.main()
            seen = json.loads(state.read_text(encoding='utf-8'))
        send.assert_called_once()
        self.assertEqual(send.call_args.args[0], '42')
        self.assertIn('https://a/1', seen)
        self.assertIn('skip:https://a/2', seen)
        self.assertIn('title:Тема', seen)


if __name__ == '__main__':
    unittest.main()
