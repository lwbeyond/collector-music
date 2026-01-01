from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from collector_music.common import MusicError, WorkspaceBusy, json_bytes, parse_json
from scripts import run_scheduled

ROOT = Path(__file__).resolve().parents[1]


class ScheduledEntryTests(unittest.TestCase):
    def test_busy_tick_preserves_the_active_jobs_receipt(self):
        (ROOT / '.tmp').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / '.tmp') as directory:
            home = Path(directory)
            receipt = home / '.local/automation/last-run.json'
            receipt.parent.mkdir(parents=True)
            original = json_bytes(dict(status='ok', checked_at_utc='2026-10-30T00:00:00+00:00'))
            receipt.write_bytes(original)
            with patch.object(run_scheduled, 'PROJECT', home), patch.object(
                    run_scheduled, 'run_tick', side_effect=WorkspaceBusy('busy fixture')):
                self.assertEqual(run_scheduled.main(), 0)
            self.assertEqual(receipt.read_bytes(), original)

    def test_actual_failure_is_still_recorded_and_returns_nonzero(self):
        (ROOT / '.tmp').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / '.tmp') as directory:
            home = Path(directory)
            with patch.object(run_scheduled, 'PROJECT', home), patch.object(
                    run_scheduled, 'run_tick', side_effect=MusicError('failure fixture')):
                self.assertEqual(run_scheduled.main(), 1)
            self.assertEqual(parse_json((home / '.local/automation/last-run.json').read_bytes())['status'], 'failed')
