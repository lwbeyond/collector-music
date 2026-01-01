from __future__ import annotations

import json
import os
import tempfile
import uuid
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import duckdb

from . import METRIC_VERSION, SCHEMA_VERSION
from .common import MusicError, atomic_write, digest, identifier, json_bytes, now, parse_json, safe_child
from .store import Workspace
from ._retention import best_effort, checked
from .retention import prune_publications

LEGACY_TABLES = ("imports", "observations", "observation_evidence", "ranking_items", "library_items", "coverage", "changes")
TABLES_BY_VERSION = {
    "1.0.0": LEGACY_TABLES,
    "1.1.0": LEGACY_TABLES + ("platform_reports",),
    "1.2.0": LEGACY_TABLES + ("platform_reports", "song_tags"),
}
TABLES = TABLES_BY_VERSION[SCHEMA_VERSION]
DAILY_SCHEMA = [
    ("person_key", "VARCHAR"), ("source", "VARCHAR"), ("device_key", "VARCHAR"),
    ("account_key", "VARCHAR"), ("date", "VARCHAR"), ("timezone", "VARCHAR"),
    ("record_count", "BIGINT"), ("unique_recording_keys", "BIGINT"),
    ("night_record_count", "BIGINT"), ("actual_listened_seconds", "DOUBLE"),
    ("coverage_status", "VARCHAR"), ("identity_basis", "VARCHAR"),
    ("time_basis", "VARCHAR"), ("metric_version", "VARCHAR"),
]


def quote(value):
    return '"' + value.replace('"', '""') + '"'


def daily_rows(observations, timezone_name="Asia/Shanghai"):
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise MusicError("未知分析时区。") from exc
    groups = {}
    for row in observations:
        if row["event_kind"] not in ("scrobble", "recent_observation") or row["occurred_at_utc"] is None:
            continue
        date = datetime.fromisoformat(row["occurred_at_utc"]).astimezone(zone)
        key = (row["person_key"], row["source"], row["device_key"], row["account_key"], date.date().isoformat())
        item = groups.setdefault(key, {"count": 0, "recordings": set(), "night": 0})
        item["count"] += 1
        item["recordings"].add(row["recording_key"])
        item["night"] += date.hour < 6
    output = []
    for key, item in sorted(groups.items(), key=lambda pair: tuple(x or "" for x in pair[0])):
        output.append((*key, timezone_name, item["count"], len(item["recordings"]), item["night"], None,
            "observed_only", "source_labels" if key[1] == "pano" else "platform_track_id",
            "collector_play_start" if key[1] == "pano" else "upstream_playTime_unverified", METRIC_VERSION))
    return output


