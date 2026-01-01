from contextlib import closing
from datetime import datetime
from pathlib import Path
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import duckdb

from collector_music.collector import collect_reports
from collector_music.audit import audit
from collector_music.common import LoginRequired, MusicError, digest, json_bytes, parse_json
from collector_music.lake import publish, resolve_version, verify_version
from collector_music.platform_reports import dashboard_reports, import_reports, normalize_report
from collector_music.store import Workspace

ROOT = Path(__file__).resolve().parents[1]


def ms(value):
    return int(datetime.fromisoformat(value).timestamp() * 1000)


def monthly_body(month="09", duration=120):
    return dict(code=200, data=dict(type="month",
        startTime=ms(f"2026-{month}-01T00:00:00+08:00"),
        endTime=ms(f"2026-{month}-30T00:00:00+08:00"),
        listenTimeDistributionBlock=dict(playDuration=duration, listenDays=1,
            durationDetails=[dict(period=f"2026-{month}-01", duration=duration, reachLimit=False)]),
        topStyleBlock=dict(sections=[dict(genreName="Pop", percent="60")]),
        topLanguageBlock=dict(sections=[dict(language="English", percent="100", playSongNum=5)]),
        topAgeBlock=dict(sections=[dict(age="2020s", playSongNum=5)]),
        topSongBlock=dict(songId=100, songName="A song", artists=[dict(artistName="Artist")], playCount=2),
        topArtistBlock=dict(artisId=200, artistName="Artist", playCount=2)))


def report_document(observed="2026-10-01T01:00:00+00:00", reports=None):
    reports = reports if reports is not None else {
        "monthly": dict(status="ok", observed_at_utc=observed, body=monthly_body()),
        "realtime": dict(status="ok", observed_at_utc=observed, body=monthly_body("10", 5)),
        "preference": dict(status="ok", observed_at_utc=observed,
            body=dict(code=200, data=dict(tagPreferenceVos=[dict(tagName="Pop", ratio="53")]))),
        "total": dict(status="ok", observed_at_utc=observed, body=dict(code=200, data=dict(totalDuration=5000))),
    }
    return dict(format="collector-music-netease-reports", version=1, adapter_version="4.40.1",
        account_key="42", collected_at_utc=observed, reports=reports)


