from __future__ import annotations

import copy
import json
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from collector_music.common import MusicError, json_bytes, parse_json
from collector_music.lake import create_table, publish, query, resolve_version, verify_version
from collector_music.maintenance import backup, replay, status, verify
from collector_music.netease import import_netease
from collector_music.pano import import_pano
from collector_music.store import Workspace

ROOT = Path(__file__).resolve().parents[1]
TIMESTAMP = int(datetime(2026, 10, 1, 16, 30, tzinfo=timezone.utc).timestamp() * 1000)


class PublicationBatchTests(unittest.TestCase):
    def setUp(self):
        (ROOT / '.tmp').mkdir(exist_ok=True)

    def test_bulk_materialization_preserves_exact_values_across_batch_boundaries(self):
        import duckdb
        values = [None, '', '引号"和逗号,与换行\n', '{"嵌套":"原始 JSON"}', '\\N']
        rows = [(index, values[index % len(values)], None if index % 3 == 0 else index / 8,
                 9223372036854775807 - index) for index in range(1001)]
        schema = [('id', 'BIGINT'), ('text', 'VARCHAR'), ('value', 'DOUBLE'), ('large', 'BIGINT')]
        with tempfile.TemporaryDirectory(dir=ROOT / '.tmp') as staging, duckdb.connect() as connection:
            create_table(connection, 'sample', schema, rows, staging_directory=staging)
            self.assertEqual(connection.execute('SELECT * FROM sample ORDER BY id').fetchall(), rows)
            create_table(connection, 'empty_sample', schema, [], staging_directory=staging)
            self.assertEqual(connection.execute('SELECT count(*) FROM empty_sample').fetchone()[0], 0)
            self.assertEqual(list(Path(staging).iterdir()), [])

    def test_invalid_row_width_fails_without_partial_materialization(self):
        import duckdb
        with tempfile.TemporaryDirectory(dir=ROOT / '.tmp') as staging, duckdb.connect() as connection:
            with self.assertRaises(MusicError):
                create_table(connection, 'sample', [('id', 'BIGINT'), ('text', 'VARCHAR')],
                             [(1, 'valid'), (2,)], staging_directory=staging)
            self.assertEqual(connection.execute('SELECT count(*) FROM sample').fetchone()[0], 0)
            self.assertEqual(list(Path(staging).iterdir()), [])


def pano_row(**overrides):
    return dict(timeHuman="2026-10-02 00:30", timeMs=TIMESTAMP, artist="测试歌手",
                track="同一首测试歌", album="测试专辑", albumArtist=None, durationMs=180000,
                mediaPlayerPackage="com.netease.cloudmusic", mediaPlayerName="网易云音乐",
                mediaPlayerVersion="fixture", event="scrobble", **overrides)


