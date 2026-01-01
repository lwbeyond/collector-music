from __future__ import annotations

from contextlib import closing
import copy
from pathlib import Path
import tempfile
import unittest

from collector_music.common import MusicError, json_bytes, parse_json
from collector_music.netease import import_netease
from collector_music.pano import import_pano
from collector_music.song_tags import (
    FORMAT, catalog_tracks, import_song_tags, latest_song_tags, normalize_song_tags, pending_song_ids,
)
from collector_music.store import Workspace, insert_row
from tests.test_pipeline import line, netease_document, pano_row
from tests.test_tag_coverage import encyclopedia

ROOT = Path(__file__).resolve().parents[1]


def tags_document(observed='2026-10-30T01:00:00Z', songs=None, account='123456'):
    return dict(format=FORMAT, version=1, adapter_version='4.40.1', account_key=account,
        collected_at_utc=observed, songs=songs if songs is not None else [
            dict(song_id='123', observed_at_utc=observed, status='ok', body=encyclopedia())])


class SongTagTests(unittest.TestCase):
    def setUp(self):
        (ROOT / '.tmp').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / '.tmp', prefix='test-song-tags-')
        self.workspace = Workspace(self.temp.name).initialize()
        self.path = self.workspace.home / 'tags.json'

    def tearDown(self):
        self.temp.cleanup()

    def load(self, document=None, person='me'):
        self.path.write_bytes(json_bytes(document or tags_document()))
        return import_song_tags(self.workspace, self.path, person=person)

    def import_catalog(self, document=None, person='me'):
        path = self.workspace.home / 'catalog.json'
        path.write_bytes(json_bytes(document or netease_document()))
        return import_netease(self.workspace, path, person=person)

    def test_catalog_unions_history_by_id_and_keeps_unmatched_pano_separate(self):
        first = netease_document()
        first['scopes']['likes']['body']['ids'] = [123, 234]
        self.import_catalog(first)
        second = copy.deepcopy(first)
        second['collected_at_utc'] = '2026-10-03T01:00:00Z'
        second['scopes'] = {'likes': dict(status='ok', observed_at_utc='2026-10-03T00:59:00Z',
            body=dict(code=200, ids=[456]))}
        self.import_catalog(second)
        path = self.workspace.home / 'pano.jsonl'
        path.write_bytes(line(pano_row()) * 2)
        import_pano(self.workspace, path, device='phone', stream='main')
        other = netease_document()
        other['account_key'] = '99'
        other['scopes'] = {'likes': dict(status='ok', observed_at_utc='2026-10-03T00:59:00Z',
            body=dict(code=200, ids=[789]))}
        self.import_catalog(other, person='another')
        with closing(self.workspace.connect(readonly=True)) as conn:
            catalog = catalog_tracks(conn)
            self.assertEqual(catalog['song_ids'], ['123', '234', '456'])
            self.assertEqual(catalog['sources'], dict(observations=1, rankings=1, likes_history=3))
            self.assertEqual(catalog['unmatched_pano_count'], 1)
            self.assertEqual(catalog['unmatched_pano_observations'], 2)
            self.assertEqual(pending_song_ids(conn), catalog['song_ids'])

    def test_import_is_idempotent_and_does_not_create_listening_facts(self):
        self.import_catalog()
        with closing(self.workspace.connect(readonly=True)) as conn:
            observations = conn.execute('SELECT count(*) FROM observations').fetchone()[0]
            self.assertEqual(pending_song_ids(conn), ['123'])
        self.assertEqual(self.load()['songs'], {'123': 'ok'})
        self.assertEqual(self.load()['status'], 'duplicate')
        with closing(self.workspace.connect(readonly=True)) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM song_tags').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT count(*) FROM observations').fetchone()[0], observations)
            self.assertEqual(pending_song_ids(conn), [])
            result = latest_song_tags(conn)['123']
        self.assertEqual(result['fields']['genre'], ['流行-欧美流行'])
        self.assertEqual(result['status'], 'ok')
        for key in ('account_key', 'person_key', 'payload_json', 'cookie'):
            self.assertNotIn(key, result)

    def test_catalog_keeps_same_title_distinct_ids_and_excludes_other_platforms(self):
        self.import_catalog()
        with self.workspace.transaction() as conn:
            original = dict(conn.execute('SELECT * FROM observations LIMIT 1').fetchone())
            insert_row(conn, 'observations', dict(original, id='same-title-second-id',
                platform_track_id='234', recording_key='netease:track:234', source_record_id='second'))
            insert_row(conn, 'observations', dict(original, id='foreign-platform',
                source='another_platform', platform_track_id='999', recording_key='foreign:999', source_record_id='foreign'))
        with closing(self.workspace.connect(readonly=True)) as conn:
            self.assertEqual(conn.execute('SELECT count(DISTINCT track) FROM observations').fetchone()[0], 1)
            self.assertEqual(catalog_tracks(conn)['song_ids'], ['123', '234'])
            self.assertEqual(pending_song_ids(conn), ['123', '234'])

    def test_failure_keeps_success_but_explicit_empty_replaces_it(self):
        self.import_catalog()
        self.load()
        observed = '2026-10-30T02:00:00Z'
        failed = tags_document(observed, [dict(song_id=123, observed_at_utc=observed, status='failed', error='offline')])
        self.assertEqual(self.load(failed)['status'], 'partial')
        with closing(self.workspace.connect(readonly=True)) as conn:
            self.assertEqual(latest_song_tags(conn)['123']['status'], 'stale')
            self.assertEqual(latest_song_tags(conn)['123']['fields']['genre'], ['流行-欧美流行'])
            self.assertEqual(pending_song_ids(conn), [])
        observed = '2026-10-30T03:00:00Z'
        empty = tags_document(observed, [dict(song_id=123, observed_at_utc=observed, status='ok',
            body=dict(code=200, data=dict(blocks=[])))])
        self.assertEqual(self.load(empty)['songs'], {'123': 'empty'})
        with closing(self.workspace.connect(readonly=True)) as conn:
            result = latest_song_tags(conn)['123']
            self.assertEqual(result['status'], 'empty')
            self.assertEqual(result['fields']['genre'], [])
            self.assertEqual(result['fields']['bpm'], [])
            self.assertEqual(pending_song_ids(conn), [])

    def test_structurally_invalid_response_is_failed_and_pending(self):
        self.import_catalog()
        document = tags_document()
        document['songs'][0]['body'] = {'code': 200}
        self.assertEqual(self.load(document)['songs'], {'123': 'failed'})
        with closing(self.workspace.connect(readonly=True)) as conn:
            self.assertEqual(pending_song_ids(conn), ['123'])
            self.assertEqual(latest_song_tags(conn)['123']['status'], 'unavailable')
            self.assertIsNone(latest_song_tags(conn)['123']['data_status'])

    def test_global_cache_is_shared_by_platform_id_not_person_or_name(self):
        catalog = netease_document()
        catalog['scopes']['likes']['body']['ids'] = [123, 234]
        self.import_catalog(catalog)
        self.load(tags_document(account='99'), person='another')
        with closing(self.workspace.connect(readonly=True)) as conn:
            self.assertEqual(pending_song_ids(conn), ['234'])
        with self.assertRaises(MusicError):
            self.load(tags_document(account='99'), person='me')

    def test_credentials_duplicate_ids_and_future_attempts_are_rejected_before_archive(self):
        for kind in ('credentials', 'duplicate', 'future'):
            document = tags_document()
            if kind == 'credentials':
                document['songs'][0]['body']['nested'] = {'Authorization': 'SECRET'}
            elif kind == 'duplicate':
                document['songs'].append(dict(document['songs'][0], song_id='00123'))
            else:
                document['songs'][0]['observed_at_utc'] = '2026-10-31T01:00:00Z'
            with self.subTest(kind=kind), self.assertRaises(MusicError):
                self.load(document)
        self.assertFalse(list((self.workspace.raw / 'captures').glob('*.json')))

    def test_bpm_numeric_duplicates_removed_and_recommendations_ignored(self):
        body = encyclopedia()
        basic = body['data']['blocks'][1]
        basic['creatives'][-1]['uiElement']['textLinks'] = [{'text': '97'}, {'text': '97.0'}, {'text': '0'}, {'text': 'NaN'}]
        state, result = normalize_song_tags(body)
        self.assertEqual(state, 'ok')
        self.assertEqual(result['fields']['bpm'], [97.0])
        self.assertEqual(result['fields']['bpm_raw'], ['97', '97.0', '0', 'NaN'])
        self.assertEqual(result['fields']['recommended_tags'], ['欢快'])
        body['data']['blocks'] = body['data']['blocks'][:1]
        state, result = normalize_song_tags(body)
        self.assertEqual(state, 'empty')
        self.assertEqual(result['fields']['recommended_tags'], [])

    def test_migration_preserves_identity_and_preexisting_facts(self):
        self.import_catalog()
        with closing(self.workspace.connect()) as conn:
            identity = conn.execute("SELECT value FROM meta WHERE key='database_id'").fetchone()[0]
            facts = conn.execute('SELECT count(*) FROM observations').fetchone()[0]
            conn.execute('DROP TABLE song_tags')
            conn.execute('PRAGMA user_version=2')
            conn.execute("UPDATE meta SET value='1.1.0' WHERE key='schema_version'")
            conn.commit()
        with closing(self.workspace.connect(readonly=True)) as conn:
            self.assertEqual(pending_song_ids(conn), ['123'])
        self.workspace.initialize()
        with closing(self.workspace.connect(readonly=True)) as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 3)
            self.assertEqual(conn.execute("SELECT value FROM meta WHERE key='database_id'").fetchone()[0], identity)
            self.assertEqual(conn.execute('SELECT count(*) FROM observations').fetchone()[0], facts)
            self.assertEqual(conn.execute('SELECT count(*) FROM song_tags').fetchone()[0], 0)

    def test_tied_times_use_import_then_row_id_for_cache_and_failure_state(self):
        self.load()
        changed = tags_document()
        changed['songs'][0]['status'] = 'failed'
        self.load(changed)
        with closing(self.workspace.connect(readonly=True)) as conn:
            newest = conn.execute('SELECT * FROM song_tags ORDER BY observed_at_utc DESC,import_id DESC,id DESC').fetchone()
            expected = 'stale' if newest['status'] == 'failed' else 'ok'
            self.assertEqual(latest_song_tags(conn)['123']['status'], expected)


if __name__ == '__main__':
    unittest.main()
