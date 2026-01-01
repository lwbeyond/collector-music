from __future__ import annotations

import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from collector_music.collector import account_from_status, bridge, clean_body, collect, login
from collector_music.common import MusicError, atomic_write, json_bytes, parse_json
from collector_music.store import Workspace

ROOT = Path(__file__).resolve().parents[1]


class CollectorTests(unittest.TestCase):
    def setUp(self):
        (ROOT/".tmp").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="test-collector-", dir=ROOT/".tmp")
        self.workspace = Workspace(self.temp.name).initialize()
        atomic_write(self.workspace.home/".local/session.json", json_bytes({"account_key":"42", "cookie":"TEST_SECRET"}))

    def tearDown(self):
        self.temp.cleanup()

    def response(self, body):
        return {"body":body}

    def test_anonymous_status_is_not_an_account(self):
        with self.assertRaises(MusicError):
            account_from_status({"data":{"code":200,"account":None}})

    def test_account_identity_mismatch_stops_before_data_requests(self):
        with patch("collector_music.collector.bridge", return_value=self.response({"data":{"code":200,"account":{"id":99}}})) as call:
            with self.assertRaises(MusicError):
                collect(self.workspace)
        self.assertEqual(call.call_count, 1)
        self.assertFalse(list((self.workspace.raw/"captures").glob("*.json")))

    def test_network_failure_stops_later_scopes_and_archives_status(self):
        responses = [self.response({"data":{"code":200,"account":{"id":42}}}), MusicError("timeout")]
        with patch("collector_music.collector.bridge", side_effect=responses) as call, patch("collector_music.collector.time.sleep"):
            result = collect(self.workspace, scopes=("recent","likes"))
        self.assertEqual(call.call_count, 2)
        self.assertEqual(result["status"], "partial")
        snapshot = parse_json((self.workspace.home/".local/last-collection.json").read_bytes())
        self.assertEqual(snapshot["scopes"]["likes"]["status"], "failed")
        for path in (self.workspace.raw/"objects").glob("*"):
            self.assertNotIn(b"TEST_SECRET", path.read_bytes())

    def test_playlist_pagination_is_complete_before_normalization(self):
        responses = [self.response({"data":{"code":200,"account":{"id":42}}}),
                     self.response({"code":200,"playlist":[{"id":100,"name":"a"}],"more":True}),
                     self.response({"code":200,"playlist":[{"id":101,"name":"b"}],"more":False})]
        with patch("collector_music.collector.bridge", side_effect=responses) as call, patch("collector_music.collector.time.sleep"):
            result = collect(self.workspace, scopes=("playlists",))
        self.assertEqual(result["inserted_records"], 2)
        self.assertEqual(call.call_args_list[2].args[1]["offset"], 1)
        snapshot = parse_json((self.workspace.home/".local/last-collection.json").read_bytes())
        self.assertEqual(len(snapshot["scopes"]["playlists"]["body"]["original_pages"]), 2)

    def test_duplicate_playlist_page_is_failure_and_pages_retained(self):
        responses = [self.response({"data":{"code":200,"account":{"id":42}}}),
                     self.response({"code":200,"playlist":[{"id":100,"name":"a"}],"more":True}),
                     self.response({"code":200,"playlist":[{"id":100,"name":"a"}],"more":False})]
        with patch("collector_music.collector.bridge", side_effect=responses), patch("collector_music.collector.time.sleep"):
            result = collect(self.workspace, scopes=("playlists",))
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["inserted_records"], 0)
        snapshot = parse_json((self.workspace.home/".local/last-collection.json").read_bytes())
        self.assertEqual(len(snapshot["scopes"]["playlists"]["pages"]), 2)

    def test_recursive_credentials_removed(self):
        self.assertEqual(clean_body({"cookie":"x", "data":[{"MUSIC_U":"y","id":1}]}), {"data":[{"id":1}]})

    def detail(self, song_id):
        return dict(id=song_id, name=f"Song {song_id}", ar=[dict(name="Artist")], al=dict(name="Album"))

    def test_likes_fetch_all_detail_batches_and_match_by_id_not_response_order(self):
        responses = [self.response({"data":{"code":200,"account":{"id":42}}}),
            self.response({"code":200,"ids":[10,20,30]}),
            self.response({"code":200,"songs":[self.detail(20),self.detail(10)]}),
            self.response({"code":200,"songs":[self.detail(30)]})]
        with patch("collector_music.collector.bridge", side_effect=responses) as call, patch("collector_music.collector.time.sleep"), patch("collector_music.collector.SONG_DETAIL_BATCH", 2):
            result = collect(self.workspace, scopes=("likes",))
        self.assertEqual(result["likes_detail_status"], "ok")
        self.assertEqual(call.call_args_list[2].args[1]["ids"], "10,20")
        self.assertEqual(call.call_args_list[3].args[1]["ids"], "30")
        with closing(self.workspace.connect(readonly=True)) as conn:
            rows = conn.execute("SELECT entity_id,payload_json FROM library_items ORDER BY position").fetchall()
            self.assertEqual([(r[0],parse_json(r[1])["song"]["name"]) for r in rows], [("10","Song 10"),("20","Song 20"),("30","Song 30")])
            self.assertEqual(conn.execute("SELECT count(*) FROM observations").fetchone()[0], 0)

    def test_missing_song_detail_keeps_relationship_and_marks_partial(self):
        responses = [self.response({"data":{"code":200,"account":{"id":42}}}),
            self.response({"code":200,"ids":[10,20]}), self.response({"code":200,"songs":[self.detail(10)]})]
        with patch("collector_music.collector.bridge", side_effect=responses), patch("collector_music.collector.time.sleep"):
            result = collect(self.workspace, scopes=("likes",))
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["inserted_records"], 2)
        body = parse_json((self.workspace.home/".local/last-collection.json").read_bytes())["scopes"]["likes"]["body"]
        self.assertEqual(body["missing_detail_ids"], ["20"])
        self.assertEqual(body["ids"], [10,20])

    def test_detail_network_failure_archives_completed_batches_and_stops_requests(self):
        responses = [self.response({"data":{"code":200,"account":{"id":42}}}),
            self.response({"code":200,"ids":[10,20,30]}),
            self.response({"code":200,"songs":[self.detail(10),self.detail(20)]}), MusicError("timeout")]
        with patch("collector_music.collector.bridge", side_effect=responses) as call, patch("collector_music.collector.time.sleep"), patch("collector_music.collector.SONG_DETAIL_BATCH", 2):
            result = collect(self.workspace, scopes=("likes","playlists"))
        self.assertEqual(call.call_count, 4)
        self.assertEqual(result["scopes"], {"likes":"ok","playlists":"failed"})
        self.assertEqual(result["status"], "partial")
        body = parse_json((self.workspace.home/".local/last-collection.json").read_bytes())["scopes"]["likes"]["body"]
        self.assertEqual(len(body["songs"]), 2)
        self.assertEqual(body["missing_detail_ids"], ["30"])
        self.assertEqual(len(body["detail_pages"]), 1)

    def test_unrequested_detail_is_not_attached_to_another_song(self):
        responses = [self.response({"data":{"code":200,"account":{"id":42}}}),
            self.response({"code":200,"ids":[10]}), self.response({"code":200,"songs":[self.detail(20)]})]
        with patch("collector_music.collector.bridge", side_effect=responses), patch("collector_music.collector.time.sleep"):
            result = collect(self.workspace, scopes=("likes",))
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["inserted_records"], 1)
        body = parse_json((self.workspace.home/".local/last-collection.json").read_bytes())["scopes"]["likes"]["body"]
        self.assertEqual(body["songs"], [])

    def test_qr_login_saves_only_after_identity_validation(self):
        responses = [self.response({"data":{"unikey":"TEST"}}),
                     self.response({"data":{"qrimg":"data:image/png;base64,aGVsbG8="}}),
                     self.response({"code":803,"cookie":"NEW_TEST_SECRET"}),
                     self.response({"data":{"code":200,"account":{"id":42}}})]
        with patch("collector_music.collector.bridge", side_effect=responses), patch("collector_music.collector.time.sleep"):
            result = login(self.workspace, open_browser=False, notify=lambda _: None)
        self.assertEqual(result["account_key"], "42")
        self.assertEqual(parse_json((self.workspace.home/".local/session.json").read_bytes())["cookie"], "NEW_TEST_SECRET")
        self.assertFalse(list((self.workspace.raw/"objects").glob("*")))

    def test_expired_qr_keeps_existing_session(self):
        before = (self.workspace.home/".local/session.json").read_bytes()
        responses = [self.response({"data":{"unikey":"TEST"}}),
                     self.response({"data":{"qrimg":"data:image/png;base64,aGVsbG8="}}), self.response({"code":800})]
        with patch("collector_music.collector.bridge", side_effect=responses), patch("collector_music.collector.time.sleep"):
            with self.assertRaises(MusicError):
                login(self.workspace, open_browser=False, notify=lambda _: None)
        self.assertEqual((self.workspace.home/".local/session.json").read_bytes(), before)

    @unittest.skipUnless((ROOT/"node_modules/@neteasecloudmusicapienhanced/api").exists(), "Node dependencies not installed")
    def test_bridge_allows_offline_qr_but_blocks_account_writes(self):
        response = bridge("login_qr_create", {"key":"test-offline", "qrimg":True})
        self.assertTrue(response["body"]["data"]["qrimg"].startswith("data:image/png;base64,"))
        with self.assertRaises(MusicError):
            bridge("like", {"id":123,"like":True})


if __name__ == "__main__":
    unittest.main()
