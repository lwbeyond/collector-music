from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from collector_music.common import LoginRequired, MusicError, atomic_write, json_bytes, parse_json
from collector_music.store import Workspace
from scripts.check_tag_coverage import check_coverage, extract_labels, select_sample, summarize

ROOT = Path(__file__).resolve().parents[1]


def encyclopedia():
    return {'code': 200, 'data': {'blocks': [
        {'code': 'SONG_PLAY_ABOUT_RELATED_PLAYLIST', 'creatives': [
            {'uiElement': {'mainTitle': {'title': '推荐标签'}},
             'resources': [{'uiElement': {'mainTitle': {'title': '错误的歌单情绪'}}}]}]},
        {'code': 'SONG_PLAY_ABOUT_SONG_BASIC', 'creatives': [
            {'uiElement': {'mainTitle': {'title': '曲风'}, 'textLinks': None},
             'resources': [{'uiElement': {'mainTitle': {'title': '流行-欧美流行'}}}]},
            {'uiElement': {'mainTitle': {'title': '推荐标签'}},
             'resources': [{'uiElement': {'mainTitle': {'title': '欢快'}}},
                           {'uiElement': {'mainTitle': {'title': '欢快'}}}]},
            {'uiElement': {'mainTitle': {'title': '语种'}, 'textLinks': [{'text': '英语'}]}},
            {'uiElement': {'mainTitle': {'title': 'BPM'}, 'textLinks': [{'text': '97.5'}, {'text': '未知'}]}}
        ]}]}}


class TagCoverageTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(':memory:')
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript('''
            CREATE TABLE observations(platform_track_id TEXT, occurred_at_utc TEXT, source TEXT,
                account_key TEXT, event_kind TEXT);
            CREATE TABLE coverage(source TEXT, scope TEXT, account_key TEXT, observed_at_utc TEXT,
                import_id TEXT, transport_status TEXT);
            CREATE TABLE library_items(entity_id TEXT, position INTEGER, import_id TEXT, scope TEXT, account_key TEXT);
        ''')

    def tearDown(self):
        self.connection.close()

    def add_sources(self):
        self.connection.executemany('INSERT INTO observations VALUES(?,?,?,?,?)',
            [(str(i), '2026-10-01T00:00:00Z', 'netease_recent', '42', 'recent_observation') for i in range(1, 31)])
        self.connection.execute("INSERT INTO observations VALUES('other',NULL,'netease_recent','99','recent_observation')")
        self.connection.execute("INSERT INTO coverage VALUES('netease','likes','42','2026-10-01','good','ok')")
        self.connection.executemany('INSERT INTO library_items VALUES(?,?,?,?,?)',
            [(str(i), i, 'good', 'likes', '42') for i in range(1, 101)])

    def test_sample_has_fifteen_per_source_unique_and_spread(self):
        self.add_sources()
        sample, basis = select_sample(self.connection, '42')
        self.assertEqual(len(sample), 30)
        self.assertEqual(len({s['song_id'] for s in sample}), 30)
        self.assertEqual(basis['sources'], {'recent': 15, 'likes': 15})
        self.assertEqual(sample[-1]['song_id'], '100')
        self.assertNotIn('other', {s['song_id'] for s in sample})
        self.assertEqual(sample, select_sample(self.connection, '42')[0])

    def test_successful_empty_likes_does_not_resurrect_old_snapshot(self):
        self.add_sources()
        self.connection.execute("INSERT INTO coverage VALUES('netease','likes','42','2026-10-02','empty','ok')")
        sample, basis = select_sample(self.connection, '42')
        self.assertEqual(basis['liked_pool'], 0)
        self.assertEqual(basis['sources'], {'recent': 30, 'likes': 0})
        self.assertEqual(len(sample), 30)

    def test_latest_failed_likes_marks_retained_sample_stale(self):
        self.add_sources()
        self.connection.execute("INSERT INTO coverage VALUES('netease','likes','42','2026-10-02','failed','failed')")
        _, basis = select_sample(self.connection, '42')
        self.assertTrue(basis['likes_snapshot_stale'])
        self.assertEqual(basis['liked_pool'], 100)

    def test_extracts_only_song_basic_fields_without_guessing_hierarchy(self):
        result = extract_labels(encyclopedia())
        self.assertEqual(result['fields']['genre'], ['流行-欧美流行'])
        self.assertEqual(result['fields']['recommended_tags'], ['欢快'])
        self.assertEqual(result['fields']['bpm'], [97.5])
        self.assertTrue(all(result['available'].values()))

    def test_related_playlist_labels_do_not_fill_missing_song_tags(self):
        body = encyclopedia()
        body['data']['blocks'] = body['data']['blocks'][:1]
        result = extract_labels(body)
        self.assertFalse(result['basic_block_present'])
        self.assertFalse(any(result['available'].values()))
        with self.assertRaises(MusicError):
            extract_labels({'code': 200, 'data': {}})
        malformed = encyclopedia()
        malformed['data']['blocks'][1]['creatives'][0]['uiElement'] = []
        with self.assertRaises(MusicError):
            extract_labels(malformed)

    def test_summary_separates_missing_labels_from_errors_and_pending(self):
        known = dict(status='ok', available=dict(genre=True, language=True, bpm=False, recommended_tags=False))
        report = summarize([known, dict(status='failed')], {'selected': 3})
        self.assertEqual((report['attempted'], report['succeeded'], report['failed'], report['not_attempted']), (2, 1, 1, 1))
        self.assertEqual(report['coverage']['genre']['fraction'], 1)
        self.assertEqual(report['coverage']['bpm']['missing'], 1)

    def test_probe_redacts_credentials_and_stops_after_login_failure(self):
        (ROOT / '.tmp').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / '.tmp', prefix='test-tag-') as temp:
            workspace = Workspace(temp).initialize()
            atomic_write(workspace.home / '.local/session.json', json_bytes({'cookie': 'SECRET', 'account_key': '42'}))
            body = encyclopedia()
            body['cookie'] = 'SECRET'
            body['data']['Authorization'] = 'SECRET'
            fake_sample = ([{'song_id': str(i), 'sample_source': 'recent'} for i in range(1, 4)], {'selected': 3})
            responses = [{'body': {'data': {'code': 200, 'account': {'id': 42}}}},
                         {'body': body}, LoginRequired('登录失效')]
            output = workspace.home / '.local/validation/test'
            with patch('scripts.check_tag_coverage.select_sample', return_value=fake_sample), \
                 patch('scripts.check_tag_coverage.bridge', side_effect=responses) as call, \
                 patch('scripts.check_tag_coverage.time.sleep') as sleep:
                report = check_coverage(workspace, output, notify=lambda _: None)
            self.assertEqual(call.call_count, 3)
            self.assertEqual(sleep.call_count, 2)
            self.assertEqual([r['status'] for r in report['results']], ['ok', 'failed', 'not_attempted'])
            for path in output.rglob('*.json'):
                self.assertNotIn('SECRET', path.read_text(encoding='utf-8'))
            self.assertTrue(parse_json((output / 'summary.json').read_bytes())['results'][1]['needs_login'])


if __name__ == '__main__':
    unittest.main()
