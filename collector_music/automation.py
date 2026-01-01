"""Finite, restartable scheduler tick. No device/USB access and no external messages."""
from __future__ import annotations

import os
from contextlib import closing
from datetime import datetime, timedelta, timezone

from .audit import audit
from . import SCHEMA_VERSION
from .collector import collect, collect_reports, record_report_failure
from .common import LoginRequired, MusicError, WorkspaceBusy, atomic_write, digest, json_bytes, json_text, now, parse_json
from .lake import TABLES, publish, resolve_version
from .maintenance import backup
from .tag_collection import collect_song_tags, plan_song_tags

INTERVALS = {"recent": 10800, "library": 86400, "reports": 86400, "tags": 900, "backup": 86400, "audit": 604800}
MINIMUMS = {"recent": 3600, "library": 21600, "reports": 21600, "tags": 900, "backup": 86400, "audit": 86400}
SCOPES = {"recent": ("recent",), "library": ("likes", "playlists", "week", "all"), "reports": ()}


def timestamp(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.utcoffset() is None:
        raise MusicError("调度时间必须带时区。")
    return result.astimezone(timezone.utc)


def stamp(value):
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def session_fingerprint(workspace):
    path = workspace.home / ".local/session.json"
    return digest(path.read_bytes()) if path.exists() else "missing"


def revision(workspace):
    with closing(workspace.connect(readonly=True)) as connection:
        connection.execute("BEGIN")
        identity = connection.execute("SELECT value FROM meta WHERE key='database_id'").fetchone()[0]
        number = connection.execute("SELECT coalesce(max(seq),0) FROM changes").fetchone()[0]
        return dict(database_id=identity, data_revision=number)


def ensure_publication(workspace):
    current = revision(workspace)
    lake = workspace.data / "lake"
    if (lake / "current.json").exists():
        manifest = parse_json((resolve_version(lake) / "manifest.json").read_bytes())
        if (all(manifest.get(key) == value for key, value in current.items())
                and manifest.get("schema_version") == SCHEMA_VERSION
                and set(manifest.get("datasets", {})) == set(TABLES) | {"daily_music_features"}):
            return dict(status="unchanged", **current)
    return dict(status="published", **publish(workspace))


def run_tick(workspace, config_path=None):
    """One tick; overdue jobs run once, with persistent deadlines and backoff."""
    path = config_path or workspace.home / ".local/automation.json"
    config = parse_json(path.read_bytes())
    if config.get("version") != 1 or not isinstance(config.get("enabled"), bool):
        raise MusicError("自动化配置版本或 enabled 不符。")
    if not config["enabled"]:
        return dict(status="disabled")
    intervals = dict(INTERVALS)
    overrides = config.get("interval_seconds", {})
    if not isinstance(overrides, dict) or not set(overrides).issubset(intervals):
        raise MusicError("自动化仅支持近期、收藏榜单、平台报告、增量歌曲标签、备份和审计；没有设备接收作业。")
    for name, value in overrides.items():
        if type(value) is not int or not MINIMUMS[name] <= value <= 31 * 86400:
            raise MusicError("自动化间隔超出支持范围。")
        intervals[name] = value
    workspace.initialize()
    with workspace.lock("automation"):
        directory = workspace.home / ".local/automation"
        directory.mkdir(exist_ok=True)
        state_path = directory / "state.json"
        state = parse_json(state_path.read_bytes()) if state_path.exists() else dict(version=1, jobs={})
        if state.get("version") != 1 or not isinstance(state.get("jobs"), dict):
            raise MusicError("调度状态格式不符；保留状态文件以便检查。")
        jobs = state["jobs"]
        started = timestamp(now())
        credential = session_fingerprint(workspace)
        outcomes = {}

        def save():
            state["checked_at_utc"] = now()
            atomic_write(state_path, json_bytes(state))

        def record(name, status, *, detail=None, worked=False):
            previous = jobs.get(name, {})
            item = dict(previous, status=status, last_attempt_at_utc=now())
            if detail is not None:
                item["detail"] = detail
            healthy = status in ("ok", "unchanged", "published", "pending")
            deferred = status == "deferred"
            item["failures"] = (previous.get("failures", 0) if deferred else
                                0 if healthy else previous.get("failures", 0) + 1)
            if healthy:
                item["last_success_at_utc"] = now()
            interval = intervals.get(name, 900)
            delay = 60 if deferred else interval if healthy else min(43200, 1800 * 2 ** min(item["failures"] - 1, 5))
            item["next_due_at_utc"] = stamp(timestamp(now()) + timedelta(seconds=delay))
            jobs[name] = item
            # Unchanged idle ticks stay quiet; work, failure transitions and recovery are recorded.
            if worked or status != previous.get("status") or detail != previous.get("detail"):
                event = dict(at_utc=now(), job=name, status=status, detail=detail)
                with (directory / "events.jsonl").open("a", encoding="utf-8") as output:
                    output.write(json_text(event) + "\n")
            outcomes[name] = dict(status=status, next_due_at_utc=item["next_due_at_utc"])
            save()

        def failed(name, error):
            message = str(error) if isinstance(error, MusicError) else f"{type(error).__name__}，本轮失败，保留数据并退避重试。"
            record(name, "failed", detail=message)

        def due(name):
            value = jobs.get(name, {}).get("next_due_at_utc")
            return value is None or timestamp(value) <= started

        def configure_node():
            node = config.get("node_executable")
            if node:
                from pathlib import Path
                if not Path(node).is_absolute() or not Path(node).is_file():
                    raise MusicError("配置的 Node 路径不可用。")
                os.environ["MUSIC_NODE_EXE"] = node

        paused = state.get("login_pause")
        if paused and paused.get("session_fingerprint") != credential:
            state.pop("login_pause")
            for name in (*SCOPES, "tags"):
                jobs.setdefault(name, {}).pop("next_due_at_utc", None)
            save()

        for name, scopes in SCOPES.items():
            if state.get("login_pause"):
                pause = state["login_pause"]
                if name == "reports" and not pause.get("reports_failure_recorded"):
                    try:
                        with workspace.lock("collect"):
                            record_report_failure(workspace, person=config.get("person_key", "me"),
                                observed_at=pause["since_utc"], needs_login=True, failure_stage="login_pause")
                        pause["reports_failure_recorded"] = True
                        save()
                    except WorkspaceBusy:
                        record(name, "deferred", detail="其他采集正在运行，稍后继续。")
                        continue
                    except Exception as error:
                        failed(name, error)
                        continue
                if jobs.get(name, {}).get("status") != "login_required":
                    record(name, "login_required", detail="等待重新扫码；会话文件更新后自动恢复。")
                continue
            if not due(name):
                continue
            report_failure_recorded = False
            try:
                configure_node()
                with workspace.lock("collect"):
                    if name == "reports":
                        result = collect_reports(workspace, person=config.get("person_key", "me"), delay=3)
                    else:
                        result = collect(workspace, person=config.get("person_key", "me"), scopes=scopes, delay=3)
                if result.get("needs_login"):
                    report_failure_recorded = name == "reports" and ("reports" in result or result.get("status") == "duplicate")
                    raise LoginRequired("网易云登录已失效，请重新扫码。")
                detail = dict(scopes=result.get("reports") if name == "reports" else result.get("scopes"),
                    inserted_records=result.get("inserted_records", 0))
                record(name, "warning" if result.get("status") == "partial" else "ok", detail=detail, worked=True)
            except LoginRequired:
                state["login_pause"] = dict(session_fingerprint=credential, since_utc=now(),
                    reports_failure_recorded=report_failure_recorded)
                record(name, "login_required", detail="等待重新扫码；会话文件更新后自动恢复。")
            except WorkspaceBusy:
                record(name, "deferred", detail="其他采集正在运行，稍后继续。")
            except Exception as error:
                failed(name, error)

        # Discover new IDs after core collection, but never rescan successful empty labels.
        if state.get("login_pause"):
            if jobs.get("tags", {}).get("status") != "login_required":
                record("tags", "login_required", detail="等待重新扫码；会话文件更新后自动恢复。")
        elif due("tags"):
            try:
                plan = plan_song_tags(workspace, person=config.get("person_key", "me"))
                if not plan['pending']:
                    record("tags", "unchanged", detail=dict(remaining=0))
                else:
                    configure_node()
                    result = collect_song_tags(workspace, person=config.get("person_key", "me"), limit=20, delay=3)
                    detail = {key: result.get(key) for key in ('attempted', 'ok', 'empty', 'failed', 'remaining', 'stop_reason')}
                    if result['status'] == 'stopped' and result['stop_reason'] == 'login_required':
                        state['login_pause'] = dict(session_fingerprint=credential, since_utc=now(), reports_failure_recorded=False)
                        record('tags', 'login_required', detail=detail)
                    else:
                        status = {'complete': 'ok', 'limited': 'pending', 'deferred': 'deferred'}.get(result['status'], 'warning')
                        record('tags', status, detail=detail, worked=bool(result.get('attempted')))
            except WorkspaceBusy:
                record('tags', 'deferred', detail="其他标签补采正在运行，稍后继续。")
            except LoginRequired:
                state['login_pause'] = dict(session_fingerprint=credential, since_utc=now(), reports_failure_recorded=False)
                record('tags', 'login_required', detail="等待重新扫码；会话文件更新后自动恢复。")
            except Exception as error:
                failed('tags', error)

        # Publication also repairs an import that committed just before an interrupted tick.
        # A platform failure never prevents publishing separately received local data.
        if due("publication"):
            try:
                result = ensure_publication(workspace)
                record("publication", result["status"], detail=revision(workspace), worked=result["status"] == "published")
            except Exception as error:
                failed("publication", error)

        if due("backup"):
            try:
                current = revision(workspace)
                if jobs.get("backup", {}).get("saved_revision") == current:
                    record("backup", "unchanged")
                else:
                    filename = "music-" + timestamp(now()).strftime("%Y%m%dT%H%M%S%fZ") + ".sqlite"
                    result = backup(workspace, workspace.data / "backups/automatic" / filename)
                    from ._retention import best_effort
                    from .retention import prune_backups
                    result["retention"] = best_effort(prune_backups, workspace, result["path"])
                    jobs.setdefault("backup", {})["saved_revision"] = current
                    record("backup", "ok", detail=dict(path=result["path"], **current), worked=True)
            except Exception as error:
                failed("backup", error)

        if due("audit"):
            try:
                result = audit(workspace, recovery_check=True)
                atomic_write(directory / "last-audit.json", json_bytes(result))
                record("audit", result["status"], detail=dict(warnings=result["warnings"]), worked=True)
            except Exception as error:
                failed("audit", error)
        save()
        unhealthy = {name: item["status"] for name, item in jobs.items()
                     if item.get("status") in ("failed", "warning", "login_required")}
        result = dict(status="warning" if unhealthy else "ok", checked_at_utc=now(),
                      jobs_run=outcomes, attention=unhealthy, usb_enabled=False)
        atomic_write(directory / "last-run.json", json_bytes(result))
        return result
