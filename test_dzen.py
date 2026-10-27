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
       'sections': [{'emoji': '🎬', 'heading': 'Что показали.', 'text': 'Абзац один.\n\n\n\nАбзац два.'},
                    {'emoji': '', 'heading': 'Что смущает', 'text': 'Хронометраж.'}],
       'verdict': 'Ждём.'}


def sections(n, size=10):
    return dict(RAW, sections=[{'emoji': '🎮', 'heading': f'Блок {i}', 'text': 'слово ' * size} for i in range(n)])


class DzenTests(unittest.TestCase):
    def test_clean_article_title_is_first_sentence(self):
        art = dz.clean_article(RAW)
        self.assertEqual(art['title'], '«Новый Бэтмен» выглядит как лучший фильм DC.')
        self.assertEqual(dz.clean_article(dict(RAW, title='«Бэтмен вернулся»'))['title'], 'Бэтмен вернулся.')
        self.assertEqual(art['sections'][0]['heading'], 'Что показали')
        self.assertEqual(art['sections'][1]['emoji'], '▪️')
        self.assertEqual(dz.clean_article(dict(RAW, verdict='🎯 Вердикт: ждём.'))['verdict'], 'ждём.')

    def test_incomplete_article_rejected(self):
        with self.assertRaises(RuntimeError):
            dz.clean_article({'title': 'Т', 'lead': '', 'sections': [], 'verdict': ''})

    def test_caption_structure(self):
        cap = dz.caption(dz.clean_article(RAW))
        self.assertTrue(cap.startswith('<b>«Новый Бэтмен» выглядит как лучший фильм DC.</b>\n\nВышел трейлер.'))
        self.assertIn('🎬 <b>Что показали</b>\n', cap)
        self.assertTrue(cap.endswith('🎯 <b>Вердикт:</b> Ждём.'))
        self.assertNotIn('<b>', dz.caption(dz.clean_article(RAW), bold=False))

    def test_caption_escapes_html(self):
        cap = dz.caption(dz.clean_article(dict(RAW, lead='A < B & C')))
        self.assertIn('A &lt; B &amp; C', cap)
        self.assertEqual(dz.visible('<b>A &lt; B</b>'), 5)

    def test_fit_keeps_caption_within_limit(self):
        art = dz.fit(dz.clean_article(sections(4, 60)))
        self.assertLessEqual(dz.visible(dz.caption(art)), dz.CAPTION)
        self.assertEqual(art['sections'][0]['heading'], 'Блок 0')
        long = dz.fit(dz.clean_article(dict(sections(1, 400), lead='длинно ' * 200)))
        self.assertLessEqual(dz.visible(dz.caption(long)), dz.CAPTION)

    def test_one_photo_uses_send_photo(self):
        with patch.object(dz.tg, 'api', return_value={}) as api:
            dz.publish('@c', dz.clean_article(RAW), [b'a'])
        method, fields, files = api.call_args.args
        self.assertEqual(method, 'sendPhoto')
        self.assertEqual(fields['parse_mode'], 'HTML')
        self.assertEqual(files, {'photo': b'a'})

    def test_two_photos_album_with_text_on_first(self):
        with patch.object(dz.tg, 'api', return_value={}) as api:
            dz.publish('@c', dz.clean_article(RAW), [b'a', b'b'])
        method, fields, files = api.call_args.args
        self.assertEqual(method, 'sendMediaGroup')
        media = json.loads(fields['media'])
        self.assertIn('Вердикт', media[0]['caption'])
        self.assertNotIn('caption', media[1])

    def test_photos_stop_searching_once_found(self):
        pick = {'link': 'https://a/1', 'title': 'T', 'query': 'q', 'game': 'G'}
        with patch.object(dz.tg, 'pictures', return_value=[b'x', b'y', b'z']), \
             patch.object(dz.tg, 'looks_like', side_effect=lambda d: {b'x': 0, b'y': 2**200 - 1, b'z': 2**100 - 1}[d]), \
             patch.object(dz, 'bing_links') as bing, patch.object(dz, 'steam_shots') as steam, quiet():
            self.assertEqual(dz.gather_photos([pick]), [b'x', b'y'])
        bing.assert_not_called()
        steam.assert_not_called()

    def test_photos_fall_back_to_bing_then_steam(self):
        pick = {'link': 'https://a/1', 'title': 'T', 'query': 'q', 'game': 'G'}
        with patch.object(dz.tg, 'pictures', return_value=[]), \
             patch.object(dz, 'bing_links', return_value=[{'link': 'https://b/2', 'title': 't', 'image': ''}]), \
             patch.object(dz, 'steam_shots', return_value=[b's']), \
             patch.object(dz.tg, 'looks_like', return_value=0), quiet():
            self.assertEqual(dz.gather_photos([pick]), [b's'])

    def test_main_publishes_and_remembers(self):
        items = [{'index': 0, 'link': 'https://a/1', 'title': 'Тема', 'summary': ''},
                 {'index': 1, 'link': 'https://a/2', 'title': 'Тема 2', 'summary': ''}]
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / 'seen_dzen.json'
            with patch.object(dz, 'SEEN_FILE', state), patch.dict(os.environ, {'DZEN_CHAT_ID': '@c'}), \
                 patch.object(dz, 'candidates', return_value=items), \
                 patch.object(dz, 'choose', return_value=(items[0], [items[1]], 'угол')), \
                 patch.object(dz, 'gather_photos', return_value=[b'a']), \
                 patch.object(dz, 'write', return_value=dz.clean_article(RAW)), \
                 patch.object(dz, 'publish') as pub, quiet():
                dz.main()
            seen = json.loads(state.read_text(encoding='utf-8'))
        pub.assert_called_once()
        self.assertEqual(pub.call_args.args[0], '@c')
        self.assertIn('https://a/1', seen)
        self.assertIn('skip:https://a/2', seen)
        self.assertIn('title:Тема', seen)


if __name__ == '__main__':
    unittest.main()
