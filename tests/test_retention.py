import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from collector_music.lake import publish, resolve_version
from collector_music.store import Workspace


class RetentionTests(unittest.TestCase):
    def test_only_successful_publication_reclaims_retired_copies(self):
        directory = Path(__file__).resolve().parents[1] / '.tmp'
        directory.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=directory) as temporary:
            workspace = Workspace(temporary).initialize()
            publish(workspace)
            first = resolve_version(workspace.data / 'lake')
            publish(workspace)
            second = resolve_version(workspace.data / 'lake')
            for path in [first, *first.rglob('*')]:
                os.utime(path, (1, 1))
            with patch('collector_music.lake.verify_version', side_effect=ValueError('invalid')):
                with self.assertRaises(ValueError):
                    publish(workspace)
            self.assertTrue(first.exists())
            self.assertEqual(resolve_version(workspace.data / 'lake'), second)
            publish(workspace)
            self.assertFalse(first.exists())
            self.assertTrue(second.exists())  # retired-reader grace period
            self.assertTrue(Path(workspace.db).is_file())