class PlatformReportTests(unittest.TestCase):
    def setUp(self):
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        self.workspace = Workspace(self.temp.name).initialize()
        self.path = self.workspace.home / "reports.json"

    def tearDown(self):
        self.temp.cleanup()

    def load(self, document=None):
        self.path.write_bytes(json_bytes(document or report_document()))
        return import_reports(self.workspace, self.path)

    def dashboard(self):
        with closing(self.workspace.connect(readonly=True)) as conn:
            return dashboard_reports(conn)

    def test_import_is_idempotent_preserves_distinct_periods_and_does_not_create_plays(self):
        self.assertEqual(self.load()["inserted_records"], 4)
        self.assertEqual(self.load()["status"], "duplicate")
        with closing(self.workspace.connect(readonly=True)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM platform_reports").fetchone()[0], 4)
            self.assertEqual(conn.execute("SELECT count(*) FROM observations").fetchone()[0], 0)
        result = self.dashboard()
        self.assertEqual(result["monthly"][0]["period_label"], "2026-09")
        self.assertEqual(result["realtime"]["period_label"], "2026-10")
        self.assertEqual(result["monthly"][0]["metrics"]["duration_raw"], 120)
        self.assertIsNone(result["monthly"][0]["metrics"]["duration_minutes"])
        self.assertEqual(result["monthly"][0]["metrics"]["duration_unit"], "unknown")
        self.assertEqual(result["preference"]["tags"], [dict(name="Pop", percent=53.0)])
        for forbidden in ("account_key", "person_key", "raw_ref", "payload_json", "cookie"):
            self.assertNotIn(forbidden, json.dumps(result))

    def test_failed_report_preserves_last_success_as_stale_and_empty_supersedes_it(self):
        self.load()
        observed = "2026-10-02T01:00:00+00:00"
        failed = report_document(observed, {"preference": dict(status="failed", observed_at_utc=observed, error="offline")})
        self.assertEqual(self.load(failed)["status"], "partial")
        self.assertEqual(self.dashboard()["preference"]["status"], "stale")
        self.assertEqual(len(self.dashboard()["preference"]["tags"]), 1)
        observed = "2026-10-03T01:00:00+00:00"
        empty = report_document(observed, {"preference": dict(status="ok", observed_at_utc=observed,
            body=dict(code=200, data=dict(tagPreferenceVos=[])))})
        self.load(empty)
        self.assertEqual(self.dashboard()["preference"]["status"], "empty")
        self.assertEqual(self.dashboard()["preference"]["tags"], [])

    def test_malformed_report_is_archived_as_failed_without_period_or_fabricated_values(self):
        body = monthly_body()
        body["data"]["listenTimeDistributionBlock"]["durationDetails"].append(dict(period="2026-10-01", duration=3))
        observed = "2026-10-01T01:00:00+00:00"
        result = self.load(report_document(reports={"monthly": dict(status="ok", observed_at_utc=observed, body=body)}))
        self.assertEqual(result["reports"]["monthly"], "failed")
        with closing(self.workspace.connect(readonly=True)) as conn:
            row = conn.execute("SELECT * FROM platform_reports").fetchone()
            self.assertIsNone(row["period_start_utc"])
            self.assertIn("2026-10-01", row["payload_json"])
        self.assertEqual(self.dashboard()["status"], "unavailable")

    def test_missing_data_or_listen_blocks_preserve_previous_report_as_stale(self):
        self.load()
        without_blocks = monthly_body()
        without_blocks["data"].pop("listenTimeDistributionBlock")
        for index, body in enumerate((dict(code=200), without_blocks), start=2):
            with self.subTest(body=body):
                observed = f"2026-10-0{index}T01:00:00+00:00"
                result = self.load(report_document(observed, {"monthly": dict(
                    status="ok", observed_at_utc=observed, body=body)}))
                self.assertEqual(result["reports"]["monthly"], "failed")
                current = self.dashboard()["monthly"][0]
                self.assertEqual(current["status"], "stale")
                self.assertEqual(current["data_status"], "ok")
                self.assertEqual(current["period_label"], "2026-09")
                self.assertEqual(current["metrics"]["duration_raw"], 120)

    def test_explicit_empty_data_and_listen_blocks_remain_valid_empty_results(self):
        for data in (None, {}):
            self.assertEqual(normalize_report("monthly", dict(code=200, data=data))[0], "empty")
        body = monthly_body()
        body["data"]["listenTimeDistributionBlock"] = None
        self.assertEqual(normalize_report("monthly", body)[0], "empty")
        body["data"]["listenTimeDistributionBlock"] = {}
        self.assertEqual(normalize_report("monthly", body)[0], "empty")

    def test_invalid_numbers_duplicate_days_and_wrong_report_type_are_rejected(self):
        for mutation in ("negative", "nan", "bool", "duplicate", "wrong_type"):
            body = monthly_body()
            distribution = body["data"]["listenTimeDistributionBlock"]
            if mutation == "duplicate":
                distribution["durationDetails"] *= 2
            elif mutation == "wrong_type":
                body["data"]["type"] = "week"
            else:
                distribution["playDuration"] = {"negative": -1, "nan": float("nan"), "bool": True}[mutation]
            with self.subTest(mutation=mutation), self.assertRaises(MusicError):
                normalize_report("monthly", body)

    def test_credentials_rejected_before_archiving_and_person_binding_preserved(self):
        document = report_document()
        document["reports"]["monthly"]["body"]["nested"] = {"Authorization": "SECRET"}
        with self.assertRaises(MusicError):
            self.load(document)
        self.assertFalse(list((self.workspace.raw / "captures").glob("*.json")))
        self.load()
        with self.assertRaises(MusicError):
            import_reports(self.workspace, self.path, person="someone-else")

    def test_successful_empty_month_does_not_resurrect_old_current_report(self):
        self.load()
        observed = "2026-10-02T01:00:00+00:00"
        self.load(report_document(observed, {"monthly": dict(status="ok", observed_at_utc=observed, body=dict(code=200, data=None))}))
        result = self.dashboard()
        self.assertEqual(len(result["monthly"]), 1)
        self.assertEqual(result["monthly"][0]["status"], "empty")
        self.assertNotIn("period_label", result["monthly"][0])

    def test_latest_success_per_month_is_selected_without_summing_snapshots(self):
        self.load()
        observed = "2026-10-02T01:00:00+00:00"
        self.load(report_document(observed, {"monthly": dict(status="ok", observed_at_utc=observed, body=monthly_body(duration=130))}))
        observed = "2026-11-01T01:00:00+00:00"
        self.load(report_document(observed, {"monthly": dict(status="ok", observed_at_utc=observed, body=monthly_body("10", 200))}))
        entries = self.dashboard()["monthly"]
        self.assertEqual([r["period_label"] for r in entries], ["2026-10", "2026-09"])
        self.assertEqual([r["metrics"]["duration_raw"] for r in entries], [200, 130])

    def test_additive_migration_preserves_existing_database_identity(self):
        with closing(self.workspace.connect()) as conn:
            identity = conn.execute("SELECT value FROM meta WHERE key='database_id'").fetchone()[0]
            conn.execute("DROP TABLE platform_reports")
            conn.execute("PRAGMA user_version=1")
            conn.execute("UPDATE meta SET value='1.0.0' WHERE key='schema_version'")
            conn.commit()
        self.workspace.initialize()
        with closing(self.workspace.connect(readonly=True)) as conn:
            self.assertEqual(conn.execute("SELECT value FROM meta WHERE key='database_id'").fetchone()[0], identity)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 3)
            self.assertEqual(conn.execute("SELECT count(*) FROM platform_reports").fetchone()[0], 0)

    def test_publication_contains_reports_and_old_version_remains_readable(self):
        self.load()
        published = publish(self.workspace)
        directory = resolve_version(self.workspace.data / "lake")
        manifest = verify_version(directory)
        self.assertEqual(manifest["schema_version"], "1.2.0")
        with duckdb.connect() as conn:
            self.assertEqual(conn.read_parquet(str(directory / "platform_reports.parquet")).count("*").fetchone()[0], 4)
        # A genuine 1.0 manifest has no report dataset; newer readers must still open it.
        manifest["schema_version"] = "1.0.0"
        manifest["datasets"].pop("platform_reports")
        manifest["datasets"].pop("song_tags")
        (directory / "manifest.json").write_bytes(json_bytes(manifest))
        self.assertEqual(verify_version(directory)["schema_version"], "1.0.0")
        self.assertEqual(dashboard_reports(None, available=False)["status"], "empty")

    def test_collector_keeps_other_reports_after_failure_and_strips_credentials(self):
        (self.workspace.home / ".local/session.json").write_bytes(json_bytes(dict(account_key="42", cookie="SECRET")))
        responses = [dict(body=dict(data=dict(code=200, account=dict(id=42)))), MusicError("offline"),
            dict(body=monthly_body("10", 3)), dict(body=dict(code=200, data=dict(tagPreferenceVos=[]), cookie="SECRET")),
            dict(body=dict(code=200, data=dict(totalDuration=1)))]
        with patch("collector_music.collector.bridge", side_effect=responses) as bridge, patch("collector_music.collector.time.sleep"):
            result = collect_reports(self.workspace)
        self.assertEqual(bridge.call_count, 5)
        self.assertEqual(result["reports"], dict(monthly="failed", realtime="ok", preference="empty", total="ok"))
        for path in (self.workspace.raw / "objects").glob("*"):
            self.assertNotIn(b"SECRET", path.read_bytes())

    def test_collector_account_mismatch_stops_and_expired_session_archives_failures(self):
        (self.workspace.home / ".local/session.json").write_bytes(json_bytes(dict(account_key="42", cookie="SECRET")))
        with patch("collector_music.collector.bridge", return_value=dict(body=dict(data=dict(code=200, account=dict(id=99))))) as bridge:
            with self.assertRaises(LoginRequired):
                collect_reports(self.workspace)
        self.assertEqual(bridge.call_count, 1)
        responses = [dict(body=dict(data=dict(code=200, account=dict(id=42)))), LoginRequired("expired")]
        with patch("collector_music.collector.bridge", side_effect=responses) as bridge, patch("collector_music.collector.time.sleep"):
            result = collect_reports(self.workspace)
        self.assertEqual(bridge.call_count, 2)
        self.assertTrue(result["needs_login"])
        self.assertEqual(set(result["reports"].values()), {"failed"})

    def test_preflight_failure_marks_bound_accounts_stale_without_report_requests(self):
        self.load()
        (self.workspace.home / ".local/session.json").write_bytes(json_bytes(dict(account_key="42", cookie="SECRET")))
        for error, needs_login in ((LoginRequired("SECRET"), True), (MusicError("SECRET"), False)):
            with self.subTest(error=type(error).__name__), patch("collector_music.collector.bridge", side_effect=error) as bridge:
                result = collect_reports(self.workspace)
            self.assertEqual(bridge.call_count, 1)
            self.assertEqual(bridge.call_args.args[0], "login_status")
            self.assertEqual(result["status"], "partial")
            self.assertEqual(result["needs_login"], needs_login)
            self.assertEqual(set(result["reports"].values()), {"failed"})
            current = self.dashboard()
            self.assertEqual(current["monthly"][0]["status"], "stale")
            self.assertEqual(current["monthly"][0]["metrics"]["duration_raw"], 120)
            self.assertEqual(current["preference"]["status"], "stale")
            publish(self.workspace)
            audit_result = audit(self.workspace)
            self.assertEqual(set(row["status"] for row in audit_result["latest_platform_reports"]), {"failed"})
            self.assertTrue(any("平台报告" in warning for warning in audit_result["warnings"]))
            for path in (self.workspace.raw / "objects").glob("*"):
                self.assertNotIn(b"SECRET", path.read_bytes())

    def test_preflight_failure_does_not_bind_unverified_identity_or_replace_mismatched_account(self):
        (self.workspace.home / ".local/session.json").write_bytes(json_bytes(dict(account_key="42", cookie="SECRET")))
        with patch("collector_music.collector.bridge", side_effect=LoginRequired("expired")):
            with self.assertRaises(LoginRequired):
                collect_reports(self.workspace)
        with closing(self.workspace.connect(readonly=True)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM source_accounts").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM platform_reports").fetchone()[0], 0)
        self.load()
        with patch("collector_music.collector.bridge", return_value=dict(body=dict(data=dict(code=200, account=dict(id=99))))):
            with self.assertRaises(LoginRequired):
                collect_reports(self.workspace)
        with closing(self.workspace.connect(readonly=True)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM platform_reports").fetchone()[0], 4)


if __name__ == "__main__":
    unittest.main()
