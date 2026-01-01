from contextlib import ExitStack, closing
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from collector_music import SCHEMA_VERSION
from collector_music.automation import ensure_publication, run_tick
from collector_music.collector import account_from_status
from collector_music.common import LoginRequired, MusicError, digest, json_bytes, parse_json
from collector_music.lake import publish, resolve_version, verify_version
from collector_music.netease import import_netease
from collector_music.pano import import_pano
from collector_music.platform_reports import dashboard_reports, import_reports
from collector_music.store import Workspace
from collector_music.song_tags import import_song_tags
from collector_music.tag_collection import plan_song_tags
from test_pipeline import ROOT, line, pano_row, netease_document
from test_platform_reports import report_document


class AutomationTests(unittest.TestCase):
    def setUp(self):
        (ROOT / '.tmp').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / '.tmp')
        self.home = Path(self.temp.name)
        self.workspace = Workspace(self.home).initialize()
        (self.home / '.local/automation.json').write_bytes(json_bytes(dict(version=1, enabled=True)))
        (self.home / '.local/session.json').write_bytes(json_bytes(dict(fixture='not-a-credential')))
        source = self.home / 'pano.jsonl'
        source.write_bytes(line(pano_row()))
        import_pano(self.workspace, source, device='phone', stream='main')
        publish(self.workspace)
        self.stack = ExitStack()
        self.clock = self.stack.enter_context(patch('collector_music.automation.now', return_value='2026-10-03T01:00:00+00:00'))
        self.collector = self.stack.enter_context(patch('collector_music.automation.collect', side_effect=self.fake_collect))
        self.reports_collector = self.stack.enter_context(patch('collector_music.automation.collect_reports',
            return_value=dict(status='imported', reports={'monthly':'ok'}, inserted_records=1)))
        self.tag_plan = self.stack.enter_context(patch('collector_music.automation.plan_song_tags', return_value=dict(pending=0)))
        self.tag_collector = self.stack.enter_context(patch('collector_music.automation.collect_song_tags'))
        self.auditor = self.stack.enter_context(patch('collector_music.automation.audit', return_value=dict(status='ok', warnings=[])))
        # The scheduled runner must not inspect or receive any device, even if connected.
        self.receiver = self.stack.enter_context(patch('collector_music.devices.receive_configured', side_effect=AssertionError('USB not authorized')))

    def tearDown(self):
        self.receiver.assert_not_called()
        self.stack.close()
        self.temp.cleanup()

    def fake_collect(self, workspace, *, person, scopes, delay):
        self.assertGreaterEqual(delay, 3)
        document = netease_document()
        document['collected_at_utc'] = self.clock.return_value
        document['scopes'] = {key: dict(value, observed_at_utc=self.clock.return_value)
                              for key, value in document['scopes'].items() if key in scopes}
        path = self.home / 'collection.json'
        path.write_bytes(json_bytes(document))
        result = import_netease(workspace, path, person=person)
        return dict(result, scopes={name:'ok' for name in scopes})

    def state(self):
        return parse_json((self.home / '.local/automation/state.json').read_bytes())

    def test_due_jobs_publish_once_idle_ticks_are_quiet_and_missed_days_do_not_burst(self):
        first = run_tick(self.workspace)
        self.assertEqual(first['status'], 'ok')
        self.assertFalse(first['usb_enabled'])
        self.assertEqual(self.collector.call_count, 2)
        self.assertEqual(self.reports_collector.call_count, 1)
        logs = self.home / '.local/automation/events.jsonl'
        log_bytes = logs.read_bytes()
        generations = list((self.home / 'data/lake/versions').iterdir())
        run_tick(self.workspace)
        self.assertEqual(logs.read_bytes(), log_bytes)
        self.assertEqual(len(list((self.home / 'data/lake/versions').iterdir())), len(generations))
        self.clock.return_value = '2026-10-03T04:01:00+00:00'
        run_tick(self.workspace)
        self.assertEqual(self.collector.call_count, 3)
        self.assertEqual(self.collector.call_args.kwargs['scopes'], ('recent',))
        self.clock.return_value = '2026-10-11T04:01:00+00:00'
        run_tick(self.workspace)
        self.assertEqual(self.collector.call_count, 5)
        self.assertEqual(self.auditor.call_count, 2)
        self.assertEqual(len(list((self.home / 'data/backups/automatic').glob('*.sqlite'))), 1)

    def test_expired_login_pauses_both_platform_jobs_until_session_changes(self):
        self.collector.side_effect = LoginRequired('expired fixture')
        result = run_tick(self.workspace)
        self.assertEqual(result['status'], 'warning')
        self.assertEqual(self.collector.call_count, 1)
        self.assertEqual(self.state()['jobs']['library']['status'], 'login_required')
        self.assertEqual(self.state()['jobs']['backup']['status'], 'ok')
        self.clock.return_value = '2026-10-05T01:00:00+00:00'
        run_tick(self.workspace)
        self.assertEqual(self.collector.call_count, 1)
        (self.home / '.local/session.json').write_bytes(json_bytes(dict(fixture='new-session')))
        self.collector.side_effect = self.fake_collect
        self.assertEqual(run_tick(self.workspace)['status'], 'ok')
        self.assertEqual(self.collector.call_count, 3)
        self.assertNotIn('login_pause', self.state())

    def test_network_failures_back_off_and_do_not_block_local_maintenance(self):
        self.collector.side_effect = MusicError('offline fixture')
        run_tick(self.workspace)
        state = self.state()
        self.assertEqual(state['jobs']['recent']['next_due_at_utc'], '2026-10-03T01:30:00.000+00:00')
        self.assertEqual(state['jobs']['backup']['status'], 'ok')
        self.clock.return_value = '2026-10-03T01:15:00+00:00'
        run_tick(self.workspace)
        self.assertEqual(self.collector.call_count, 2)
        self.clock.return_value = '2026-10-03T01:31:00+00:00'
        run_tick(self.workspace)
        self.assertEqual(self.state()['jobs']['recent']['next_due_at_utc'], '2026-10-03T02:31:00.000+00:00')

    def test_partial_capture_is_published_and_retried_without_erasing_history(self):
        original = self.fake_collect
        def partial(*args, **kwargs):
            return dict(original(*args, **kwargs), status='partial')
        self.collector.side_effect = partial
        result = run_tick(self.workspace)
        self.assertEqual(result['status'], 'warning')
        self.assertEqual(result['jobs_run']['publication']['status'], 'published')
        self.assertEqual(self.state()['jobs']['recent']['failures'], 1)

    def test_overlapping_runner_is_rejected_and_disabled_config_does_nothing(self):
        with self.workspace.lock('automation'), self.assertRaisesRegex(MusicError, '已有 automation'):
            run_tick(self.workspace)
        self.collector.assert_not_called()
        (self.home / '.local/automation.json').write_bytes(json_bytes(dict(version=1, enabled=False)))
        self.assertEqual(run_tick(self.workspace)['status'], 'disabled')
        self.collector.assert_not_called()

    def test_busy_collector_defers_without_failure_or_false_success(self):
        with self.workspace.lock('collect'):
            result = run_tick(self.workspace)
        self.assertEqual(result['status'], 'ok')
        self.collector.assert_not_called()
        self.reports_collector.assert_not_called()
        for name in ('recent', 'library', 'reports'):
            item = self.state()['jobs'][name]
            self.assertEqual(item['status'], 'deferred')
            self.assertEqual(item['failures'], 0)
            self.assertNotIn('last_success_at_utc', item)
            self.assertEqual(item['next_due_at_utc'], '2026-10-03T01:01:00.000+00:00')
        self.clock.return_value = '2026-10-03T01:02:00+00:00'
        self.assertEqual(run_tick(self.workspace)['status'], 'ok')
        self.assertEqual(self.collector.call_count, 2)

    def test_interrupted_publication_retries_without_recollecting(self):
        with patch('collector_music.automation.publish', side_effect=MusicError('interrupted')):
            self.assertEqual(run_tick(self.workspace)['status'], 'warning')
        self.clock.return_value = '2026-10-03T01:31:00+00:00'
        result = run_tick(self.workspace)
        self.assertEqual(result['jobs_run']['publication']['status'], 'published')
        self.assertEqual(self.collector.call_count, 2)

    def test_server_error_does_not_require_relogin(self):
        with self.assertRaises(MusicError) as failure:
            account_from_status(dict(code=500))
        self.assertNotIsInstance(failure.exception, LoginRequired)
        with self.assertRaises(LoginRequired):
            account_from_status(dict(code=200, account=None))

    def test_tags_are_bounded_incremental_and_empty_queue_makes_no_requests(self):
        run_tick(self.workspace)
        self.tag_collector.assert_not_called()
        self.assertEqual(self.state()['jobs']['tags']['status'], 'unchanged')
        self.clock.return_value = '2026-10-03T01:16:00+00:00'
        self.tag_plan.return_value = dict(pending=27)
        self.tag_collector.return_value = dict(status='limited', attempted=20, ok=19, empty=1, failed=0, remaining=7, stop_reason=None)
        self.assertEqual(run_tick(self.workspace)['status'], 'ok')
        self.assertEqual(self.tag_collector.call_args.kwargs['limit'], 20)
        self.assertEqual(self.state()['jobs']['tags']['status'], 'pending')
        self.assertEqual(self.state()['jobs']['tags']['failures'], 0)
        self.clock.return_value = '2026-10-03T01:32:00+00:00'
        self.tag_plan.return_value = dict(pending=7)
        self.tag_collector.return_value = dict(status='complete', attempted=7, ok=7, empty=0, failed=0, remaining=0, stop_reason=None)
        self.assertEqual(run_tick(self.workspace)['status'], 'ok')
        self.assertEqual(self.state()['jobs']['tags']['status'], 'ok')

    def test_tag_login_failure_pauses_and_session_change_resumes(self):
        self.tag_plan.return_value = dict(pending=1)
        self.tag_collector.return_value = dict(status='stopped', stop_reason='login_required', attempted=0)
        self.assertEqual(run_tick(self.workspace)['status'], 'warning')
        self.assertEqual(self.state()['jobs']['tags']['status'], 'login_required')
        self.clock.return_value = '2026-10-03T02:00:00+00:00'
        run_tick(self.workspace)
        self.assertEqual(self.tag_collector.call_count, 1)
        (self.home / '.local/session.json').write_bytes(json_bytes(dict(fixture='new-session')))
        self.tag_plan.return_value = dict(pending=0)
        self.assertEqual(run_tick(self.workspace)['status'], 'ok')
        self.assertNotIn('login_pause', self.state())

    def test_new_song_is_discovered_after_core_import_and_empty_tags_stay_cached(self):
        self.tag_plan.side_effect = plan_song_tags
        def capture_tags(workspace, *, person, limit, delay):
            ids = plan_song_tags(workspace, person=person, limit=limit)['song_ids']
            payload = dict(format='collector-music-netease-song-tags', version=1, account_key='123456',
                collected_at_utc=self.clock.return_value, songs=[dict(song_id=key, status='ok',
                    observed_at_utc=self.clock.return_value, body=dict(code=200, data=dict(blocks=[]))) for key in ids])
            path = self.home / 'tags.json'
            path.write_bytes(json_bytes(payload))
            import_song_tags(workspace, path, person=person)
            return dict(status='complete', attempted=len(ids), ok=0, empty=len(ids), failed=0, remaining=0, stop_reason=None)
        self.tag_collector.side_effect = capture_tags
        self.assertEqual(run_tick(self.workspace)['status'], 'ok')
        self.assertEqual(self.tag_collector.call_count, 1)
        self.assertEqual(plan_song_tags(self.workspace)['pending'], 0)
        self.clock.return_value = '2026-10-03T01:16:00+00:00'
        self.assertEqual(run_tick(self.workspace)['status'], 'ok')
        self.assertEqual(self.tag_collector.call_count, 1)
        document = netease_document()
        document['collected_at_utc'] = self.clock.return_value
        document['scopes'] = {'likes': dict(status='ok', observed_at_utc=self.clock.return_value, body=dict(code=200, ids=[123, 456]))}
        path = self.home / 'new-song.json'
        path.write_bytes(json_bytes(document))
        import_netease(self.workspace, path)
        self.clock.return_value = '2026-10-03T01:32:00+00:00'
        self.assertEqual(run_tick(self.workspace)['status'], 'ok')
        self.assertEqual(self.tag_collector.call_count, 2)
        self.assertEqual(self.state()['jobs']['tags']['detail']['attempted'], 1)
        self.assertEqual(plan_song_tags(self.workspace)['pending'], 0)

    def test_failed_tags_back_off_without_blocking_core_publication(self):
        self.tag_plan.return_value = dict(pending=1)
        self.tag_collector.return_value = dict(status='stopped', attempted=1, failed=1, remaining=1, stop_reason='rate_limited')
        self.assertEqual(run_tick(self.workspace)['status'], 'warning')
        self.assertEqual(self.state()['jobs']['tags']['next_due_at_utc'], '2026-10-03T01:30:00.000+00:00')
        self.assertEqual(self.state()['jobs']['publication']['status'], 'published')
        self.clock.return_value = '2026-10-03T01:16:00+00:00'
        run_tick(self.workspace)
        self.assertEqual(self.tag_collector.call_count, 1)

    def test_tag_only_tick_uses_configured_node_without_core_jobs(self):
        run_tick(self.workspace)
        node = self.home / 'node.exe'
        node.touch()
        config = self.home / '.local/automation.json'
        config.write_bytes(json_bytes(dict(version=1, enabled=True, node_executable=str(node))))
        self.clock.return_value = '2026-10-03T01:16:00+00:00'
        self.tag_plan.return_value = dict(pending=1)
        def collect_tags(*args, **kwargs):
            self.assertEqual(os.environ['MUSIC_NODE_EXE'], str(node))
            return dict(status='complete', attempted=1, remaining=0)
        self.tag_collector.side_effect = collect_tags
        with patch.dict(os.environ):
            os.environ.pop('MUSIC_NODE_EXE', None)
            self.assertEqual(run_tick(self.workspace)['status'], 'ok')
        self.assertEqual(self.collector.call_count, 2)
        self.tag_collector.assert_called_once()

    def test_report_failure_is_isolated_from_existing_jobs_and_retried(self):
        self.reports_collector.side_effect = MusicError('reports unavailable')
        result = run_tick(self.workspace)
        self.assertEqual(result['status'], 'warning')
        self.assertEqual(self.state()['jobs']['reports']['status'], 'failed')
        self.assertEqual(self.state()['jobs']['recent']['status'], 'ok')
        self.assertEqual(self.state()['jobs']['library']['status'], 'ok')
        self.assertEqual(self.state()['jobs']['backup']['status'], 'ok')
        self.assertEqual(self.collector.call_count, 2)
        self.clock.return_value = '2026-10-03T01:31:00+00:00'
        self.reports_collector.side_effect = None
        self.assertEqual(run_tick(self.workspace)['status'], 'ok')
        self.assertEqual(self.collector.call_count, 2)
        self.assertEqual(self.reports_collector.call_count, 2)

    def test_schema_only_upgrade_republishes_even_without_new_observations(self):
        lake = self.workspace.data / 'lake'
        directory = resolve_version(lake)
        manifest = parse_json((directory / 'manifest.json').read_bytes())
        manifest['schema_version'] = '1.0.0'
        manifest['datasets'].pop('platform_reports')
        manifest['datasets'].pop('song_tags', None)
        payload = json_bytes(manifest)
        (directory / 'manifest.json').write_bytes(payload)
        pointer = parse_json((lake / 'current.json').read_bytes())
        pointer['manifest_sha256'] = digest(payload)
        (lake / 'current.json').write_bytes(json_bytes(pointer))
        result = ensure_publication(self.workspace)
        self.assertEqual(result['status'], 'published')
        self.assertEqual(verify_version(resolve_version(lake))['schema_version'], SCHEMA_VERSION)
        self.assertEqual(ensure_publication(self.workspace)['status'], 'unchanged')

    def test_shared_login_pause_marks_reports_stale_once_and_resume_collects_again(self):
        source = self.home / 'reports.json'
        source.write_bytes(json_bytes(report_document()))
        import_reports(self.workspace, source)
        (self.home / '.local/session.json').write_bytes(json_bytes(dict(account_key='42', cookie='OLD_SECRET')))
        self.collector.side_effect = LoginRequired('expired earlier channel')
        with patch('collector_music.collector.bridge', side_effect=AssertionError('pause must not request endpoints')):
            result = run_tick(self.workspace)
        self.assertEqual(result['status'], 'warning')
        self.reports_collector.assert_not_called()
        with closing(self.workspace.connect(readonly=True)) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM platform_reports').fetchone()[0], 8)
            self.assertEqual(dashboard_reports(conn)['monthly'][0]['status'], 'stale')
        self.assertTrue(self.state()['login_pause']['reports_failure_recorded'])
        self.clock.return_value = '2026-10-03T01:15:00+00:00'
        with patch('collector_music.collector.bridge', side_effect=AssertionError('pause must not request endpoints')):
            run_tick(self.workspace)
        with closing(self.workspace.connect(readonly=True)) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM platform_reports').fetchone()[0], 8)
        for path in (self.workspace.raw / 'objects').glob('*'):
            self.assertNotIn(b'OLD_SECRET', path.read_bytes())
        # If a process stopped after import but before persisting the marker,
        # repeating the same pause event must not add another failure snapshot.
        state = self.state()
        state['login_pause'].pop('reports_failure_recorded')
        (self.home / '.local/automation/state.json').write_bytes(json_bytes(state))
        self.clock.return_value = '2026-10-03T01:30:00+00:00'
        run_tick(self.workspace)
        with closing(self.workspace.connect(readonly=True)) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM platform_reports').fetchone()[0], 8)
        (self.home / '.local/session.json').write_bytes(json_bytes(dict(account_key='42', cookie='NEW_SECRET')))
        self.collector.side_effect = self.fake_collect
        def report_recovered(workspace, **kwargs):
            source.write_bytes(json_bytes(report_document(self.clock.return_value)))
            return import_reports(workspace, source)
        self.reports_collector.side_effect = report_recovered
        self.assertEqual(run_tick(self.workspace)['status'], 'ok')
        self.assertNotIn('login_pause', self.state())
        self.assertEqual(self.reports_collector.call_count, 1)
        with closing(self.workspace.connect(readonly=True)) as conn:
            self.assertEqual(dashboard_reports(conn)['monthly'][0]['status'], 'ok')
