from __future__ import annotations

import sqlite3
import uuid
import os
from contextlib import closing, contextmanager
from pathlib import Path

from . import PARSER_VERSION, SCHEMA_VERSION
from .common import MusicError, WorkspaceBusy, atomic_write, digest, json_bytes, now, parse_json, safe_child, stable_id

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS imports(
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, person_key TEXT NOT NULL,
 device_key TEXT, stream_name TEXT, account_key TEXT,
 observed_at_utc TEXT NOT NULL, ingested_at_utc TEXT NOT NULL,
 payload_sha256 TEXT NOT NULL, raw_ref TEXT NOT NULL, parser_version TEXT NOT NULL,
 status TEXT NOT NULL, inserted_records INTEGER NOT NULL, pending_bytes INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS streams(
 id TEXT PRIMARY KEY, person_key TEXT NOT NULL, device_key TEXT NOT NULL,
 stream_name TEXT NOT NULL, committed_bytes INTEGER NOT NULL,
 prefix_sha256 TEXT NOT NULL, last_raw_ref TEXT NOT NULL,
 UNIQUE(person_key,device_key,stream_name)
);
CREATE TABLE IF NOT EXISTS source_accounts(
 source TEXT NOT NULL, account_key TEXT NOT NULL, person_key TEXT NOT NULL,
 PRIMARY KEY(source,account_key)
);
CREATE TABLE IF NOT EXISTS observations(
 id TEXT PRIMARY KEY, person_key TEXT NOT NULL, source TEXT NOT NULL,
 account_key TEXT, device_key TEXT, stream_id TEXT, source_record_id TEXT NOT NULL,
 event_kind TEXT NOT NULL, recording_key TEXT NOT NULL, platform_track_id TEXT,
 track TEXT NOT NULL, artist TEXT NOT NULL, album TEXT, album_artist TEXT,
 track_duration_ms INTEGER, actual_listened_seconds REAL,
 occurred_at_utc TEXT, observed_at_utc TEXT NOT NULL, ingested_at_utc TEXT NOT NULL,
 source_timezone TEXT, time_precision TEXT NOT NULL, time_basis TEXT NOT NULL,
 player_package TEXT, player_name TEXT, player_version TEXT,
 import_id TEXT NOT NULL REFERENCES imports(id), raw_pointer TEXT NOT NULL,
 quality_flags TEXT NOT NULL, payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS observations_time ON observations(occurred_at_utc, id);
CREATE TABLE IF NOT EXISTS observation_evidence(
 observation_id TEXT NOT NULL REFERENCES observations(id),
 import_id TEXT NOT NULL REFERENCES imports(id), raw_pointer TEXT NOT NULL,
 PRIMARY KEY(observation_id,import_id,raw_pointer)
);
CREATE TABLE IF NOT EXISTS ranking_items(
 id TEXT PRIMARY KEY, import_id TEXT NOT NULL REFERENCES imports(id),
 person_key TEXT NOT NULL, account_key TEXT NOT NULL, scope TEXT NOT NULL,
 position INTEGER NOT NULL, platform_track_id TEXT NOT NULL,
 track TEXT NOT NULL, artist TEXT NOT NULL, reported_play_count REAL,
 reported_score REAL, observed_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS library_items(
 id TEXT PRIMARY KEY, import_id TEXT NOT NULL REFERENCES imports(id),
 person_key TEXT NOT NULL, account_key TEXT NOT NULL, scope TEXT NOT NULL,
 entity_id TEXT NOT NULL, position INTEGER NOT NULL, label TEXT,
 observed_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS coverage(
 id TEXT PRIMARY KEY, import_id TEXT NOT NULL REFERENCES imports(id),
 person_key TEXT NOT NULL, source TEXT NOT NULL, account_key TEXT, device_key TEXT,
 scope TEXT NOT NULL, status TEXT NOT NULL, transport_status TEXT NOT NULL,
 observed_at_utc TEXT NOT NULL, min_event_at_utc TEXT, max_event_at_utc TEXT,
 record_count INTEGER NOT NULL, detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS platform_reports(
 id TEXT PRIMARY KEY, import_id TEXT NOT NULL REFERENCES imports(id),
 person_key TEXT NOT NULL, account_key TEXT NOT NULL, report_kind TEXT NOT NULL,
 observed_at_utc TEXT NOT NULL, status TEXT NOT NULL,
 period_start_utc TEXT, period_end_utc TEXT,
 normalized_json TEXT NOT NULL, payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS platform_reports_lookup
 ON platform_reports(account_key,report_kind,observed_at_utc);
CREATE TABLE IF NOT EXISTS song_tags(
 id TEXT PRIMARY KEY, import_id TEXT NOT NULL REFERENCES imports(id),
 person_key TEXT NOT NULL, account_key TEXT NOT NULL, source TEXT NOT NULL,
 platform_track_id TEXT NOT NULL, observed_at_utc TEXT NOT NULL,
 status TEXT NOT NULL, normalized_json TEXT NOT NULL, payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS song_tags_lookup
 ON song_tags(source,platform_track_id,observed_at_utc,import_id,id);
CREATE TABLE IF NOT EXISTS changes(
 seq INTEGER PRIMARY KEY AUTOINCREMENT, entity_type TEXT NOT NULL,
 entity_id TEXT NOT NULL, operation TEXT NOT NULL,
 import_id TEXT NOT NULL REFERENCES imports(id), changed_at_utc TEXT NOT NULL
);
"""


class Workspace:
    def __init__(self, home: str | Path):
        self.home = Path(home).resolve()
        self.raw = self.home / "raw"
        self.data = self.home / "data"
        self.db = self.data / "music.sqlite"

    def initialize(self):
        self.data.mkdir(parents=True, exist_ok=True)
        self.raw.mkdir(parents=True, exist_ok=True)
        (self.home / ".local").mkdir(parents=True, exist_ok=True)
        with self.lock("initialize"), closing(self.connect()) as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2, 3):
                raise MusicError(f"不支持数据库结构版本 {version}，请使用匹配的程序。")
            conn.executescript(SCHEMA)
            conn.execute("INSERT OR IGNORE INTO meta VALUES('database_id',?)", (uuid.uuid4().hex,))
            conn.execute("INSERT OR REPLACE INTO meta VALUES('schema_version',?)", (SCHEMA_VERSION,))
            conn.execute("PRAGMA user_version=3")
            conn.commit()
        return self

    @contextmanager
    def lock(self, name):
        directory = self.home / ".local"
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / (name + ".lock")).open("a+b") as handle:
            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise WorkspaceBusy(f"已有 {name} 作业运行，请等待完成后重试。") from exc
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle, fcntl.LOCK_UN)

    def connect(self, readonly=False):
        if readonly:
            if not self.db.is_file():
                raise MusicError("资料库尚未建立，请先运行 init。")
            conn = sqlite3.connect(self.db.as_uri() + "?mode=ro", uri=True, timeout=30)
            conn.execute("PRAGMA query_only=ON")
        else:
            conn = sqlite3.connect(self.db, timeout=30)
            conn.execute("PRAGMA journal_mode=DELETE")
            conn.execute("PRAGMA synchronous=FULL")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @contextmanager
    def transaction(self):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def archive(self, payload: bytes, *, kind, person, device=None, stream=None,
                account=None, collected_at=None, settings=None):
        with self.lock("archive"):
            return self._archive(payload, kind=kind, person=person, device=device, stream=stream,
                                 account=account, collected_at=collected_at, settings=settings)

    def _archive(self, payload: bytes, *, kind, person, device=None, stream=None,
                 account=None, collected_at=None, settings=None):
        payload_hash = digest(payload)
        identity = dict(kind=kind, person_key=person, device_key=device, stream_name=stream,
                        account_key=account, collected_at=collected_at, settings=settings or {})
        capture_id = stable_id(identity, payload_hash)
        path = self.raw / "captures" / (capture_id + ".json")
        if path.exists():
            manifest, original = self.read_capture(path)
            if original != payload:
                raise MusicError("已存在的原始归档与输入不符。")
            return manifest
        object_path = self.raw / "objects" / (payload_hash + ".bin")
        if object_path.exists():
            if digest(object_path.read_bytes()) != payload_hash:
                raise MusicError("原始归档哈希错误，停止覆盖。")
        else:
            atomic_write(object_path, payload)
        manifest = dict(format="collector-music-capture", version=1, id=capture_id,
                        **identity, observed_at_utc=collected_at or now(),
                        archived_at_utc=now(), payload_sha256=payload_hash,
                        payload_bytes=len(payload), payload_path=f"objects/{payload_hash}.bin",
                        raw_ref=f"captures/{capture_id}.json")
        atomic_write(path, json_bytes(manifest))
        return manifest

    def read_capture(self, path: Path):
        manifest = parse_json(path.read_bytes())
        if manifest.get("format") != "collector-music-capture" or manifest.get("version") != 1:
            raise MusicError("不支持的原始归档清单。")
        payload = safe_child(self.raw, manifest["payload_path"]).read_bytes()
        if digest(payload) != manifest["payload_sha256"] or len(payload) != manifest["payload_bytes"]:
            raise MusicError(f"原始归档校验失败：{path.name}")
        identity = {k: manifest[k] for k in ("kind", "person_key", "device_key", "stream_name",
                                            "account_key", "collected_at", "settings")}
        if stable_id(identity, manifest["payload_sha256"]) != manifest["id"]:
            raise MusicError("原始归档身份校验失败。")
        if manifest["raw_ref"] != f"captures/{manifest['id']}.json":
            raise MusicError("原始归档引用不匹配。")
        return manifest, payload


def insert_row(conn, table, values):
    columns = list(values)
    conn.execute(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                 [values[c] for c in columns])


def begin_import(conn, manifest):
    row = conn.execute("SELECT * FROM imports WHERE id=?", (manifest["id"],)).fetchone()
    if row:
        return dict(row)
    insert_row(conn, "imports", dict(id=manifest["id"], kind=manifest["kind"],
        person_key=manifest["person_key"], device_key=manifest["device_key"],
        stream_name=manifest["stream_name"], account_key=manifest["account_key"],
        observed_at_utc=manifest["observed_at_utc"], ingested_at_utc=now(),
        payload_sha256=manifest["payload_sha256"], raw_ref=manifest["raw_ref"],
        parser_version=PARSER_VERSION, status="pending", inserted_records=0, pending_bytes=0))
    return None


def change(conn, kind, key, operation, capture_id):
    insert_row(conn, "changes", dict(entity_type=kind, entity_id=key, operation=operation,
                                     import_id=capture_id, changed_at_utc=now()))


def finish_import(conn, manifest, count, pending=0, status="imported"):
    conn.execute("UPDATE imports SET status=?,inserted_records=?,pending_bytes=? WHERE id=?",
                 (status, count, pending, manifest["id"]))
    change(conn, "import", manifest["id"], status, manifest["id"])
    return dict(import_id=manifest["id"], status=status, inserted_records=count,
                pending_bytes=pending, raw_ref=manifest["raw_ref"])
