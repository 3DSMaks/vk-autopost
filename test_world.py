import contextlib
import io
import json
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import world_post as wp


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


def item(source, title, minutes=5, link=None):
    return {'source': source, 'title': title, 'link': link or f'https://{source}.ru/{abs(hash(title))}',
            'date': datetime.now(timezone.utc) - timedelta(minutes=minutes), 'summary': '', 'image': ''}


RSS = '''<?xml version="1.0" encoding="windows-1251"?><rss><channel>
<item><title>Свежая</title><link>https://a.ru/1</link><pubDate>{fresh}</pubDate></item>
<item><title>Старая</title><link>https://a.ru/2</link><pubDate>{old}</pubDate></item>
</channel></rss>'''


class WorldTests(unittest.TestCase):
    def test_feed_keeps_only_fresh_and_decodes_encoding(self):
        now = datetime.now(timezone.utc)
        fmt = '%a, %d %b %Y %H:%M:%S +0000'
        raw = RSS.format(fresh=(now - timedelta(minutes=10)).strftime(fmt),
                         old=(now - timedelta(hours=5)).strftime(fmt)).encode('cp1251')
        with patch.object(wp.news, 'http', return_value=raw):
            got = wp.fetch_feed('Интерфакс', 'https://x', now)
        self.assertEqual([i['title'] for i in got], ['Свежая'])

    def test_google_news_title_suffix_removed(self):
        now = datetime.now(timezone.utc)
        raw = (f'<rss><item><title>Quake hits Chile - Reuters</title><link>https://news.google.com/rss/articles/x</link>'
               f'<pubDate>{now.strftime("%a, %d %b %Y %H:%M:%S +0000")}</pubDate></item></rss>').encode()
        with patch.object(wp.news, 'http', return_value=raw):
            got = wp.fetch_feed('Reuters', wp.GOOGLE + 'reuters.com', now)
        self.assertEqual(got[0]['title'], 'Quake hits Chile')
        self.assertFalse(wp.can_open(got[0]))

    def test_single_source_event_is_not_published(self):
        items = [item('ТАСС', 'Землетрясение в Чили'), item('ТАСС', 'Землетрясение в Чили: подробности'),
                 item('BBC', 'Election results'), item('Reuters', 'Election results announced')]
        reply = {'events': [{'items': [0, 1], 'title': 'Землетрясение', 'importance': 9},
                            {'items': [2, 3], 'title': 'Итоги выборов', 'importance': 8},
                            {'items': [2, 3, 99], 'title': 'Мелочь', 'importance': 3}]}
        with patch.object(wp.tg, 'gemini_json', return_value=reply), quiet():
            events = wp.confirmed_events(items, [])
        self.assertEqual([e['title'] for e in events], ['Итоги выборов'])
        self.assertEqual(events[0]['sources'], ['BBC', 'Reuters'])

    def test_text_names_sources_and_has_no_markup(self):
        event = {'title': 'Итоги', 'importance': 8, 'sources': ['ТАСС', 'Reuters'],
                 'items': [item('ТАСС', 'Итоги'), item('Reuters', 'Results', link='https://news.google.com/rss/articles/x')]}
        with patch.object(wp.tg, 'gemini_json', return_value={'title': '**Итоги выборов**', 'body': 'Факт.\n\n\n\nЕщё факт.'}), \
             patch.object(wp.tg, 'article_text', return_value='текст') as text:
            msg = wp.write(event)
        self.assertEqual(msg, '⚡ Итоги выборов\n\nФакт.\n\nЕщё факт.\n\nИсточники: ТАСС, Reuters')
        self.assertEqual(text.call_count, 1)  # ссылку Google News не открываем

    def test_group_id_from_link(self):
        for raw in ('https://vk.ru/club123', 'public123', '123', 'vk.com/club123'):
            with patch.dict(os.environ, {'WORLD_GROUP_ID': raw}):
                self.assertEqual(wp.group_id(), '123', raw)
        with patch.dict(os.environ, {'WORLD_GROUP_ID': 'https://vk.ru/worldnews'}), \
             patch.object(wp.news, 'vk', return_value={'groups': [{'id': 555}]}), patch.object(wp.news, 'photo_token', return_value='T'):
            self.assertEqual(wp.group_id(), '555')

    def test_main_publishes_and_remembers_all_links(self):
        items = [item('ТАСС', 'Событие'), item('BBC', 'Event')]
        event = {'title': 'Событие', 'importance': 7, 'items': items, 'sources': ['ТАСС', 'BBC']}
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / 'seen_world.json'
            state.write_text('[]', encoding='utf-8')
            with patch.object(wp, 'SEEN_FILE', state), patch.dict(os.environ, {'WORLD_GROUP_ID': '42'}), \
                 patch.object(wp, 'fresh_items', return_value=items), patch.object(wp, 'confirmed_events', return_value=[event]), \
                 patch.object(wp, 'write', return_value='🌍 Событие'), patch.object(wp, 'photos_for', return_value=[]), \
                 patch.object(wp, 'post_token', return_value='T'), \
                 patch.object(wp.news, 'vk', side_effect=lambda m, **p: calls.append((m, p)) or {'post_id': 1}), quiet():
                wp.main()
            seen = json.loads(state.read_text(encoding='utf-8'))
        self.assertEqual(calls[0][0], 'wall.post')
        self.assertEqual(calls[0][1]['owner_id'], '-42')
        self.assertNotIn('attachments', calls[0][1])
        self.assertIn(items[0]['link'], seen)
        self.assertIn('skip:' + items[1]['link'], seen)
        self.assertIn('title:Событие', seen)
        self.assertEqual(wp.tg.history_links(seen), {items[0]['link'], items[1]['link']})


if __name__ == '__main__':
    unittest.main()
