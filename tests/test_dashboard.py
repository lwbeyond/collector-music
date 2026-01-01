from __future__ import annotations

import copy
import http.client
import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from collector_music.common import json_bytes
from collector_music.dashboard import DashboardStore, PublicationChanged, dashboard_data, handler_for
from collector_music.lake import publish
from collector_music.netease import import_netease
from collector_music.pano import import_pano
from collector_music.platform_reports import import_reports
from collector_music.store import Workspace
from test_pipeline import ROOT, line, netease_document, pano_row


class DashboardTests(unittest.TestCase):
    def setUp(self):
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        self.root = Path(self.temp.name)
        self.workspace = Workspace(self.root / "workspace").initialize()
        self.lake = self.workspace.data / "lake"
        self.document = netease_document()
        self.ingest(self.document)
        pano = self.root / "pano.jsonl"
        pano.write_bytes(line(pano_row()))
        import_pano(self.workspace, pano, device="phone", stream="main")
        publish(self.workspace)

    def tearDown(self):
        self.temp.cleanup()

    def ingest(self, document):
        path = self.root / "snapshot.json"
        path.write_bytes(json_bytes(document))
        import_netease(self.workspace, path)

    def later(self):
        document = copy.deepcopy(self.document)
        document["collected_at_utc"] = "2026-10-03T01:00:00Z"
        for scope in document["scopes"].values():
            scope["observed_at_utc"] = "2026-10-03T00:59:00Z"
        return document

    def test_source_counts_are_separate_and_payload_is_minimal(self):
        data = dashboard_data(self.lake)
        self.assertEqual(data["stats"], dict(pano=1, recent=1, likes=1, playlists=1))
        self.assertEqual(data["rankings"]["all"][0]["artist"], "艺人")
        self.assertEqual(data["likes"][0]["track"], "测试歌曲")
        self.assertEqual({r["source"] for r in data["records"]}, {"pano", "netease_recent"})
        serialized = json.dumps(data)
        for private in ("payload_json", "raw_ref", "cookie", "account_key", "actual_listened_seconds"):
            self.assertNotIn(private, serialized)

    def test_library_uses_latest_successful_snapshot_not_sum(self):
        document = self.later()
        document["scopes"]["likes"]["body"]["ids"] = []
        self.ingest(document)
        publish(self.workspace)
        data = dashboard_data(self.lake)
        self.assertEqual(data["stats"]["likes"], 0)
        self.assertEqual(data["likes"], [])
        self.assertEqual(data["stats"]["playlists"], 1)
        self.assertEqual(len(data["rankings"]["week"]), 1)

    def test_overlap_annotations_preserve_both_source_rows_and_counts(self):
        song=self.document['scopes']['recent']['body']['data']['list'][0]['data']
        log=self.root/'matching.jsonl'
        log.write_bytes(line(dict(pano_row(),track=song['name'],artist='艺人',album='专辑',durationMs=210000)))
        import_pano(self.workspace,log,device='tablet',stream='main')
        publish(self.workspace)
        data=dashboard_data(self.lake)
        self.assertEqual(data['stats']['pano'],2)
        self.assertEqual(data['stats']['recent'],1)
        self.assertEqual(len(data['records']),3)
        self.assertEqual(len(data['overlap_candidates']),1)
        self.assertEqual(data['overlap_candidates'][0]['status'],'candidate')
        self.assertEqual(data['overlap_candidates'][0]['time_delta_ms'],0)
        self.assertNotIn('person_key',json.dumps(data))

    def test_all_enriched_likes_are_visible_without_listening_history_or_record_limit(self):
        document = self.later()
        document["scopes"]["likes"]["body"].update(ids=[700,800], detail_status="ok", songs=[
            dict(id=i,name=f"Liked {i}",ar=[dict(name="New artist")],al=dict(name="New album")) for i in [800,700]])
        document["scopes"]["playlists"]["body"]["playlist"] = [
            dict(id=1,name="Created",creator=dict(userId=123456),subscribed=False,trackCount=2),
            dict(id=2,name="Subscribed",creator=dict(userId=987),subscribed=True,trackCount=4)]
        self.ingest(document)
        publish(self.workspace)
        with patch("collector_music.dashboard.RECORD_LIMIT", 1):
            data = dashboard_data(self.lake)
        self.assertEqual([i["track"] for i in data["likes"]], ["Liked 700","Liked 800"])
        self.assertEqual(data["likes"][0]["artist"], "New artist")
        self.assertEqual(data["likes"][0]["album"], "New album")
        self.assertEqual(data["scopes"]["likes"]["details_resolved"], 2)
        self.assertFalse(data["likes_truncated"])
        self.assertEqual([i["kind"] for i in data["playlists"]], ["created","subscribed"])
        self.assertEqual([i["track_count"] for i in data["playlists"]], [2,4])

    def test_failed_scope_keeps_previous_value_with_stale_label(self):
        document = self.later()
        document["scopes"]["likes"] = dict(status="failed", observed_at_utc="2026-10-03T00:59:00Z", error="fixture failure")
        self.ingest(document)
        publish(self.workspace)
        data = dashboard_data(self.lake)
        self.assertEqual(data["stats"]["likes"], 1)
        self.assertEqual(data["scopes"]["likes"]["status"], "stale")
        self.assertTrue(data["scopes"]["likes"]["latest_attempt_at"] > data["scopes"]["likes"]["observed_at"])

    def test_store_updates_only_after_publication(self):
        store = DashboardStore(self.workspace.home)
        first = store.read()
        self.ingest(self.later())
        self.assertEqual(store.read()["generation"], first["generation"])
        new = publish(self.workspace)
        self.assertEqual(store.read()["generation"], new["generation"])

    def test_platform_reports_are_published_separately_with_actual_period_and_no_raw_data(self):
        store = DashboardStore(self.workspace.home)
        self.assertEqual(store.read()["platform_reports"]["status"], "empty")
        stamp = lambda value: int(datetime.fromisoformat(value).timestamp() * 1000)
        document = dict(format="collector-music-netease-reports", version=1, account_key="123456",
            collected_at_utc="2026-10-30T00:00:00Z", reports={
                "monthly": dict(status="ok", observed_at_utc="2026-10-30T00:00:00Z", body={
                    "code": 200, "data": {"type": "month", "startTime": stamp("2026-09-01T00:00:00+08:00"),
                        "endTime": stamp("2026-09-30T00:00:00+08:00"),
                        "listenTimeDistributionBlock": {"playDuration": 606, "listenDays": 12},
                        "topStyleBlock": {"sections": [{"genreName": "流行", "percent": "56"}]},
                        "friendsListenMonthBlock": {"privateFixture": "DO_NOT_EXPOSE"}}})})
        path = self.root / "reports.json"
        path.write_bytes(json_bytes(document))
        import_reports(self.workspace, path)
        self.assertEqual(store.read()["platform_reports"]["status"], "empty")
        publish(self.workspace)
        data = store.read()
        report = data["platform_reports"]["monthly"][0]
        self.assertEqual(report["period_label"], "2026-09")
        self.assertEqual(report["metrics"]["listen_days"], 12)
        self.assertIsNone(report["metrics"]["duration_minutes"])
        self.assertEqual(report["genres"], [{"name": "流行", "percent": 56}])
        self.assertEqual(data["stats"]["recent"], 1)
        serialized = json.dumps(data["platform_reports"])
        for private in ("account_key", "person_key", "payload_json", "raw_ref", "DO_NOT_EXPOSE"):
            self.assertNotIn(private, serialized)

    def test_analysis_uses_complete_publication_even_when_records_are_capped(self):
        log = self.root / "more.jsonl"
        log.write_bytes(line(pano_row()) * 12)
        import_pano(self.workspace, log, device="tablet", stream="many")
        publish(self.workspace)
        with patch("collector_music.dashboard.RECORD_LIMIT", 1):
            store = DashboardStore(self.workspace.home)
            data = store.read()
        self.assertEqual(len(data["records"]), 1)
        analysis = store.analyze(dict(generation=[data["generation"]], source=["pano"], period=["all"]))
        self.assertEqual(analysis["summary"]["total"], 13)
        self.assertEqual(sum(analysis["hours"]), 13)
        self.assertEqual(sum(sum(row) for row in analysis["heatmap"]), 13)
        self.assertEqual(analysis["summary"]["total"], sum(row["records"] for row in analysis["daily"]))

    def test_analytics_and_relative_dates_follow_publication_and_local_midnight(self):
        store = DashboardStore(self.workspace.home)
        first = store.read()
        params = dict(generation=[first["generation"]], source=["pano"], period=["7"])
        with patch("collector_music.dashboard.datetime") as clock:
            clock.now.return_value = datetime(2026, 10, 8, 15, 59, tzinfo=timezone.utc).astimezone(ZoneInfo('Asia/Shanghai'))
            self.assertEqual(store.analyze(params)["summary"]["total"], 1)
            clock.now.return_value = datetime(2026, 10, 8, 16, 0, tzinfo=timezone.utc).astimezone(ZoneInfo('Asia/Shanghai'))
            after = store.analyze(params)
            self.assertEqual(after["today"], "2026-10-09")
            self.assertEqual(after["summary"]["total"], 0)
        log = self.root / "new.jsonl"
        log.write_bytes(line(pano_row()) * 2)
        import_pano(self.workspace, log, device="tablet", stream="new")
        all_params = dict(params, period=["all"])
        self.assertEqual(store.analyze(all_params)["summary"]["total"], 1)
        new = publish(self.workspace)
        with self.assertRaises(PublicationChanged):
            store.analyze(all_params)
        all_params["generation"] = [new["generation"]]
        self.assertEqual(store.analyze(all_params)["summary"]["total"], 3)

    def test_uninitialized_workspace_is_empty_and_not_created(self):
        home = self.root / "absent"
        self.assertEqual(DashboardStore(home).read()["status"], "empty")
        self.assertFalse(home.exists())

    def test_http_exposes_only_dashboard_and_rejects_foreign_origin_and_writes(self):
        store = DashboardStore(self.workspace.home)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(store))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def request(path, method="GET", headers=None):
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
            try:
                conn.request(method, path, headers=headers or {})
                response = conn.getresponse()
                return response.status, dict(response.getheaders()), response.read()
            finally:
                conn.close()
        try:
            status, headers, body = request("/api/dashboard")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["stats"]["pano"], 1)
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
            generation = json.loads(body)["generation"]
            status, _, body = request(f"/api/analytics?generation={generation}&source=pano&period=all")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["summary"]["total"], 1)
            self.assertEqual(request("/analytics.js")[0], 200)
            self.assertEqual(request("/reports.js")[0], 200)
            self.assertEqual(request("/api/analytics?generation=old")[0], 409)
            for invalid in ("source=all", "period=custom", "period=30&period=7", "sql=SELECT%201"):
                self.assertEqual(request(f"/api/analytics?generation={generation}&{invalid}")[0], 400)
            self.assertEqual(request(f"/api/analytics?generation={generation}", headers={"Origin": "https://example.com"})[0], 403)
            for path in ("/.local/session.json", "/../data/music.sqlite", "/api/query?sql=SELECT%201"):
                self.assertEqual(request(path)[0], 404)
            self.assertEqual(request("/api/dashboard", headers={"Host": "example.com"})[0], 403)
            self.assertEqual(request("/api/dashboard", headers={"Origin": "https://example.com"})[0], 403)
            self.assertEqual(request("/api/dashboard", method="POST")[0], 405)
            # Explicit refresh must not silently serve a corrupted publication from cache.
            current = json.loads((self.lake / "current.json").read_text("utf-8"))
            with (self.lake / "versions" / current["generation"] / "observations.parquet").open("ab") as output:
                output.write(b"tampered")
            status, _, body = request("/api/dashboard?refresh=1")
            self.assertEqual(status, 503)
            self.assertNotIn(str(self.workspace.home), body.decode())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
