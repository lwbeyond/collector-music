"""Read-only loopback dashboard over a verified, fixed Parquet publication."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import ProxyHandler, build_opener
from zoneinfo import ZoneInfo

import duckdb

from .common import MusicError
from .analytics import AnalysisQueryError, AnalyticsIndex, artist_label
from .lake import resolve_version, verify_version
from .overlap import candidates
from .platform_reports import dashboard_reports
from .song_catalog import song_catalog, catalog_facets

ASSETS = Path(__file__).parent / "web"
RECORD_LIMIT = 5000


def rows(conn, sql, params=()):
    result = conn.execute(sql, params)
    columns = [x[0] for x in result.description]
    return [dict(zip(columns, row)) for row in result.fetchall()]


def dashboard_data(lake):
    return dashboard_snapshot(lake)[0]


def dashboard_snapshot(lake):
    directory = resolve_version(lake)
    manifest = verify_version(directory)
    if manifest["schema_version"] not in ("1.0.0", "1.1.0", "1.2.0"):
        raise MusicError("看板尚不支持此数据契约。")
    with duckdb.connect() as con:
        for name, item in manifest["datasets"].items():
            con.read_parquet(str(directory / item["path"])).create_view(name)
        observations = rows(con, """SELECT id,person_key,source,device_key,event_kind,platform_track_id,recording_key,player_package,
            track,artist,album,track_duration_ms,occurred_at_utc,observed_at_utc,time_basis
            FROM observations ORDER BY occurred_at_utc DESC NULLS LAST,id DESC LIMIT ?""", [RECORD_LIMIT])
        counts = {r["source"]: r["n"] for r in rows(con, """SELECT source,count(*) AS n FROM observations
            WHERE event_kind IN ('scrobble','recent_observation') GROUP BY source""")}
        observation_count = con.execute("SELECT count(*) FROM observations").fetchone()[0]
        last_device = rows(con, """SELECT track,artist,device_key,occurred_at_utc FROM observations
            WHERE source='pano' AND event_kind='scrobble'
            ORDER BY occurred_at_utc DESC NULLS LAST,id DESC LIMIT 1""")
        daily = rows(con, "SELECT source,date,sum(record_count) AS records FROM daily_music_features GROUP BY source,date ORDER BY date")
        coverage = rows(con, "SELECT * FROM coverage ORDER BY observed_at_utc DESC,import_id DESC")
        imports = rows(con, """SELECT kind,device_key,status,observed_at_utc,inserted_records,pending_bytes
            FROM imports ORDER BY observed_at_utc DESC,id DESC LIMIT 12""")
        latest, successful = {}, {}
        for item in coverage:
            if item["source"] != "netease":
                continue
            key = (item["account_key"], item["scope"])
            latest.setdefault(key, item)
            if item["transport_status"] == "ok":
                successful.setdefault(key, item)
        summaries = {}
        libraries = {"likes": [], "playlists": []}
        rankings = {"week": [], "all": []}
        for scope in ("recent", "week", "all", "likes", "playlists"):
            good = [v for (_, s), v in successful.items() if s == scope]
            current = [v for (_, s), v in latest.items() if s == scope]
            summaries[scope] = dict(count=sum(v["record_count"] for v in good) if good else None,
                observed_at=max((v["observed_at_utc"] for v in good), default=None),
                latest_attempt_at=max((v["observed_at_utc"] for v in current), default=None),
                status="unavailable" if not good else "stale" if any(v["transport_status"] != "ok" for v in current) else "ok")
            for item in good:
                if scope in libraries:
                    for entry in rows(con, """SELECT entity_id,label,position,payload_json FROM library_items
                            WHERE import_id=? AND scope=? ORDER BY position""", [item["import_id"], scope]):
                        payload = json.loads(entry.pop("payload_json"))
                        if scope == "likes":
                            song = payload.get("song") if isinstance(payload, dict) else None
                            if song:
                                artists = song.get("ar", song.get("artists", []))
                                album = song.get("al", song.get("album"))
                                entry.update(track=song["name"], artist=" / ".join(a["name"] for a in artists),
                                             album=album.get("name") if album else None,
                                             track_duration_ms=song.get("dt", song.get("duration")))
                        else:
                            creator = payload.get("creator")
                            own = isinstance(creator, dict) and str(creator.get("userId")) == item["account_key"]
                            entry.update(kind="created" if own else "subscribed" if payload.get("subscribed") is True else "unknown",
                                         track_count=payload.get("trackCount"), special_type=payload.get("specialType"))
                        libraries[scope].append(entry)
                if scope in rankings:
                    rankings[scope].extend(rows(con, """SELECT position,platform_track_id,track,artist,
                        reported_play_count,reported_score FROM ranking_items
                        WHERE import_id=? AND scope=? ORDER BY position""", [item["import_id"], scope]))
        known = {}
        for item in observations:
            item["artist"] = artist_label(item["artist"], item["source"])
            if item["platform_track_id"]:
                known.setdefault(item["platform_track_id"], item)
        overlap_candidates = candidates(observations)
        for item in observations:
            item.pop("person_key")
            item.pop("player_package")
        for entries in rankings.values():
            for item in entries:
                item["artist"] = artist_label(item["artist"])
                known.setdefault(item["platform_track_id"], item)
        likes = []
        for item in libraries["likes"]:
            song = known.get(item["entity_id"], {})
            likes.append(dict(track_id=item["entity_id"], track=item.get("track", song.get("track")),
                              artist=item.get("artist", song.get("artist")), album=item.get("album", song.get("album")),
                              track_duration_ms=item.get("track_duration_ms", song.get("track_duration_ms"))))
        summaries["likes"]["details_resolved"] = sum(bool(item["track"]) for item in likes)
        devices = rows(con, """SELECT i.device_key,max(i.observed_at_utc) AS last_received,
            (SELECT count(*) FROM observations o WHERE o.source='pano' AND o.event_kind='scrobble'
             AND o.device_key=i.device_key) AS records
            FROM imports i WHERE i.kind='pano' GROUP BY i.device_key""")
        catalog = song_catalog(con, tags_available="song_tags" in manifest['datasets'])
        cursor = con.execute("""SELECT source,event_kind,recording_key,platform_track_id,track,artist,album,occurred_at_utc
            FROM observations WHERE event_kind IN ('scrobble','recent_observation')
            ORDER BY occurred_at_utc DESC NULLS LAST,id DESC""")
        columns = [column[0] for column in cursor.description]

        def all_observations():
            while batch := cursor.fetchmany(4096):
                for row in batch:
                    yield dict(zip(columns, row))

        analytics = AnalyticsIndex(all_observations(), manifest["analysis_timezone"], catalog)
        platform_reports = dashboard_reports(con, available="platform_reports" in manifest["datasets"])
    data = dict(status="ready", generation=manifest["generation"], data_revision=manifest["data_revision"],
        published_at=manifest["published_at_utc"], timezone=manifest["analysis_timezone"],
        today=datetime.now(ZoneInfo(manifest["analysis_timezone"])).date().isoformat(),
        stats=dict(pano=counts.get("pano", 0), recent=counts.get("netease_recent", 0),
            likes=summaries["likes"]["count"], playlists=summaries["playlists"]["count"]),
        records=observations, records_total=observation_count, records_truncated=observation_count > RECORD_LIMIT,
        overlap_candidates=overlap_candidates,
        daily=daily, scopes=summaries, rankings=rankings, playlists=libraries["playlists"],
        likes=likes, likes_truncated=False,
        devices=devices, imports=imports, latest_device=last_device[0] if last_device else None,
        platform_reports=platform_reports, song_catalog=catalog_facets(catalog))
    return data, analytics


class PublicationChanged(Exception):
    pass


class DashboardStore:
    def __init__(self, home):
        self.home = Path(home).resolve()
        self.lake = self.home / "data/lake"
        self.identity = hashlib.sha256(str(self.home).encode()).hexdigest()[:24]
        self.lock = threading.RLock()
        self.pointer = None
        self.cached = None
        self.analytics = None

    def read(self, force=False):
        with self.lock:
            pointer_path = self.lake / "current.json"
            if not pointer_path.exists():
                self.pointer = self.cached = self.analytics = None
                return dict(status="empty", message="还没有已发布的数据。先导入一份记录，再发布到看板。")
            pointer = pointer_path.read_bytes()
            if force or pointer != self.pointer or self.cached is None:
                data, analytics = dashboard_snapshot(self.lake)
                self.cached, self.pointer, self.analytics = data, pointer, analytics
            # Relative date ranges must advance even without a new publication.
            return dict(self.cached, today=datetime.now(ZoneInfo(self.cached["timezone"])).date().isoformat())

    def analyze(self, params):
        allowed = {"generation", "source", "period", "start", "end", "artist", "genre", "language", "tag", "q"}
        if set(params) - allowed or any(len(values) != 1 for values in params.values()):
            raise AnalysisQueryError("分析参数无效或重复。")
        query = {key: values[0] for key, values in params.items()}
        generation = query.pop("generation", None)
        with self.lock:
            data = self.read()
            if data["status"] == "empty":
                return data
            if generation != data["generation"]:
                raise PublicationChanged()
            result = self.analytics.query(today=data["today"], **query)
            return dict(result, status="ready", generation=data["generation"], data_revision=data["data_revision"], today=data["today"])


def handler_for(store):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send_content(self, status, body, content_type):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(body)

        def send_json(self, status, data):
            self.send_content(status, json.dumps(data, ensure_ascii=False, allow_nan=False).encode(), "application/json; charset=utf-8")

        def do_GET(self):
            port = self.server.server_port
            allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
            origin = self.headers.get("Origin")
            if self.headers.get("Host") not in allowed or (origin and origin not in {f"http://{x}" for x in allowed}):
                self.send_json(403, {"error": "仅允许本机看板访问。"})
                return
            url = urlsplit(self.path)
            if url.path == "/api/health":
                self.send_json(200, dict(app="collector-music-dashboard", workspace=store.identity))
            elif url.path == "/api/dashboard":
                try:
                    self.send_json(200, store.read(force=url.query == "refresh=1"))
                except (OSError, MusicError, ValueError, KeyError, duckdb.Error):
                    self.send_json(503, {"error": "已发布资料未通过读取或完整性检查，请运行资料校验后重试。"})
            elif url.path == "/api/analytics":
                try:
                    try:
                        params = parse_qs(url.query, keep_blank_values=True, max_num_fields=12)
                    except ValueError:
                        raise AnalysisQueryError("分析参数过多。") from None
                    self.send_json(200, store.analyze(params))
                except PublicationChanged:
                    self.send_json(409, {"error": "已发布版本发生变化，请点击刷新展示后继续分析。"})
                except AnalysisQueryError as exc:
                    self.send_json(400, {"error": str(exc)})
                except (OSError, MusicError, ValueError, KeyError, duckdb.Error):
                    self.send_json(503, {"error": "分析资料未通过读取或完整性检查，请刷新展示后重试。"})
            else:
                assets = {"/": ("index.html", "text/html; charset=utf-8"),
                    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                    "/analytics.js": ("analytics.js", "text/javascript; charset=utf-8"),
                    "/reports.js": ("reports.js", "text/javascript; charset=utf-8"),
                    "/style.css": ("style.css", "text/css; charset=utf-8"),
                    "/favicon.svg": ("favicon.svg", "image/svg+xml")}
                if url.path not in assets:
                    self.send_json(404, {"error": "页面不存在。"})
                    return
                file, content_type = assets[url.path]
                self.send_content(200, (ASSETS / file).read_bytes(), content_type)

        def do_POST(self):
            self.send_json(405, {"error": "看板仅用于读取已发布数据。"})
    return Handler


def serve(home, port=8767, open_browser=True):
    store = DashboardStore(home)
    server = ThreadingHTTPServer(("127.0.0.1", port), handler_for(store))
    url = f"http://127.0.0.1:{server.server_port}"
    print(f"音乐手记：{url}（仅本机）", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def launch(home, port=8767, open_browser=True):
    """Reuse only this workspace's server; never kill an occupied port."""
    home = Path(home).resolve()
    store = DashboardStore(home)
    url = f"http://127.0.0.1:{port}"
    opener = build_opener(ProxyHandler({}))

    def ready():
        try:
            with opener.open(url + "/api/health", timeout=1) as response:
                data = json.load(response)
        except (URLError, OSError):
            return False
        except ValueError as exc:
            raise MusicError("看板端口已被其他服务占用，请用 --port 选择另一端口。") from exc
        if data != dict(app="collector-music-dashboard", workspace=store.identity):
            raise MusicError("此端口正在运行其他服务或资料库，请选择另一端口。")
        return True

    if not ready():
        directory = home / ".local"
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / "dashboard.log").open("ab") as log:
            process = subprocess.Popen([sys.executable, "-m", "collector_music", "--home", str(home),
                "dashboard", "--port", str(port), "--no-open"], cwd=Path(__file__).resolve().parents[1],
                stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), close_fds=True)
        for _ in range(80):
            if process.poll() is not None:
                raise MusicError("看板启动失败，检查 .local/dashboard.log 或换一个端口。")
            if ready():
                break
            time.sleep(.1)
        else:
            process.terminate()
            process.wait(timeout=5)
            raise MusicError("看板启动超时。")
    if open_browser:
        webbrowser.open(url)
    return dict(url=url, status="ready")
