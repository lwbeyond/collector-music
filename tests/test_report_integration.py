import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import duckdb

from collector_music.audit import audit
from collector_music.common import MusicError, json_bytes
from collector_music.dashboard import dashboard_data
from collector_music.lake import publish
from collector_music.platform_reports import import_reports
from collector_music.store import Workspace
from integrations.music_snapshot import MusicSnapshot
from test_pipeline import ROOT


class ReportIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / '.tmp')
        self.root = Path(self.temporary.name)
        self.workspace = Workspace(self.root / 'workspace').initialize()

    def tearDown(self):
        self.temporary.cleanup()

    def capture(self, day, tags=None, failed=False):
        entry = dict(status='failed' if failed else 'ok', observed_at_utc=f'2026-10-{day:02d}T00:00:00Z')
        if not failed:
            entry['body'] = dict(code=200, data=dict(tagPreferenceVos=tags or []))
        else:
            entry['error'] = 'fixture outage'
        document = dict(format='collector-music-netease-reports', version=1, adapter_version='fixture',
            account_key='123456', collected_at_utc=f'2026-10-{day:02d}T00:01:00Z', reports={'preference': entry})
        path = self.root / 'report.json'
        path.write_bytes(json_bytes(document))
        return import_reports(self.workspace, path)

    def test_report_archive_replays_and_same_capture_is_idempotent(self):
        first = self.capture(1, [dict(tagName='流行', ratio='53')])
        duplicate = import_reports(self.workspace, self.root / 'report.json')
        self.assertEqual(duplicate['status'], 'duplicate')
        self.assertEqual(duplicate['inserted_records'], 0)
        publish(self.workspace)
        result = audit(self.workspace, recovery_check=True)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['published_table_checks']['platform_reports']['rows'], 1)
        self.assertEqual(result['replay_fact_checks']['platform_reports']['rows'], 1)
        with closing(self.workspace.connect(readonly=True)) as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM observations').fetchone()[0], 0)
            self.assertEqual(connection.execute('SELECT import_id FROM platform_reports').fetchone()[0], first['import_id'])

    def test_current_report_retains_failure_and_empty_semantics_with_as_of(self):
        self.capture(1, [dict(tagName='流行', ratio='53')])
        self.capture(2, failed=True)
        publish(self.workspace)
        with duckdb.connect() as connection:
            MusicSnapshot(self.workspace.data / 'lake').attach(connection)
            row = connection.execute('SELECT status,snapshot_stale FROM music.current_platform_reports').fetchone()
            self.assertEqual(row, ('ok', True))
            self.assertEqual(connection.execute('SELECT count(*) FROM music.platform_report_history').fetchone()[0], 2)
        failed_audit = audit(self.workspace, recovery_check=True)
        self.assertEqual(failed_audit['status'], 'warning')
        self.assertTrue(any('平台报告/preference' in warning for warning in failed_audit['warnings']))

        self.capture(3, [])
        publish(self.workspace)
        with duckdb.connect() as connection:
            MusicSnapshot(self.workspace.data / 'lake').attach(connection)
            row = connection.execute('SELECT status,snapshot_stale,normalized_json FROM music.current_platform_reports').fetchone()
            self.assertEqual(row[:2], ('empty', False))
            self.assertEqual(json.loads(row[2])['tags'], [])
        with duckdb.connect() as connection:
            MusicSnapshot(self.workspace.data / 'lake', as_of='2026-10-01T00:01:00Z').attach(connection)
            self.assertEqual(connection.execute('SELECT status,snapshot_stale FROM music.current_platform_reports').fetchone(), ('ok', False))
            self.assertEqual(connection.execute('SELECT count(*) FROM music.platform_report_history').fetchone()[0], 1)
            self.assertEqual(connection.execute('SELECT count(*) FROM music.platform_reports').fetchone()[0], 3)

    def test_same_revision_report_tampering_is_detected(self):
        self.capture(1, [dict(tagName='流行', ratio='53')])
        publish(self.workspace)
        with self.workspace.transaction() as connection:
            connection.execute("UPDATE platform_reports SET normalized_json='{}'")
        with self.assertRaisesRegex(MusicError, 'platform_reports.*SQLite'):
            audit(self.workspace)

    def test_timestamp_ties_choose_the_same_attempt_in_all_consumers(self):
        self.capture(1, [dict(tagName='流行', ratio='53')])
        self.capture(1, [dict(tagName='摇滚', ratio='47')])
        self.capture(1, failed=True)
        publish(self.workspace)
        with closing(self.workspace.connect(readonly=True)) as connection:
            latest = connection.execute('SELECT status FROM platform_reports ORDER BY observed_at_utc DESC,import_id DESC,id DESC LIMIT 1').fetchone()[0]
            expected = connection.execute("SELECT id,normalized_json FROM platform_reports WHERE status='ok' ORDER BY observed_at_utc DESC,import_id DESC,id DESC LIMIT 1").fetchone()
        view = dashboard_data(self.workspace.data / 'lake')['platform_reports']['preference']
        self.assertEqual(view['id'], expected[0])
        self.assertEqual(view['status'] == 'stale', latest == 'failed')
        with duckdb.connect() as connection:
            MusicSnapshot(self.workspace.data / 'lake').attach(connection)
            row = connection.execute('SELECT id,normalized_json,snapshot_stale FROM music.current_platform_reports').fetchone()
            self.assertEqual(row, (expected[0], expected[1], latest == 'failed'))
        self.assertEqual(audit(self.workspace)['latest_platform_reports'][0]['status'], latest)


if __name__ == '__main__':
    unittest.main()
