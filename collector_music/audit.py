"""Local integrity and replay audit; never fetches data or mutates source facts."""
from __future__ import annotations

import hashlib
import tempfile
from contextlib import closing
from pathlib import Path

import duckdb

from .common import MusicError, digest, json_text, now
from .lake import TABLES, resolve_version, verify_version
from .maintenance import replay, verify
from .store import Workspace


def fingerprint(rows):
    result = hashlib.sha256()
    count = 0
    for row in rows:
        result.update((json_text(list(row)) + "\n").encode("utf-8"))
        count += 1
    return dict(rows=count, sha256=result.hexdigest())


def audit(workspace: Workspace, recovery_check=False):
    integrity = verify(workspace)
    directory = resolve_version(workspace.data / "lake")
    manifest = verify_version(directory)
    warnings = []
    with closing(workspace.connect(readonly=True)) as conn:
        conn.execute("BEGIN")
        revision = conn.execute("SELECT coalesce(max(seq),0) FROM changes").fetchone()[0]
        database_id = conn.execute("SELECT value FROM meta WHERE key='database_id'").fetchone()[0]
        database_schema = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        if manifest["database_id"] != database_id:
            raise MusicError("分析快照来自另一资料库，不能据此分析当前数据。")
        fresh = revision == manifest["data_revision"]
        if not fresh:
            warnings.append("当前发布快照落后于资料库；先 publish 再开始新的分析。")
        if manifest["schema_version"] != database_schema:
            warnings.append("当前发布仍使用旧数据契约；请 publish 以包含新增数据集。")
        table_checks = {}
        if fresh:
            with duckdb.connect() as lake:
                for table in TABLES:
                    if table not in manifest["datasets"]:
                        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", [table]).fetchone()
                        if exists and conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]:
                            raise MusicError(f"{table} 已有记录，但同修订发布缺少该数据集。")
                        continue
                    left = fingerprint(conn.execute(f'SELECT * FROM "{table}" ORDER BY 1'))
                    right = fingerprint(lake.execute("SELECT * FROM read_parquet(?) ORDER BY 1",
                                                     [str(directory / f"{table}.parquet")]).fetchall())
                    if left != right:
                        raise MusicError(f"{table} 已发布内容与同修订 SQLite 不一致。")
                    table_checks[table] = left
        for stream in conn.execute("SELECT * FROM streams"):
            capture, payload = workspace.read_capture(workspace.raw / stream["last_raw_ref"])
            end = stream["committed_bytes"]
            if end > len(payload) or end < 0 or digest(payload[:end]) != stream["prefix_sha256"] or (end and payload[end-1:end] != b"\n"):
                raise MusicError("设备日志水位/前缀校验不一致，停止分析验收。")
            if any(capture[k] != stream[k] for k in ("person_key", "device_key", "stream_name")):
                raise MusicError("设备日志实例与其原件身份不一致。")
        observations = [dict(row) for row in conn.execute("""SELECT source,event_kind,count(*) AS records,
            count(track_duration_ms) AS duration_known,count(actual_listened_seconds) AS actual_seconds_known,
            min(occurred_at_utc) AS first_event_at,max(occurred_at_utc) AS last_event_at
            FROM observations GROUP BY source,event_kind ORDER BY source,event_kind""")]
        if any(row["actual_seconds_known"] for row in observations):
            raise MusicError("当前来源契约不支持实际聆听时长，发现非空值需要调查。")
        coverage = [dict(row) for row in conn.execute("""WITH ranked AS (
            SELECT *,row_number() OVER (PARTITION BY person_key,source,account_key,device_key,scope
                ORDER BY observed_at_utc DESC,import_id DESC) AS rn FROM coverage)
            SELECT source,device_key,scope,status,transport_status,record_count,observed_at_utc
            FROM ranked WHERE rn=1 ORDER BY source,scope,device_key""")]
        for row in coverage:
            if row["transport_status"] in ("failed", "partial", "pending_tail"):
                warnings.append(f"{row['source']}/{row['scope']} 最近一次状态为 {row['transport_status']}。")
        report_attempts = []
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='platform_reports'").fetchone():
            report_attempts = [dict(row) for row in conn.execute("""WITH ranked AS (
                SELECT *,row_number() OVER (PARTITION BY person_key,account_key,report_kind
                    ORDER BY observed_at_utc DESC,import_id DESC,id DESC) AS rn FROM platform_reports)
                SELECT report_kind,status,observed_at_utc,period_start_utc,period_end_utc
                FROM ranked WHERE rn=1 ORDER BY report_kind""")]
            for row in report_attempts:
                if row["status"] == "failed":
                    warnings.append(f"网易云平台报告/{row['report_kind']} 最近一次采集或解析失败。")
        tag_status = []
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='song_tags'").fetchone():
            tag_status = [dict(row) for row in conn.execute("""WITH ranked AS (
                SELECT source,status,row_number() OVER (PARTITION BY source,platform_track_id
                    ORDER BY observed_at_utc DESC,import_id DESC,id DESC) AS rn FROM song_tags)
                SELECT source,status,count(*) AS tracks FROM ranked WHERE rn=1
                GROUP BY source,status ORDER BY source,status""")]
            for row in tag_status:
                if row["status"] == "failed":
                    warnings.append(f"{row['source']} 歌曲标签有 {row['tracks']} 首最近一次采集或解析失败。")
        conn.rollback()
    restored_checks = None
    if recovery_check:
        temporary = workspace.home / ".tmp"
        temporary.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="analysis-audit-", dir=temporary) as name:
            root = Path(name).resolve()
            if root.parent != temporary.resolve():
                raise MusicError("恢复演练目录不在指定临时目录内。")
            result = replay(workspace, root / "restored")
            if result["rejected"]:
                raise MusicError("恢复演练存在被拒绝原件，详查归档后再用于分析。")
            restored = Workspace(root / "restored")
            restored_checks = {}
            with closing(workspace.connect(readonly=True)) as original, closing(restored.connect(readonly=True)) as recovered:
                original.execute("BEGIN")
                if original.execute("SELECT coalesce(max(seq),0) FROM changes").fetchone()[0] != revision:
                    raise MusicError("验收期间发生新导入，请在采集结束后重跑 audit。")
                for table in ("observations", "observation_evidence", "ranking_items", "library_items",
                              "platform_reports", "song_tags", "streams", "source_accounts"):
                    if not original.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", [table]).fetchone():
                        continue
                    columns = [r["name"] for r in original.execute(f'PRAGMA table_info("{table}")')
                               if r["name"] != "ingested_at_utc"]
                    fields = ",".join('"' + c + '"' for c in columns)
                    ordering = ",".join(str(i+1) for i in range(len(columns)))
                    sql = f'SELECT {fields} FROM "{table}" ORDER BY {ordering}'
                    left, right = fingerprint(original.execute(sql)), fingerprint(recovered.execute(sql))
                    if left != right:
                        raise MusicError(f"恢复演练中 {table} 的来源事实与原库不一致。")
                    restored_checks[table] = left
                original.rollback()
    return dict(checked_at_utc=now(), status="warning" if warnings else "ok", integrity=integrity,
        generation=manifest["generation"], data_revision=revision, publication_current=fresh,
        published_table_checks=table_checks, replay_checked=recovery_check, replay_fact_checks=restored_checks,
        observations=observations, latest_coverage=coverage, latest_platform_reports=report_attempts,
        latest_song_tag_status=tag_status, warnings=warnings,
        limits=["Pano 与平台近期观测分别分析；跨设备/来源未做事件合并。",
                "设备连续后台覆盖未验证，无记录日期不代表零播放。",
                "曲长不等于实际聆听时长；热评、歌曲红心总数及歌单成员尚未进入正式事实表。",
                "平台报告是独立汇总，时长原值单位未核实，不填入逐次观测的实际聆听时长。",
                "歌曲标签沿用平台音乐百科，缺失不猜测；不代表听者情绪，也不生成播放事实。",
                "关系和榜单需取每账号每范围的最新成功快照；历史快照不能直接累计。"])