def create_table(conn, name, schema, rows, *, staging_directory=None):
    conn.execute(f"CREATE TABLE {quote(name)} ({','.join(quote(c)+' '+t for c,t in schema)})")
    if rows:
        if staging_directory is None:
            conn.executemany(f"INSERT INTO {quote(name)} VALUES ({','.join('?' for _ in schema)})", rows)
            return
        # Native typed JSON loading avoids Python parameter binding for each cell.
        # An explicit schema preserves BIGINT precision, NULL and text verbatim.
        path = Path(staging_directory) / (uuid.uuid4().hex + ".jsonl")
        columns = [column for column, _ in schema]
        maximum_object_size = 16 * 1024 * 1024
        created = False
        try:
            with path.open("xb") as output:
                created = True
                for row in rows:
                    if len(row) != len(columns):
                        raise MusicError(f"{name} 发布行字段数量不匹配。")
                    encoded = (json.dumps(dict(zip(columns, row)), ensure_ascii=False,
                        allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")
                    maximum_object_size = max(maximum_object_size, len(encoded))
                    output.write(encoded)
            conn.read_json(str(path), columns=dict(schema), format="newline_delimited",
                maximum_object_size=maximum_object_size).insert_into(name)
        finally:
            if created:
                path.unlink(missing_ok=True)


def publish(workspace: Workspace, timezone_name="Asia/Shanghai"):
    workspace.initialize()
    with workspace.lock("publish"):
        return _publish(workspace, timezone_name)


def _publish(workspace, timezone_name):
    generation = datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:12]
    lake = workspace.data / "lake"
    previous = resolve_version(lake) if (lake / "current.json").exists() else None
    destination = lake / "versions" / generation
    destination.mkdir(parents=True, exist_ok=False)
    datasets = {}
    temporary = workspace.home / ".tmp"
    temporary.mkdir(exist_ok=True)
    if temporary.resolve().parent != workspace.home:
        raise MusicError("发布临时目录必须位于当前资料目录内。")
    # Holding a SQLite read transaction pins all exported tables to one revision.
    with tempfile.TemporaryDirectory(prefix="publish-", dir=temporary) as staging_directory, \
            closing(workspace.connect(readonly=True)) as source, duckdb.connect() as target:
        source.execute("BEGIN")
        database_id = source.execute("SELECT value FROM meta WHERE key='database_id'").fetchone()[0]
        revision = source.execute("SELECT coalesce(max(seq),0) FROM changes").fetchone()[0]
        observations = []
        for table in TABLES:
            columns = source.execute(f"PRAGMA table_info({table})").fetchall()
            schema = [(c["name"], {"INTEGER": "BIGINT", "REAL": "DOUBLE", "TEXT": "VARCHAR"}[c["type"]]) for c in columns]
            rows = source.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            if table == "observations":
                observations = [dict(r) for r in rows]
            create_table(target, table, schema, [tuple(r) for r in rows], staging_directory=staging_directory)
            datasets[table] = {"schema": [{"name": c, "type": t} for c, t in schema], "rows": len(rows)}
        daily = daily_rows(observations, timezone_name)
        create_table(target, "daily_music_features", DAILY_SCHEMA, daily, staging_directory=staging_directory)
        datasets["daily_music_features"] = {"schema": [{"name": c, "type": t} for c,t in DAILY_SCHEMA], "rows": len(daily)}
        for table, metadata in datasets.items():
            path = destination / (table + ".parquet")
            target.execute(f"COPY {quote(table)} TO ? (FORMAT PARQUET, COMPRESSION ZSTD)", [str(path)])
            count = target.execute("SELECT count(*) FROM read_parquet(?)", [str(path)]).fetchone()[0]
            if count != metadata["rows"]:
                raise MusicError(f"{table} 发布读回数量不符，旧快照保留。")
            metadata.update(path=path.name, sha256=digest(path.read_bytes()))
        source.rollback()
    manifest = dict(format="collector-music-lake", version=1, generation=generation,
        database_id=database_id, data_revision=revision, schema_version=SCHEMA_VERSION,
        metric_version=METRIC_VERSION, published_at_utc=now(), analysis_timezone=timezone_name,
        coverage="observed_only", datasets=datasets)
    manifest_bytes = json_bytes(manifest)
    atomic_write(destination / "manifest.json", manifest_bytes)
    # Verify every table and schema before publishing the pointer.
    verify_version(destination)
    if previous is not None:
        os.utime(checked(workspace.home, previous), None)
    atomic_write(lake / "current.json", json_bytes(dict(generation=generation,
        manifest_path=f"versions/{generation}/manifest.json", manifest_sha256=digest(manifest_bytes))))
    best_effort(prune_publications, workspace)
    return {"generation": generation, "data_revision": revision, "datasets": {k:v["rows"] for k,v in datasets.items()}}


def resolve_version(lake: Path, generation=None):
    lake = lake.resolve()
    if generation:
        identifier(generation, "generation")
        path = lake / "versions" / generation
        manifest = parse_json((path / "manifest.json").read_bytes())
        if manifest.get("generation") != generation:
            raise MusicError("快照 generation 不一致。")
        return path
    pointer = parse_json((lake / "current.json").read_bytes())
    identifier(pointer.get("generation"), "generation")
    expected_path = f"versions/{pointer['generation']}/manifest.json"
    if pointer.get("manifest_path") != expected_path:
        raise MusicError("快照指针路径不匹配。")
    path = safe_child(lake, expected_path)
    if digest(path.read_bytes()) != pointer["manifest_sha256"]:
        raise MusicError("当前快照清单校验失败。")
    return path.parent


def verify_version(directory: Path):
    manifest = parse_json((directory / "manifest.json").read_bytes())
    if manifest.get("format") != "collector-music-lake" or manifest.get("version") != 1:
        raise MusicError("不支持的分析快照版本。")
    if manifest.get("schema_version") not in TABLES_BY_VERSION or manifest.get("metric_version") != METRIC_VERSION:
        raise MusicError("不支持的数据或指标契约版本。")
    expected_tables = TABLES_BY_VERSION[manifest["schema_version"]]
    if set(manifest["datasets"]) != set(expected_tables) | {"daily_music_features"}:
        raise MusicError("快照数据集清单不完整。")
    with duckdb.connect() as conn:
        for name, item in manifest["datasets"].items():
            if item["path"] != name + ".parquet":
                raise MusicError("快照文件名不匹配。")
            path = safe_child(directory, item["path"])
            if digest(path.read_bytes()) != item["sha256"]:
                raise MusicError(f"{name} Parquet 哈希不匹配。")
            relation = conn.read_parquet(str(path))
            actual_schema = [{"name": c, "type": str(t)} for c,t in zip(relation.columns, relation.types)]
            if actual_schema != item["schema"] or relation.count("*").fetchone()[0] != item["rows"]:
                raise MusicError(f"{name} Parquet 字段或计数不匹配。")
    return manifest


def query(lake: Path, sql: str, generation=None, limit=1000):
    if not 1 <= limit <= 10000:
        raise MusicError("查询 limit 应在 1–10000 之间。")
    directory = resolve_version(lake, generation)
    manifest = verify_version(directory)
    with duckdb.connect() as conn:
        # Materialize verified files, then close external access for user SQL.
        for name, item in manifest["datasets"].items():
            conn.execute(f"CREATE TABLE {quote(name)} AS SELECT * FROM read_parquet(?)",
                         [str(directory / item["path"])])
        conn.execute("SET enable_external_access=false")
        statements = conn.extract_statements(sql)
        if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
            raise MusicError("query 只接受一条 SELECT/WITH 查询。")
        result = conn.execute(sql)
        columns = [col[0] for col in result.description]
        if len(set(columns)) != len(columns):
            raise MusicError("查询结果列名重复，请给列指定唯一别名。")
        rows = result.fetchmany(limit+1)
        return dict(generation=manifest["generation"], data_revision=manifest["data_revision"],
                    schema_version=manifest["schema_version"], coverage=manifest["coverage"],
                    items=[dict(zip(columns,r)) for r in rows[:limit]], truncated=len(rows)>limit, limit=limit)
