from __future__ import annotations

import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

from .common import MusicError, now
from .netease import ingest_netease
from .pano import ingest_pano
from .store import Workspace


def status(workspace):
    with closing(workspace.connect(readonly=True)) as conn:
        revision = conn.execute("SELECT coalesce(max(seq),0) FROM changes").fetchone()[0]
        return dict(schema_version=conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0],
            database_id=conn.execute("SELECT value FROM meta WHERE key='database_id'").fetchone()[0],
            data_revision=revision, sqlite_version=sqlite3.sqlite_version,
            journal_mode=conn.execute("PRAGMA journal_mode").fetchone()[0],
            observations=[dict(r) for r in conn.execute("SELECT source,device_key,event_kind,count(*) AS records FROM observations GROUP BY source,device_key,event_kind")],
            imports=[dict(r) for r in conn.execute("SELECT kind,status,count(*) AS batches FROM imports GROUP BY kind,status")],
            streams=[dict(r) for r in conn.execute("SELECT person_key,device_key,stream_name,committed_bytes FROM streams")],
            coverage="observed_only", actual_listened_seconds=None)


def backup(workspace, output: Path):
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive reservation prevents overwriting any existing backup or live database.
    with output.open("xb"):
        pass
    with closing(workspace.connect(readonly=True)) as source, closing(sqlite3.connect(output)) as target:
        source.backup(target)
        if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise MusicError("备份完整性检查失败，输出已保留供检查。")
    return dict(path=str(output), created_at_utc=now(), includes_raw=False,
                note="此文件是 SQLite 一致性备份；完整恢复还需 raw 和私有配置。")


def verify(workspace):
    captures = list((workspace.raw / "captures").glob("*.json"))
    for path in captures:
        workspace.read_capture(path)
    with closing(workspace.connect(readonly=True)) as conn:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise MusicError("SQLite 完整性检查失败。")
        if conn.execute("PRAGMA foreign_key_check").fetchall():
            raise MusicError("SQLite 外键检查失败。")
        for row in conn.execute("SELECT raw_ref FROM imports"):
            workspace.read_capture(workspace.raw / row[0])
    return dict(sqlite="ok", archive="ok", captures=len(captures))


def replay(workspace, destination: Path):
    destination = destination.resolve()
    if destination == workspace.home or destination.is_relative_to(workspace.raw):
        raise MusicError("恢复目标必须是独立的新目录。")
    if destination.exists():
        raise MusicError("恢复目标已存在；请指定尚不存在的目录。")
    manifests = []
    for path in (workspace.raw / "captures").glob("*.json"):
        manifest, payload = workspace.read_capture(path)
        manifests.append((manifest, payload))
    # Pano raw input is cumulative; increasing prefix size handles archive arrival order.
    # NetEase observations retain their first received payload/provenance. A late
    # historical snapshot must not win that payload during recovery merely because
    # its upstream capture timestamp is earlier.
    manifests.sort(key=lambda pair: (pair[0]["kind"], pair[0]["person_key"],
        pair[0]["device_key"] or "", pair[0]["stream_name"] or "",
        len(pair[1]) if pair[0]["kind"] == "pano" else 0,
        pair[0]["observed_at_utc"] if pair[0]["kind"] == "pano" else pair[0]["archived_at_utc"],
        pair[0]["id"]))
    destination.mkdir(parents=True, exist_ok=False)
    shutil.copytree(workspace.raw, destination / "raw")
    restored = Workspace(destination).initialize()
    results = []
    for manifest, payload in manifests:
        ingester = {"pano": ingest_pano, "netease": ingest_netease}.get(manifest["kind"])
        if manifest["kind"] == "netease_reports":
            from .platform_reports import ingest_reports
            ingester = ingest_reports
        if manifest["kind"] == "netease_song_tags":
            from .song_tags import ingest_song_tags
            ingester = ingest_song_tags
        if not ingester:
            raise MusicError("归档中存在不支持的来源类型。")
        try:
            result = ingester(restored, manifest, payload)
        except MusicError as exc:
            results.append(dict(import_id=manifest["id"], status="rejected", reason=str(exc)))
        else:
            results.append(result)
    verify(restored)
    return dict(destination=str(destination), imports=results, rejected=sum(r["status"]=="rejected" for r in results),
                note="只重放来源事实，新的变更序号和入库时间不等同旧库；人工修订需数据库备份。")
