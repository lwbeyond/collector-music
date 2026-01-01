from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import duckdb

from collector_music.audit import audit
from collector_music.common import MusicError, digest, json_bytes
from collector_music.lake import publish, resolve_version, verify_version
from collector_music.song_tags import import_song_tags
from collector_music.store import Workspace
from integrations.music_snapshot import MusicSnapshot
from test_pipeline import ROOT


def wiki_body(genre='流行'):
    creatives = []
    for title, values in [('曲风', [genre]), ('语种', ['英语']), ('BPM', ['127']), ('推荐标签', ['活力'])]:
        creatives.append({'uiElement': {'mainTitle': {'title': title}},
            'resources': [{'uiElement': {'mainTitle': {'title': value}}} for value in values]})
    return {'code': 200, 'data': {'blocks': [{'code': 'SONG_PLAY_ABOUT_SONG_BASIC', 'creatives': creatives}]}}


class SongTagIntegrationTests(unittest.TestCase):
    def setUp(self):
        (ROOT / '.tmp').mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / '.tmp')
        self.root = Path(self.temporary.name)
        self.workspace = Workspace(self.root / 'workspace').initialize()
        self.lake = self.workspace.data / 'lake'

    def tearDown(self):
        self.temporary.cleanup()

    def capture(self, day, *, song_id='123', account='123456', genre='流行', failed=False, empty=False):
        entry = dict(song_id=song_id, status='failed' if failed else 'ok',
            observed_at_utc=f'2026-10-{day:02d}T00:00:00Z')
        if failed:
            entry['error'] = 'fixture outage'
        else:
            entry['body'] = {'code': 200, 'data': {'blocks': []}} if empty else wiki_body(genre)
        document = dict(format='collector-music-netease-song-tags', version=1, account_key=account,
            adapter_version='fixture', collected_at_utc=f'2026-10-{day:02d}T00:05:00Z', songs=[entry])
        path = self.root / 'tags.json'
        path.write_bytes(json_bytes(document))
        return import_song_tags(self.workspace, path)

    def current(self, as_of=None):
        with duckdb.connect() as connection:
            reader = MusicSnapshot(self.lake, as_of=as_of).attach(connection)
            rows = connection.execute('''SELECT platform_track_id,status,snapshot_stale,genre,language,bpm,
                bpm_raw,recommended_tags,account_key,latest_attempt_status FROM music.current_song_tags
                ORDER BY platform_track_id''').fetchall()
            return reader, rows

    def test_publication_replay_and_duplicate_preserve_tag_facts_and_typed_labels(self):
        first = self.capture(1)
        duplicate = import_song_tags(self.workspace, self.root / 'tags.json')
        self.assertEqual(duplicate['status'], 'duplicate')
        self.assertEqual(duplicate['inserted_records'], 0)
        publication = publish(self.workspace)
        self.assertEqual(publication['datasets']['song_tags'], 1)
        result = audit(self.workspace, recovery_check=True)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['published_table_checks']['song_tags']['rows'], 1)
        self.assertEqual(result['replay_fact_checks']['song_tags']['rows'], 1)
        self.assertEqual(result['latest_song_tag_status'], [dict(source='netease', status='ok', tracks=1)])
        reader, rows = self.current()
        self.assertEqual(reader.manifest['schema_version'], '1.2.0')
        self.assertEqual(reader.analysis_view_version, '1.2.0')
        self.assertEqual(rows, [('123', 'ok', False, ['流行'], ['英语'], [127.0], ['127'], ['活力'], '123456', 'ok')])
        with closing(self.workspace.connect(readonly=True)) as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM observations').fetchone()[0], 0)
            self.assertEqual(connection.execute('SELECT import_id FROM song_tags').fetchone()[0], first['import_id'])

    def test_failure_empty_and_as_of_use_completed_capture_time(self):
        self.capture(1)
        self.capture(2, failed=True)
        publish(self.workspace)
        self.assertEqual(self.current()[1][0][1:4], ('ok', True, ['流行']))
        self.assertEqual(self.current(as_of='2026-10-01T00:04:59Z')[1], [])
        self.assertEqual(self.current(as_of='2026-10-01T00:05:00Z')[1][0][1:4], ('ok', False, ['流行']))
        with duckdb.connect() as connection:
            MusicSnapshot(self.lake, as_of='2026-10-01T00:05:00Z').attach(connection)
            self.assertEqual(connection.execute('SELECT count(*) FROM music.song_tag_history').fetchone()[0], 1)
            self.assertEqual(connection.execute('SELECT count(*) FROM music.song_tags').fetchone()[0], 2)
        self.capture(3, empty=True)
        self.capture(4, failed=True)
        publish(self.workspace)
        row = self.current()[1][0]
        self.assertEqual(row[1:4], ('empty', True, []))
        self.assertEqual(row[4:8], ([], [], [], []))
        result = audit(self.workspace, recovery_check=True)
        self.assertEqual(result['status'], 'warning')
        self.assertTrue(any('歌曲标签有 1 首' in message for message in result['warnings']))
        self.assertEqual(result['replay_fact_checks']['song_tags']['rows'], 4)

    def test_current_cache_is_global_by_source_and_id_not_account_or_labels(self):
        self.capture(1, account='100', genre='摇滚')
        self.capture(2, account='200', genre='电子')
        self.capture(2, song_id='124', account='200', genre='电子')
        self.capture(3, account='100', failed=True)
        self.capture(3, song_id='999', failed=True)
        publish(self.workspace)
        rows = self.current()[1]
        self.assertEqual([row[0] for row in rows], ['123', '124'])
        self.assertEqual(rows[0][1:4], ('ok', True, ['电子']))
        self.assertEqual(rows[0][8], '200')
        self.assertEqual(rows[1][1:4], ('ok', False, ['电子']))
        with duckdb.connect() as connection:
            MusicSnapshot(self.lake).attach(connection)
            self.assertEqual(connection.execute('SELECT count(*) FROM music.latest_song_tag_attempt').fetchone()[0], 3)
            self.assertEqual(connection.execute('SELECT count(*) FROM music.song_tag_history').fetchone()[0], 5)

    def test_same_timestamp_ties_match_core_cache_order(self):
        self.capture(1, genre='摇滚')
        self.capture(1, genre='电子')
        self.capture(1, failed=True)
        publish(self.workspace)
        with closing(self.workspace.connect(readonly=True)) as connection:
            expected = connection.execute("SELECT id FROM song_tags WHERE status IN ('ok','empty') ORDER BY observed_at_utc DESC,import_id DESC,id DESC LIMIT 1").fetchone()[0]
            latest = connection.execute('SELECT status FROM song_tags ORDER BY observed_at_utc DESC,import_id DESC,id DESC LIMIT 1').fetchone()[0]
        with duckdb.connect() as connection:
            MusicSnapshot(self.lake).attach(connection)
            self.assertEqual(connection.execute('SELECT id,snapshot_stale FROM music.current_song_tags').fetchone(),
                (expected, latest == 'failed'))

    def test_legacy_publications_remain_readable_and_new_contract_requires_tags(self):
        publish(self.workspace)
        source = resolve_version(self.lake)
        for version in ('1.0.0', '1.1.0'):
            with self.subTest(version=version):
                lake = self.root / ('legacy-' + version)
                directory = lake / 'versions' / source.name
                shutil.copytree(source, directory)
                manifest = json.loads((directory / 'manifest.json').read_text('utf-8'))
                manifest['schema_version'] = version
                del manifest['datasets']['song_tags']
                if version == '1.0.0':
                    del manifest['datasets']['platform_reports']
                (directory / 'manifest.json').write_bytes(json_bytes(manifest))
                self.assertEqual(verify_version(directory)['schema_version'], version)
                with duckdb.connect() as connection:
                    MusicSnapshot(lake, source.name).attach(connection)
                    self.assertEqual(connection.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema='music' AND table_name='current_song_tags'").fetchone()[0], 0)
                    self.assertEqual(connection.execute('SELECT count(*) FROM music.observations').fetchone()[0], 0)
        broken = self.root / 'broken' / 'versions' / source.name
        shutil.copytree(source, broken)
        manifest = json.loads((broken / 'manifest.json').read_text('utf-8'))
        del manifest['datasets']['song_tags']
        (broken / 'manifest.json').write_bytes(json_bytes(manifest))
        with self.assertRaisesRegex(MusicError, '数据集清单不完整'):
            verify_version(broken)
        with self.assertRaisesRegex(ValueError, 'Incomplete'):
            MusicSnapshot(broken.parent.parent, source.name)

    def test_same_revision_tag_tampering_is_detected(self):
        self.capture(1)
        publish(self.workspace)
        with self.workspace.transaction() as connection:
            connection.execute("UPDATE song_tags SET normalized_json='{}'")
        with self.assertRaisesRegex(MusicError, 'song_tags.*SQLite'):
            audit(self.workspace)

    def test_same_revision_legacy_manifest_cannot_hide_nonempty_tags(self):
        self.capture(1)
        publish(self.workspace)
        directory = resolve_version(self.lake)
        manifest = json.loads((directory / 'manifest.json').read_text('utf-8'))
        manifest['schema_version'] = '1.1.0'
        del manifest['datasets']['song_tags']
        encoded = json_bytes(manifest)
        (directory / 'manifest.json').write_bytes(encoded)
        pointer = json.loads((self.lake / 'current.json').read_text('utf-8'))
        pointer['manifest_sha256'] = digest(encoded)
        (self.lake / 'current.json').write_bytes(json_bytes(pointer))
        with self.assertRaisesRegex(MusicError, 'song_tags.*已有记录'):
            audit(self.workspace)


if __name__ == '__main__':
    unittest.main()
