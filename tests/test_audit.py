import tempfile
import unittest
from io import StringIO
from pathlib import Path
from contextlib import closing, redirect_stdout

from collector_music.audit import audit
from collector_music.cli import main
from collector_music.common import MusicError, json_bytes
from collector_music.lake import publish
from collector_music.netease import import_netease
from collector_music.pano import import_pano
from collector_music.store import Workspace
from test_pipeline import ROOT, line, pano_row, netease_document


class AuditTests(unittest.TestCase):
    def setUp(self):
        (ROOT/".tmp").mkdir(exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/".tmp")
        self.root=Path(self.temp.name)
        self.workspace=Workspace(self.root/"workspace").initialize()
        self.log=self.root/"pano.jsonl"
        self.log.write_bytes(line(pano_row()))
        import_pano(self.workspace,self.log,device="phone",stream="main")
        source=self.root/"netease.json"
        source.write_bytes(json_bytes(netease_document()))
        import_netease(self.workspace,source)
        publish(self.workspace)

    def tearDown(self):
        self.temp.cleanup()

    def test_full_audit_replays_and_compares_source_facts(self):
        result=audit(self.workspace,recovery_check=True)
        self.assertEqual(result["status"],"ok")
        self.assertTrue(result["publication_current"])
        self.assertEqual(result["replay_fact_checks"]["observations"]["rows"],2)
        self.assertEqual(len(result["published_table_checks"]),9)
        self.assertFalse(list((self.workspace.home/".tmp").iterdir()))

    def test_stale_publication_is_reported_without_claiming_corruption(self):
        self.log.write_bytes(line(pano_row())*2)
        import_pano(self.workspace,self.log,device="phone",stream="main")
        result=audit(self.workspace)
        self.assertEqual(result["status"],"warning")
        self.assertFalse(result["publication_current"])
        with redirect_stdout(StringIO()):
            self.assertEqual(main(["--home",str(self.workspace.home),"audit"]),2)

    def test_delayed_historical_capture_does_not_change_first_event_during_replay(self):
        earlier=netease_document()
        earlier["collected_at_utc"]="2026-10-02T00:45:00Z"
        recent=earlier["scopes"]["recent"]
        earlier["scopes"]={"recent":recent}
        recent["observed_at_utc"]="2026-10-02T00:44:00Z"
        recent["body"]["data"]["list"][0]["data"]["name"]="older metadata"
        path=self.root/"delayed.json"
        path.write_bytes(json_bytes(earlier))
        import_netease(self.workspace,path)
        publish(self.workspace)
        result=audit(self.workspace,recovery_check=True)
        self.assertEqual(result["status"],"ok")
        self.assertEqual(result["replay_fact_checks"]["observations"]["rows"],2)
        self.assertEqual(result["replay_fact_checks"]["observation_evidence"]["rows"],3)

    def test_changed_fact_at_same_revision_is_detected(self):
        with self.workspace.transaction() as conn:
            conn.execute("UPDATE observations SET track='unexpected edit'")
        with self.assertRaisesRegex(MusicError,"SQLite 不一致"):
            audit(self.workspace)

    def test_wrong_stream_prefix_is_detected(self):
        with self.workspace.transaction() as conn:
            conn.execute("UPDATE streams SET prefix_sha256='invalid'")
        with self.assertRaisesRegex(MusicError,"水位/前缀"):
            audit(self.workspace)

    def test_same_labels_at_same_time_remain_distinct_across_devices(self):
        import_pano(self.workspace,self.log,device="tablet",stream="main")
        again=import_pano(self.workspace,self.log,device="tablet",stream="main")
        self.assertEqual(again["inserted_records"],0)
        with closing(self.workspace.connect(readonly=True)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM observations WHERE source='pano'").fetchone()[0],2)
