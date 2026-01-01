from __future__ import annotations

import importlib.util
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import duckdb

from collector_music.cli import execute, parser
from collector_music.common import MusicError, json_bytes
from collector_music.devices import pull_pano, receive_configured
from collector_music.lake import publish, verify_version
from collector_music.maintenance import status, replay
from collector_music.netease import import_netease
from collector_music.pano import import_pano
from collector_music.store import Workspace
from test_pipeline import ROOT, line, pano_row, netease_document


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        self.root = Path(self.temp.name)
        self.workspace = Workspace(self.root / "workspace").initialize()
        self.log = self.workspace.home / "pano.jsonl"
        self.log.write_bytes(line(pano_row()))
        import_pano(self.workspace, self.log, device="phone", stream="main")
        self.publication = publish(self.workspace)
        self.lake = self.workspace.data / "lake"
        # Exercise the documented copyable consumer independently of its source path.
        helper = self.root / "music_snapshot.py"
        shutil.copyfile(ROOT / "integrations/music_snapshot.py", helper)
        spec = importlib.util.spec_from_file_location("copied_music_snapshot", helper)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.Reader = module.MusicSnapshot

    def tearDown(self):
        self.temp.cleanup()

    def test_portable_consumer_pins_generation_and_preserves_other_sources(self):
        copied = self.root / "copied-lake"
        shutil.copytree(self.lake, copied)
        snapshot = self.Reader(copied)
        (copied / "current.json").write_text("{}", "utf-8")
        with duckdb.connect() as conn:
            conn.execute("CREATE TABLE garmin(day VARCHAR)")
            snapshot.attach(conn)
            self.assertEqual(conn.execute("SELECT sum(record_count) FROM music.daily_music_features").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM garmin").fetchone()[0], 0)
            with self.assertRaises(duckdb.Error):
                snapshot.attach(conn)
            self.assertEqual(conn.execute("SELECT count(*) FROM music.observations").fetchone()[0], 1)

    def test_consumer_rolls_back_on_bad_declared_schema(self):
        snapshot = self.Reader(self.lake)
        manifest_path = snapshot.directory / "manifest.json"
        manifest = json.loads(manifest_path.read_text("utf-8"))
        manifest["datasets"]["observations"]["rows"] += 1
        manifest_path.write_bytes(json_bytes(manifest))
        snapshot = self.Reader(self.lake, snapshot.generation)
        with duckdb.connect() as conn:
            with self.assertRaisesRegex(ValueError, "schema or count"):
                snapshot.attach(conn)
            self.assertEqual(conn.execute("SELECT count(*) FROM information_schema.schemata WHERE schema_name='music'").fetchone()[0], 0)

    def test_consumer_rechecks_files_at_attach(self):
        snapshot = self.Reader(self.lake)
        with snapshot.paths["observations"].open("ab") as output:
            output.write(b"changed")
        with duckdb.connect() as conn:
            with self.assertRaisesRegex(ValueError, "Corrupt"):
                snapshot.attach(conn)
            self.assertEqual(conn.execute("SELECT count(*) FROM information_schema.schemata WHERE schema_name='music'").fetchone()[0], 0)

    def test_receipt_pins_identity_hash_and_view_and_rebuilt_library_requires_full_input(self):
        reader = self.Reader(self.lake)
        receipt = reader.input_receipt()
        self.assertEqual(receipt["coverage"], "observed_only")
        self.assertEqual(receipt["precise_netease_ordering"], "unverified")
        pinned = dict(generation=receipt["generation"],
                      expected_manifest_sha256=receipt["manifest_sha256"],
                      expected_database_id=receipt["database_id"],
                      expected_view_version=receipt["analysis_view_version"])
        self.assertEqual(receipt, self.Reader(self.lake, **pinned).input_receipt())
        with self.assertRaisesRegex(ValueError, "checksum differs"):
            self.Reader(self.lake, receipt["generation"], expected_manifest_sha256="0"*64)
        with self.assertRaisesRegex(ValueError, "view version"):
            self.Reader(self.lake, expected_view_version="9.0.0")
        rebuilt = self.root / "synthetic-rebuilt"
        report = replay(self.workspace, rebuilt)
        self.assertEqual(report["rejected"], 0)
        recovered = Workspace(rebuilt)
        publish(recovered)
        with self.assertRaisesRegex(ValueError, "identity changed"):
            self.Reader(recovered.data / "lake", expected_database_id=receipt["database_id"])
        with duckdb.connect() as conn:
            self.Reader(recovered.data / "lake").attach(conn)
            self.assertEqual(conn.execute("SELECT count(*) FROM music.observations").fetchone()[0], 1)

    def test_consumer_rejects_missing_dataset_and_unsafe_generation(self):
        with self.assertRaises(ValueError):
            self.Reader(self.lake, "../outside")
        snapshot = self.Reader(self.lake)
        manifest_path = snapshot.directory / "manifest.json"
        manifest = json.loads(manifest_path.read_text("utf-8"))
        del manifest["datasets"]["coverage"]
        manifest_path.write_bytes(json_bytes(manifest))
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            self.Reader(self.lake, snapshot.generation)

    def test_legacy_publication_remains_readable_without_report_dataset(self):
        snapshot = self.Reader(self.lake)
        manifest = json.loads((snapshot.directory / "manifest.json").read_bytes())
        manifest["schema_version"] = "1.0.0"
        manifest["datasets"].pop("platform_reports")
        manifest["datasets"].pop("song_tags")
        content = json_bytes(manifest)
        (snapshot.directory / "manifest.json").write_bytes(content)
        pointer = json.loads((self.lake / "current.json").read_bytes())
        pointer["manifest_sha256"] = hashlib.sha256(content).hexdigest()
        (self.lake / "current.json").write_bytes(json_bytes(pointer))
        self.assertEqual(verify_version(snapshot.directory)["schema_version"], "1.0.0")
        with duckdb.connect() as conn:
            self.Reader(self.lake).attach(conn)
            self.assertEqual(conn.execute("SELECT count(*) FROM music.pano_listening_events").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema='music' AND table_name='platform_reports'").fetchone()[0], 0)

    def test_new_publication_requires_report_dataset(self):
        snapshot = self.Reader(self.lake)
        manifest_path = snapshot.directory / "manifest.json"
        manifest = json.loads(manifest_path.read_bytes())
        del manifest["datasets"]["platform_reports"]
        manifest_path.write_bytes(json_bytes(manifest))
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            self.Reader(self.lake, snapshot.generation)
        with self.assertRaisesRegex(MusicError, "数据集"):
            verify_version(snapshot.directory)

    def test_analysis_views_select_one_snapshot_and_keep_failed_scope_stale(self):
        document = netease_document()
        song = document["scopes"]["recent"]["body"]["data"]["list"][0]["data"]
        song["pop"] = 85
        document["scopes"]["likes"]["body"].update(songs=[song],detail_status="ok")
        document["scopes"]["playlists"]["body"]["playlist"][0].update(subscribed=True,trackCount=77)
        source = self.root / "netease.json"
        source.write_bytes(json_bytes(document)); import_netease(self.workspace,source)
        # A later failed scope must not erase the last complete relationship set.
        document["collected_at_utc"]="2026-10-03T01:00:00Z"
        document["scopes"]={"likes":{"status":"failed","observed_at_utc":"2026-10-03T01:00:00Z","error":"fixture"}}
        source.write_bytes(json_bytes(document)); import_netease(self.workspace,source)
        publish(self.workspace)
        with duckdb.connect() as conn:
            snapshot=self.Reader(self.lake).attach(conn)
            row=conn.execute("SELECT platform_track_id,artist_names,track_duration_ms,reported_popularity,metadata_status,snapshot_stale FROM music.current_likes").fetchone()
            self.assertEqual(row,("123",["艺人"],210000,85.0,"ok",True))
            self.assertEqual(conn.execute("SELECT relationship,reported_track_count,membership_status FROM music.current_playlists").fetchone(),("subscribed",77,"not_collected"))
            self.assertEqual(conn.execute("SELECT count(*) FROM music.pano_listening_events").fetchone()[0],1)
            self.assertEqual(conn.execute("SELECT count(*) FROM music.netease_recent_observations").fetchone()[0],1)
            self.assertEqual(conn.execute("SELECT count(*) FROM music.ranking_history").fetchone()[0],2)
            self.assertEqual(snapshot.analysis_view_version,"1.2.0")
        document["collected_at_utc"]="2026-10-04T01:00:00Z"
        document["scopes"]={"likes":{"status":"ok","observed_at_utc":"2026-10-04T01:00:00Z","body":{"code":200,"ids":[]}}}
        source.write_bytes(json_bytes(document)); import_netease(self.workspace,source)
        publish(self.workspace)
        with duckdb.connect() as conn:
            self.Reader(self.lake).attach(conn)
            self.assertEqual(conn.execute("SELECT count(*) FROM music.current_likes").fetchone()[0],0)

    def test_consumer_rejects_unknown_metric_version(self):
        snapshot=self.Reader(self.lake)
        path=snapshot.directory/"manifest.json"
        doc=json.loads(path.read_bytes());doc["metric_version"]="9.0.0";path.write_bytes(json_bytes(doc))
        with self.assertRaisesRegex(ValueError,"metric contract"):
            self.Reader(self.lake,snapshot.generation)
        with self.assertRaisesRegex(MusicError,"指标契约"):
            verify_version(snapshot.directory)

    def history_capture(self, day, ids, account="123456", enriched=False, playlists=None):
        document = netease_document()
        document["account_key"] = account
        document["collected_at_utc"] = f"2026-10-{day:02d}T01:00:00Z"
        scope = dict(status="failed" if ids is None else "ok",
                     observed_at_utc=f"2026-10-{day:02d}T00:59:00Z")
        if ids is not None:
            scope["body"] = dict(code=200, ids=ids)
            if enriched:
                scope["body"].update(detail_status="ok", songs=[dict(id=i, name=f"song-{i}") for i in ids])
        document["scopes"] = {"likes": scope}
        if playlists is not None:
            document["scopes"]["playlists"] = dict(status="ok", observed_at_utc=scope["observed_at_utc"],
                body=dict(code=200, more=False, playlist=[dict(id=i, name=f"list-{i}") for i in playlists]))
        source = self.root / "history.json"
        source.write_bytes(json_bytes(document))
        return import_netease(self.workspace, source)

    def test_relationship_history_orders_capture_time_and_isolates_accounts_scopes(self):
        # Delayed files arrive out of order; metadata-only refresh is not an addition.
        self.history_capture(5, [2, 3], playlists=[])
        self.history_capture(2, [1, 2], playlists=[1])
        self.history_capture(4, None)
        self.history_capture(3, [1, 2], enriched=True)
        self.history_capture(6, [])
        self.history_capture(7, [3])
        self.history_capture(2, [999], account="654321")
        publish(self.workspace)
        with duckdb.connect() as conn:
            self.Reader(self.lake).attach(conn)
            changes = conn.execute("""SELECT entity_id,change_kind,interval_start_utc,interval_end_utc
                FROM music.library_changes WHERE account_key='123456' AND scope='likes'
                ORDER BY interval_end_utc,change_kind,entity_id""").fetchall()
            stamp = lambda day, minute: f"2026-10-{day:02d}T{minute}:00.000+00:00"
            self.assertEqual(changes, [
                ("1", "baseline", None, stamp(2,"01:00")),
                ("2", "baseline", None, stamp(2,"01:00")),
                ("3", "observed_added", stamp(3,"00:59"), stamp(5,"01:00")),
                ("1", "observed_removed", stamp(3,"00:59"), stamp(5,"01:00")),
                ("2", "observed_removed", stamp(5,"00:59"), stamp(6,"01:00")),
                ("3", "observed_removed", stamp(5,"00:59"), stamp(6,"01:00")),
                ("3", "observed_added", stamp(6,"00:59"), stamp(7,"01:00")),
            ])
            self.assertEqual(conn.execute("SELECT count(*) FROM music.library_snapshots WHERE NOT relationship_complete").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT change_kind FROM music.library_changes WHERE account_key='654321'").fetchall(), [("baseline",)])
            self.assertEqual(conn.execute("SELECT change_kind FROM music.library_changes WHERE scope='playlists' ORDER BY interval_end_utc").fetchall(), [("baseline",),("observed_removed",)])

    def test_as_of_excludes_incomplete_future_capture_and_preserves_historical_metadata(self):
        self.history_capture(2, [1])
        first_generation = publish(self.workspace)["generation"]
        self.history_capture(3, [1], enriched=True)
        self.history_capture(4, None)
        self.history_capture(5, [])
        publish(self.workspace)
        with duckdb.connect() as conn:
            # 08:59:30 +08:00 is after scope start but before the whole capture completes.
            snapshot = self.Reader(self.lake, as_of="2026-10-03T08:59:30+08:00").attach(conn)
            self.assertEqual(snapshot.as_of, "2026-10-03T00:59:30.000+00:00")
            self.assertEqual(conn.execute("SELECT platform_track_id,track,snapshot_stale FROM music.current_likes").fetchall(), [("1",None,False)])
            self.assertEqual(conn.execute("SELECT count(*) FROM music.library_membership_history").fetchone()[0], 1)
            # Base tables always retain the complete pinned generation.
            self.assertEqual(conn.execute("SELECT count(*) FROM music.library_items").fetchone()[0], 2)
            self.Reader(self.lake, as_of="2026-10-04T01:00:00Z").attach(conn,"failed")
            self.assertEqual(conn.execute("SELECT track,snapshot_stale FROM failed.current_likes").fetchall(), [("song-1",True)])
            self.Reader(self.lake, as_of="2026-10-05T01:00:00Z").attach(conn,"empty")
            self.assertEqual(conn.execute("SELECT count(*) FROM empty.current_likes").fetchone()[0], 0)
            self.Reader(self.lake, generation=first_generation).attach(conn,"original")
            self.assertEqual(conn.execute("SELECT track FROM original.current_likes").fetchall(), [(None,)])

    def test_as_of_requires_timezone_and_does_not_backdate_device_receipt(self):
        with self.assertRaisesRegex(ValueError, "timezone"):
            self.Reader(self.lake, as_of="2026-10-03T00:00:00")
        with duckdb.connect() as conn:
            self.Reader(self.lake, as_of="2026-10-01T17:00:00Z").attach(conn)
            # The event is old, but was received later; the two time filters differ.
            self.assertEqual(conn.execute("SELECT count(*) FROM music.pano_listening_events").fetchone()[0], 0)
            self.Reader(self.lake).attach(conn,"latest")
            self.assertEqual(conn.execute("SELECT count(*) FROM latest.pano_listening_events WHERE event_time_utc < TIMESTAMPTZ '2026-10-01 17:00:00+00'").fetchone()[0], 1)

    def fake_adb(self, executable, args, timeout=30):
        if args[2:4] == ["shell", "getprop"]:
            return b"TABLET-MODEL\n"
        self.assertEqual(args[2], "pull")
        Path(args[-1]).write_bytes(line(pano_row()))
        return b"1 file pulled"

    def test_usb_pull_binds_device_and_repeat_import_is_idempotent(self):
        options = dict(serial="fixture-private-serial", device="tablet", stream="main",
                       remote="/sdcard/Download/music/pano.jsonl", collector_version="4.46",
                       rule_note="fixture: half or four minutes")
        with patch("collector_music.devices.adb_path", return_value="adb"), patch("collector_music.devices.run_adb", side_effect=self.fake_adb):
            first = pull_pano(self.workspace, **options)
            second = pull_pano(self.workspace, **options)
            with self.assertRaisesRegex(MusicError, "绑定另一台"):
                pull_pano(self.workspace, **{**options, "serial": "different-serial"})
        self.assertEqual(first["inserted_records"], 1)
        self.assertEqual(second["status"], "duplicate")
        manifest, _ = self.workspace.read_capture(self.workspace.raw / first["raw_ref"])
        self.assertEqual(manifest["settings"]["collector_version"], "4.46")
        self.assertEqual(manifest["settings"]["rule_note"], options["rule_note"])
        receipts = list((self.workspace.home / ".local/usb-receipts").glob("*.receipt.json"))
        self.assertEqual(len(receipts), 2)
        self.assertNotIn(options["serial"], receipts[0].read_text("utf-8"))
        self.assertEqual(sum(row["records"] for row in status(self.workspace)["observations"]), 2)

    def test_configured_receive_requires_exact_bound_connected_device(self):
        config = self.root / "phone.json"
        config.write_bytes(json_bytes(dict(version=1, device_key="tablet", stream_name="main",
            remote="/sdcard/Download/music/pano.jsonl", collector_version="4.46")))
        with self.assertRaisesRegex(MusicError, "尚未绑定"):
            receive_configured(self.workspace, config)
        with patch("collector_music.devices.adb_path", return_value="adb"), patch("collector_music.devices.run_adb", side_effect=self.fake_adb):
            pull_pano(self.workspace, serial="expected", device="tablet", stream="main",
                remote="/sdcard/Download/music/pano.jsonl", collector_version="4.46")
        def fake_listing(executable, args, timeout=30):
            if args == ["devices", "-l"]:
                return b"List of devices attached\nother device\nexpected device model:TABLET-MODEL\n"
            self.assertEqual(args[:2], ["-s", "expected"])
            return self.fake_adb(executable, args, timeout)
        with patch("collector_music.devices.adb_path", return_value="adb"), patch("collector_music.devices.run_adb", side_effect=fake_listing):
            self.assertEqual(receive_configured(self.workspace, config)["status"], "duplicate")
        with patch("collector_music.devices.adb_path", return_value="adb"), patch("collector_music.devices.run_adb", return_value=b"List of devices attached\nother device\nexpected unauthorized\n") as adb:
            with self.assertRaisesRegex(MusicError, "绑定设备"):
                receive_configured(self.workspace, config)
            adb.assert_called_once()

    def test_usb_path_rejected_before_adb(self):
        with patch("collector_music.devices.run_adb") as adb:
            for remote in ("/sdcard/../private.jsonl", "/sdcard/one;two.jsonl", "/data/private.jsonl"):
                with self.assertRaises(MusicError):
                    pull_pano(self.workspace, serial="device", device="phone", stream="main", remote=remote)
            adb.assert_not_called()

    def scan(self, logs):
        config = self.root / "config.json"
        config.write_bytes(json_bytes(dict(version=1, pano_logs=logs)))
        return execute(parser().parse_args(["--home", str(self.workspace.home), "scan", "--config", str(config)]))

    def test_scan_publishes_idempotently_and_failed_source_keeps_previous_publication(self):
        logs = [dict(path="pano.jsonl", device_key="phone", stream_name="main")]
        result = self.scan(logs)
        self.assertEqual(result["imports"][0]["status"], "duplicate")
        pointer = (self.lake / "current.json").read_bytes()
        logs.append(dict(path="missing.jsonl", device_key="tablet", stream_name="main"))
        with self.assertRaises(FileNotFoundError):
            self.scan(logs)
        self.assertEqual((self.lake / "current.json").read_bytes(), pointer)