def line(row):
    return (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")


def netease_document():
    song = dict(id=123, name="测试歌曲", ar=[dict(id=456, name="艺人")], al=dict(id=789, name="专辑"), dt=210000)
    bodies = {
        "recent": {"code": 200, "data": {"list": [{"playTime": TIMESTAMP, "data": song}], "total": 1}},
        "week": {"code": 200, "weekData": [{"song": song, "playCount": 3, "score": 100}]},
        "all": {"code": 200, "allData": [{"song": song, "playCount": 30, "score": 90}]},
        "likes": {"code": 200, "ids": [123]},
        "playlists": {"code": 200, "playlist": [{"id": 999, "name": "测试歌单"}], "more": False},
    }
    return dict(format="collector-music-netease", version=1, account_key="123456",
        adapter_version="4.40.1", collected_at_utc="2026-10-02T01:00:00Z",
        scopes={name: dict(status="ok", observed_at_utc="2026-10-02T00:59:00Z", body=body) for name,body in bodies.items()})


class PipelineTests(unittest.TestCase):
    def setUp(self):
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="test-", dir=ROOT / ".tmp")
        self.root = Path(self.temp.name)
        self.workspace = Workspace(self.root / "workspace").initialize()
        self.log = self.root / "pano.jsonl"

    def tearDown(self):
        self.temp.cleanup()

    def ingest(self, payload=None, **options):
        if payload is not None:
            self.log.write_bytes(payload)
        defaults = dict(device="phone", stream="main-001")
        defaults.update(options)
        return import_pano(self.workspace, self.log, **defaults)

    def sql(self, statement, params=()):
        with closing(self.workspace.connect(readonly=True)) as conn:
            return [dict(r) for r in conn.execute(statement, params)]

    def ncm(self, document=None, person="me"):
        path = self.root / "netease.json"
        path.write_bytes(json_bytes(document or netease_document()))
        return import_netease(self.workspace, path, person=person)

    def test_initialize_preserves_database_identity(self):
        first = status(self.workspace)
        self.workspace.initialize()
        self.assertEqual(first["database_id"], status(self.workspace)["database_id"])
        self.assertEqual(first["journal_mode"], "delete")

    def test_duplicate_import_has_no_new_records_or_changes(self):
        first = self.ingest(line(pano_row()))
        revision = status(self.workspace)["data_revision"]
        result = self.ingest()
        self.assertEqual(result["status"], "duplicate")
        self.assertEqual(first["import_id"], result["import_id"])
        self.assertEqual(status(self.workspace)["data_revision"], revision)

    def test_identical_play_lines_remain_distinct(self):
        payload = line(pano_row())
        self.ingest(payload)
        result = self.ingest(payload + payload)
        self.assertEqual(result["inserted_records"], 1)
        self.assertEqual(len(self.sql("SELECT * FROM observations")), 2)

    def test_pending_tail_completes_without_reimporting_prefix(self):
        first, second = line(pano_row()), line({**pano_row(), "timeMs": TIMESTAMP + 600000})
        result = self.ingest(first + second[:31])
        self.assertEqual(result["pending_bytes"], 31)
        self.assertEqual(result["inserted_records"], 1)
        result = self.ingest(first + second)
        self.assertEqual(result["inserted_records"], 1)
        self.assertEqual(result["pending_bytes"], 0)

    def test_unterminated_first_line_is_not_committed(self):
        result = self.ingest(line(pano_row()).rstrip(b"\n"))
        self.assertEqual(result["inserted_records"], 0)
        self.assertEqual(self.sql("SELECT committed_bytes FROM streams")[0]["committed_bytes"], 0)

    def test_malformed_new_line_rolls_back_entire_batch_and_keeps_raw(self):
        self.ingest(line(pano_row()))
        revision = status(self.workspace)["data_revision"]
        with self.assertRaises(MusicError):
            self.ingest(line(pano_row()) + line(pano_row()) + b'{"broken":}\n')
        self.assertEqual(len(self.sql("SELECT * FROM observations")), 1)
        self.assertEqual(status(self.workspace)["data_revision"], revision)
        self.assertEqual(len(list((self.workspace.raw / "captures").glob("*.json"))), 2)

    def test_modified_prefix_is_rejected(self):
        self.ingest(line(pano_row()))
        with self.assertRaises(MusicError):
            self.ingest(line({**pano_row(), "track": "另一首歌"}))
        self.assertEqual(self.sql("SELECT track FROM observations")[0]["track"], "同一首测试歌")

    def test_unseen_old_prefix_does_not_roll_back_state(self):
        content = line(pano_row())
        self.ingest(content * 3)
        result = self.ingest(content * 2)
        self.assertEqual(result["status"], "older_prefix")
        self.assertEqual(self.sql("SELECT committed_bytes FROM streams")[0]["committed_bytes"], len(content)*3)

    def test_device_and_stream_namespaces_are_independent(self):
        self.ingest(line(pano_row()))
        self.ingest(device="tablet")
        self.assertEqual(len(self.sql("SELECT * FROM observations")), 2)

    def test_unknown_event_type_is_not_silently_skipped(self):
        with self.assertRaises(MusicError):
            self.ingest(line({**pano_row(), "event": "future-event"}))
        self.assertFalse(self.sql("SELECT * FROM streams"))

    def test_duplicate_json_key_is_rejected(self):
        with self.assertRaises(MusicError):
            self.ingest(line(pano_row()).replace(b'"timeMs":', b'"timeMs": 0, "timeMs":'))

    def test_invalid_timestamp_and_duration_are_rejected(self):
        for field, value in [("timeMs", True), ("timeMs", None), ("timeMs", -1), ("durationMs", -1), ("durationMs", 1.5)]:
            with self.subTest(field=field, value=value), self.assertRaises(MusicError):
                self.ingest(line({**pano_row(), field: value}))

    def test_bom_and_crlf_preserve_byte_offsets(self):
        first = b"\xef\xbb\xbf" + line(pano_row()).replace(b"\n", b"\r\n")
        self.ingest(first)
        self.ingest(first + line(pano_row()))
        self.assertEqual(len(self.sql("SELECT * FROM observations")), 2)
        self.assertIn(f"byte:{len(first)}", [r["raw_pointer"] for r in self.sql("SELECT raw_pointer FROM observations")])

    def test_duration_is_never_listened_seconds(self):
        self.ingest(line(pano_row()))
        row = self.sql("SELECT * FROM observations")[0]
        self.assertEqual(row["track_duration_ms"], 180000)
        self.assertIsNone(row["actual_listened_seconds"])
        self.assertIsNone(row["source_timezone"])

    def test_netease_scopes_are_separate(self):
        self.ncm()
        self.assertEqual(len(self.sql("SELECT * FROM observations")), 1)
        self.assertEqual(len(self.sql("SELECT * FROM ranking_items")), 2)
        self.assertEqual(len(self.sql("SELECT * FROM library_items")), 2)
        self.assertEqual(len(self.sql("SELECT * FROM coverage")), 5)

    def test_netease_overlap_preserves_multiple_evidence(self):
        document = netease_document()
        self.ncm(document)
        document["collected_at_utc"] = "2026-10-02T02:00:00Z"
        self.ncm(document)
        self.assertEqual(len(self.sql("SELECT * FROM observations")), 1)
        self.assertEqual(len(self.sql("SELECT * FROM observation_evidence")), 2)
        self.assertEqual(len(self.sql("SELECT * FROM ranking_items")), 4)

    def test_account_binding_cannot_silently_change(self):
        document = netease_document()
        self.ncm(document)
        document["collected_at_utc"] = "2026-10-02T02:00:00Z"
        with self.assertRaises(MusicError):
            self.ncm(document, person="different-person")
        self.assertEqual(len(self.sql("SELECT * FROM imports")), 1)

    def test_failed_scope_is_unknown_and_does_not_erase_library(self):
        self.ncm()
        document = netease_document()
        document["collected_at_utc"] = "2026-10-03T01:00:00Z"
        document["scopes"] = {"likes": {"status": "failed", "observed_at_utc": "2026-10-03T01:00:00Z", "body": {"code": 301}}}
        result = self.ncm(document)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(self.sql("SELECT * FROM library_items WHERE scope='likes'")), 1)
        self.assertEqual(self.sql("SELECT status FROM coverage WHERE transport_status='failed'")[0]["status"], "unavailable")

    def test_malformed_success_response_rejects_entire_snapshot(self):
        document = netease_document()
        del document["scopes"]["all"]["body"]["allData"]
        with self.assertRaises(MusicError):
            self.ncm(document)
        self.assertFalse(self.sql("SELECT * FROM observations"))
        self.assertFalse(self.sql("SELECT * FROM source_accounts"))

    def test_credentials_never_enter_raw_archive(self):
        document = netease_document()
        document["scopes"]["recent"]["body"]["cookie"] = "PRIVATE"
        with self.assertRaises(MusicError):
            self.ncm(document)
        self.assertFalse(list(self.workspace.raw.glob("objects/*")))

    def test_playlist_missing_page_terminal_rejected(self):
        document = netease_document()
        document["scopes"]["playlists"]["body"]["more"] = True
        with self.assertRaises(MusicError):
            self.ncm(document)

    def test_duplicate_library_id_rejected(self):
        document = netease_document()
        document["scopes"]["likes"]["body"]["ids"] = [123,123]
        with self.assertRaises(MusicError):
            self.ncm(document)

    def test_incomplete_or_foreign_details_cannot_claim_complete(self):
        for songs in ([], [dict(id=999, name="Another song", ar=[], al=None)]):
            with self.subTest(songs=songs):
                document = netease_document()
                document["scopes"]["likes"]["body"].update(songs=songs, detail_status="ok")
                with self.assertRaises(MusicError):
                    self.ncm(document)
                self.assertFalse(self.sql("SELECT * FROM library_items"))

    def test_missing_recent_time_stays_unknown(self):
        document = netease_document()
        del document["scopes"]["recent"]["body"]["data"]["list"][0]["playTime"]
        self.ncm(document)
        self.assertIsNone(self.sql("SELECT occurred_at_utc FROM observations")[0]["occurred_at_utc"])
        publish(self.workspace)
        self.assertEqual(query(self.workspace.data/"lake", "SELECT * FROM daily_music_features")["items"], [])

    def test_pano_and_api_are_not_merged(self):
        self.ingest(line(pano_row()))
        self.ncm()
        publish(self.workspace)
        rows = query(self.workspace.data/"lake", "SELECT source,record_count FROM daily_music_features")["items"]
        self.assertEqual({r["source"] for r in rows}, {"pano", "netease_recent"})

    def test_daily_boundary_and_love_exclusion(self):
        self.ingest(line(pano_row()) + line({**pano_row(), "event": "love"}))
        publish(self.workspace)
        rows = query(self.workspace.data/"lake", "SELECT * FROM daily_music_features")["items"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["date"], "2026-10-02")
        self.assertEqual(rows[0]["record_count"], 1)
        self.assertEqual(rows[0]["night_record_count"], 1)
        self.assertIsNone(rows[0]["actual_listened_seconds"])

    def test_empty_snapshot_has_typed_parquet(self):
        published = publish(self.workspace)
        self.assertEqual(published["datasets"]["observations"], 0)
        verify_version(resolve_version(self.workspace.data/"lake"))

    def test_published_lake_is_portable_without_database(self):
        self.ingest(line(pano_row()))
        publish(self.workspace)
        standalone = self.root / "portable-lake"
        shutil.copytree(self.workspace.data/"lake", standalone)
        rows = query(standalone, "SELECT track FROM observations")["items"]
        self.assertEqual(rows[0]["track"], "同一首测试歌")

    def test_fixed_generation_does_not_change_after_new_publish(self):
        self.ingest(line(pano_row()))
        old = publish(self.workspace)
        self.ingest(line(pano_row()) * 2)
        publish(self.workspace)
        rows = query(self.workspace.data/"lake", "SELECT count(*) AS n FROM observations", old["generation"])["items"]
        self.assertEqual(rows[0]["n"], 1)

    def test_failed_publication_keeps_previous_pointer(self):
        publish(self.workspace)
        pointer = (self.workspace.data/"lake/current.json").read_bytes()
        with patch("collector_music.lake.verify_version", side_effect=MusicError("injected read-back failure")):
            with self.assertRaises(MusicError):
                publish(self.workspace)
        self.assertEqual((self.workspace.data/"lake/current.json").read_bytes(), pointer)

    def test_query_rejects_mutations_and_multiple_statements(self):
        publish(self.workspace)
        for sql in ["DELETE FROM observations", "SELECT 1; SELECT 2", "COPY observations TO 'bad.csv'", "SELECT 1 AS a,2 AS a"]:
            with self.subTest(sql=sql), self.assertRaises(MusicError):
                query(self.workspace.data/"lake", sql)

    def test_query_external_file_access_disabled(self):
        publish(self.workspace)
        import duckdb
        with self.assertRaises(duckdb.Error):
            query(self.workspace.data/"lake", "SELECT * FROM read_csv('C:/private.csv')")

    def test_corrupted_parquet_detected(self):
        publish(self.workspace)
        directory = resolve_version(self.workspace.data/"lake")
        (directory/"observations.parquet").write_bytes(b"corrupt")
        with self.assertRaises(MusicError):
            query(self.workspace.data/"lake", "SELECT 1")

    def test_raw_tampering_detected(self):
        self.ingest(line(pano_row()))
        path = next((self.workspace.raw/"objects").glob("*.bin"))
        path.write_bytes(b"corrupt")
        with self.assertRaises(MusicError):
            verify(self.workspace)

    def test_backup_is_consistent_and_does_not_overwrite(self):
        self.ingest(line(pano_row()))
        target = self.root / "backup.sqlite"
        backup(self.workspace, target)
        with closing(sqlite3.connect(target)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM observations").fetchone()[0], 1)
        with self.assertRaises(FileExistsError):
            backup(self.workspace, target)

    def test_raw_replay_recovers_facts_without_old_database(self):
        self.ingest(line(pano_row()))
        self.ingest(line(pano_row()) * 2)
        self.ncm()
        destination = self.root / "rebuilt"
        result = replay(self.workspace, destination)
        self.assertEqual(result["rejected"], 0)
        with closing(Workspace(destination).connect(readonly=True)) as conn:
            restored = [tuple(row) for row in conn.execute("SELECT id,source,track,occurred_at_utc FROM observations ORDER BY id")]
        with closing(self.workspace.connect(readonly=True)) as conn:
            original = [tuple(row) for row in conn.execute("SELECT id,source,track,occurred_at_utc FROM observations ORDER BY id")]
        self.assertEqual(restored, original)
        with self.assertRaises(MusicError):
            replay(self.workspace, destination)

    def test_replay_reports_rejected_archive_instead_of_claiming_success(self):
        self.ingest(line(pano_row()))
        with self.assertRaises(MusicError):
            self.ingest(line(pano_row()) + b'{"invalid":true}\n')
        result = replay(self.workspace, self.root/"rebuilt")
        self.assertEqual(result["rejected"], 1)

    def test_lock_prevents_overlapping_publish(self):
        with self.workspace.lock("publish"):
            with self.assertRaises(MusicError):
                publish(self.workspace)


if __name__ == "__main__":
    unittest.main()
