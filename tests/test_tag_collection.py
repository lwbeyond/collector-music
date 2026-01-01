from contextlib import closing
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from collector_music.common import LoginRequired, MusicError, WorkspaceBusy, atomic_write, json_bytes, parse_json
from collector_music.netease import import_netease
from collector_music.song_tags import extract_labels
from collector_music.store import Workspace
from collector_music.tag_collection import adopt_tag_evidence, collect_song_tags, plan_song_tags
from tests.test_pipeline import netease_document
from tests.test_tag_coverage import encyclopedia

ROOT = Path(__file__).resolve().parents[1]
ACCOUNT = "123456"


class TagCollectionTests(unittest.TestCase):
    def setUp(self):
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / ".tmp", prefix="test-tag-collection-")
        self.workspace = Workspace(self.temp.name).initialize()
        self.session = self.workspace.home / ".local/session.json"
        self.session.write_bytes(json_bytes(dict(account_key=ACCOUNT, cookie="SECRET")))
        self.catalog()

    def tearDown(self):
        self.temp.cleanup()

    def catalog(self, ids=(123, 234, 345)):
        document = netease_document()
        document["scopes"]["likes"]["body"]["ids"] = list(ids)
        path = self.workspace.home / "catalog.json"
        path.write_bytes(json_bytes(document))
        import_netease(self.workspace, path)

    def login(self, account=ACCOUNT):
        return dict(body=dict(data=dict(code=200, account=dict(id=int(account)))))

    def successful(self):
        body = encyclopedia()
        body["cookie"] = "SECRET"
        return dict(body=body)

    def count(self):
        with closing(self.workspace.connect(readonly=True)) as conn:
            return conn.execute("SELECT count(*) FROM song_tags").fetchone()[0]

    def test_full_catalog_plan_deduplicates_and_old_database_plan_is_read_only(self):
        plan = plan_song_tags(self.workspace, limit=2)
        self.assertEqual((plan["total"], plan["cached"], plan["pending"], plan["selected"]), (3, 0, 3, 2))
        self.assertEqual(plan["song_ids"], ["123", "234"])
        with closing(self.workspace.connect()) as conn:
            conn.execute("DROP TABLE song_tags")
            conn.commit()
        self.assertEqual(plan_song_tags(self.workspace)["song_ids"], ["123", "234", "345"])
        with closing(self.workspace.connect(readonly=True)) as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM sqlite_master WHERE name='song_tags'").fetchone())

    def test_limit_success_empty_and_resume_do_not_refetch_missing_fields(self):
        empty = dict(body=dict(code=200, data=dict(blocks=[])))
        with patch("collector_music.tag_collection.bridge", side_effect=[self.login(), self.successful(), empty]) as call, \
                patch("collector_music.tag_collection.time.sleep") as sleep:
            result = collect_song_tags(self.workspace, limit=2)
        self.assertEqual(result["status"], "limited")
        self.assertEqual((result["attempted"], result["ok"], result["empty"], result["remaining"]), (2, 1, 1, 1))
        self.assertEqual(sleep.call_count, 3)
        self.assertEqual([item.args[1].get("id") for item in call.call_args_list[1:]], ["123", "234"])
        with patch("collector_music.tag_collection.bridge", side_effect=[self.login(), self.successful()]) as call, \
                patch("collector_music.tag_collection.time.sleep"):
            self.assertEqual(collect_song_tags(self.workspace)["status"], "complete")
        self.assertEqual(call.call_args_list[1].args[1]["id"], "345")
        with patch("collector_music.tag_collection.bridge", side_effect=AssertionError("cached must not request")):
            self.assertEqual(collect_song_tags(self.workspace)["attempted"], 0)
        self.assertEqual(self.count(), 3)
        for path in (self.workspace.raw / "objects").glob("*"):
            self.assertNotIn(b"SECRET", path.read_bytes())

    def test_rate_limit_records_only_requested_failure_and_stops(self):
        with patch("collector_music.tag_collection.bridge", side_effect=[self.login(), dict(body=dict(code=429, message="requests too frequent"))]) as call, \
                patch("collector_music.tag_collection.time.sleep"):
            result = collect_song_tags(self.workspace)
        self.assertEqual(call.call_count, 2)
        self.assertEqual(result["stop_reason"], "rate_limited")
        self.assertEqual((result["attempted"], result["failed"], result["not_attempted"]), (1, 1, 2))
        self.assertEqual(self.count(), 1)
        self.assertEqual(plan_song_tags(self.workspace)["pending"], 3)

    def test_login_failures_or_mismatch_do_not_create_song_attempts(self):
        for response, reason in ((LoginRequired("SECRET"), "login_required"), (self.login("999"), "account_mismatch")):
            with self.subTest(reason=reason), patch("collector_music.tag_collection.bridge", side_effect=[response]) as call, \
                    patch("collector_music.tag_collection.time.sleep"):
                result = collect_song_tags(self.workspace)
            self.assertEqual(call.call_count, 1)
            self.assertEqual(result["stop_reason"], reason)
            self.assertEqual((result["attempted"], result["not_attempted"]), (0, 3))
            self.assertEqual(self.count(), 0)

    def test_shared_lock_busy_defers_without_failing_a_song(self):
        with self.workspace.lock("collect"), patch("collector_music.tag_collection.bridge", side_effect=AssertionError("busy")), \
                patch("collector_music.tag_collection.time.sleep"):
            result = collect_song_tags(self.workspace)
        self.assertEqual((result["status"], result["stop_reason"]), ("deferred", "collect_busy"))
        self.assertEqual((result["attempted"], result["failed"]), (0, 0))
        with self.workspace.lock("tag-collect"), self.assertRaises(WorkspaceBusy):
            collect_song_tags(self.workspace)

    def test_pacing_releases_shared_lock_but_each_request_holds_it(self):
        def sleep(delay):
            self.assertGreaterEqual(delay, 3)
            with self.workspace.lock("collect"):
                pass
        def request(method, params):
            with self.assertRaises(WorkspaceBusy), self.workspace.lock("collect"):
                pass
            return self.login() if method == "login_status" else self.successful()
        with patch("collector_music.tag_collection.bridge", side_effect=request) as call, \
                patch("collector_music.tag_collection.time.sleep", side_effect=sleep):
            result = collect_song_tags(self.workspace, limit=1)
        self.assertEqual(result["attempted"], 1)
        self.assertEqual(call.call_count, 2)

    def test_unattempted_ids_precede_previous_failures_on_resume(self):
        with patch("collector_music.tag_collection.bridge", side_effect=[self.login(), dict(body=dict(code=429))]), \
                patch("collector_music.tag_collection.time.sleep"):
            collect_song_tags(self.workspace)
        plan = plan_song_tags(self.workspace)
        self.assertEqual(plan["song_ids"], ["234", "345", "123"])
        self.assertEqual((plan["pending_new"], plan["pending_failed"]), (2, 1))
        with patch("collector_music.tag_collection.bridge", side_effect=[self.login(), self.successful(), self.successful()]) as call, \
                patch("collector_music.tag_collection.time.sleep"):
            result = collect_song_tags(self.workspace, limit=2)
        requested = [item.args[1]["id"] for item in call.call_args_list[1:]]
        self.assertEqual(requested, ["234", "345"])
        self.assertEqual(result["attempted"], len(set(requested)))
        self.assertEqual(plan_song_tags(self.workspace)["song_ids"], ["123"])

    def test_response_spool_recovers_without_repeating_a_finished_request(self):
        # All three songs are collected one at a time; a disk/import interruption
        # after response one must not cause that response to be fetched again.
        with patch("collector_music.tag_collection.bridge", side_effect=[self.login(), self.successful()]), \
                patch("collector_music.tag_collection.time.sleep"), \
                patch("collector_music.tag_collection.import_song_tags", side_effect=OSError("interrupted import")):
            with self.assertRaises(OSError):
                collect_song_tags(self.workspace, limit=1)
        spool = self.workspace.home / ".local/tag-collection/spool"
        self.assertEqual(len(list(spool.glob("capture-*.json"))), 1)
        with patch("collector_music.tag_collection.bridge", side_effect=[self.login(), self.successful()]) as call, \
                patch("collector_music.tag_collection.time.sleep"):
            result = collect_song_tags(self.workspace, limit=1)
        self.assertEqual(result["recovered"], 1)
        self.assertEqual(call.call_args_list[1].args[1]["id"], "234")
        self.assertEqual(list(spool.iterdir()), [])
        self.assertEqual(self.count(), 2)

    def test_session_change_stops_between_songs_and_collect_lock_is_released(self):
        changed = False
        def update(progress):
            nonlocal changed
            if progress["attempted"] == 1 and not changed:
                with self.workspace.lock("collect"):
                    self.session.write_bytes(json_bytes(dict(account_key=ACCOUNT, cookie="NEW_SECRET")))
                changed = True
        with patch("collector_music.tag_collection.bridge", side_effect=[self.login(), self.successful()]) as call, \
                patch("collector_music.tag_collection.time.sleep"):
            result = collect_song_tags(self.workspace, notify=update)
        self.assertEqual(call.call_count, 2)
        self.assertEqual(result["stop_reason"], "session_changed")
        self.assertEqual(result["not_attempted"], 2)

    def evidence(self):
        directory = self.workspace.home / ".local/validation/sample"
        observed = "2026-10-29T01:00:00.000+00:00"
        completed = "2026-10-29T01:01:00.000+00:00"
        results = []
        for index, track_id in enumerate(("123", "234"), 1):
            body = encyclopedia()
            results.append(dict(song_id=track_id, observed_at_utc=observed, status="ok", **extract_labels(body)))
            atomic_write(directory / "responses" / f"{index:02d}.json", json_bytes(dict(
                method="song_wiki_summary", song_id=track_id, observed_at_utc=observed, body=body)))
        atomic_write(directory / "summary.json", json_bytes(dict(format="collector-music-tag-coverage",
            version=1, checked_at_utc=completed, results=results)))
        return directory

    def test_adoption_retains_original_times_and_reuses_validated_raw_evidence(self):
        directory = self.evidence()
        with patch("collector_music.tag_collection.bridge", side_effect=AssertionError("adoption is offline")):
            result = adopt_tag_evidence(self.workspace, directory, account_key=ACCOUNT)
        self.assertEqual(result["adopted"], 2)
        self.assertEqual(adopt_tag_evidence(self.workspace, directory, account_key=ACCOUNT)["cached"], 2)
        with closing(self.workspace.connect(readonly=True)) as conn:
            for row in conn.execute("SELECT * FROM song_tags"):
                self.assertEqual(row["observed_at_utc"], "2026-10-29T01:00:00.000+00:00")
                self.assertEqual(parse_json(row["payload_json"])["provenance"]["kind"], "adopted_from_sample")
        self.assertEqual(plan_song_tags(self.workspace)["song_ids"], ["345"])

    def test_adoption_validates_all_entries_before_writing_any(self):
        directory = self.evidence()
        path = directory / "responses/02.json"
        response = parse_json(path.read_bytes())
        response["song_id"] = "345"
        path.write_bytes(json_bytes(response))
        with self.assertRaises(MusicError):
            adopt_tag_evidence(self.workspace, directory, account_key=ACCOUNT)
        self.assertEqual(self.count(), 0)
        directory = self.evidence()
        response = parse_json(path.read_bytes())
        response["body"]["Authorization"] = "SECRET"
        path.write_bytes(json_bytes(response))
        with self.assertRaises(MusicError):
            adopt_tag_evidence(self.workspace, directory, account_key=ACCOUNT)
        self.assertEqual(self.count(), 0)

    def test_invalid_delay_and_limit_are_rejected_before_requests(self):
        for delay in (float("nan"), float("inf"), 2, True):
            with self.subTest(delay=delay), self.assertRaises(MusicError):
                collect_song_tags(self.workspace, delay=delay)
        for limit in (0, -1, 1.5, True):
            with self.subTest(limit=limit), self.assertRaises(MusicError):
                plan_song_tags(self.workspace, limit=limit)


if __name__ == "__main__":
    unittest.main()
