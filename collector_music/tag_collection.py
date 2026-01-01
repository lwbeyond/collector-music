"""Resumable, serial collection of source-provided labels for known song IDs."""
from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime
import math
import os
from pathlib import Path
import re
import time
import uuid

from .collector import ADAPTER_VERSION, account_from_status, bridge, clean_body
from .common import LoginRequired, MusicError, WorkspaceBusy, atomic_write, digest, identifier, json_bytes, now, parse_json, utc_time
from .netease import platform_id, reject_credentials
from .song_tags import FORMAT, SOURCE, catalog_tracks, import_song_tags, normalize_song_tags, pending_song_ids, song_id

RISK_CODES = {-460, -461, -462, 405, 406, 429, 460, 461, 462, 509}


def _limit(value):
    if value is not None and (type(value) is not int or value < 1):
        raise MusicError("歌曲标签批次上限必须是正整数。")
    return value


def plan_song_tags(workspace, person="me", limit=None):
    """Plan from the full historical ID union, without network or modifications."""
    identifier(person, "person")
    _limit(limit)
    with closing(workspace.connect(readonly=True)) as conn:
        conn.execute("BEGIN")
        catalog = catalog_tracks(conn, person)
        pending = pending_song_ids(conn, person)
        has_tags = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='song_tags'").fetchone()
        failed_ids = {song_id(row[0]) for row in conn.execute(
            "SELECT DISTINCT platform_track_id FROM song_tags WHERE source=? AND status='failed'", (SOURCE,))} if has_tags else set()
        revision = conn.execute("SELECT coalesce(max(seq),0) FROM changes").fetchone()[0]
        conn.rollback()
    unattempted = [key for key in pending if key not in failed_ids]
    previously_failed = [key for key in pending if key in failed_ids]
    pending = unattempted + previously_failed
    selected = pending if limit is None else pending[:limit]
    return dict(total=catalog["total"], cached=catalog["total"] - len(pending), pending=len(pending),
        selected=len(selected), song_ids=selected, sources=catalog["sources"], data_revision=revision,
        pending_new=len(unattempted), pending_failed=len(previously_failed),
        unmatched_pano_count=catalog["unmatched_pano_count"],
        unmatched_pano_observations=catalog["unmatched_pano_observations"])


def _session(workspace, person, account_key=None):
    path = workspace.home / ".local/session.json"
    if not path.is_file():
        raise LoginRequired("尚未登录，请先运行 login。")
    payload = path.read_bytes()
    session = parse_json(payload)
    if not isinstance(session, dict):
        raise LoginRequired("本机会话无效，请重新登录。")
    account = platform_id(session.get("account_key"))
    if account_key is not None and account != platform_id(account_key):
        raise LoginRequired("指定证据账号与本机会话账号不一致。")
    with closing(workspace.connect(readonly=True)) as conn:
        bound = conn.execute("SELECT person_key FROM source_accounts WHERE source='netease' AND account_key=?", (account,)).fetchone()
    if not bound or bound[0] != person:
        raise MusicError("当前账号尚未绑定到此 person；先完成账号资料采集。")
    cookie = session.get("cookie")
    if not isinstance(cookie, str) or not cookie:
        raise LoginRequired("本机会话无效，请重新登录。")
    return account, cookie, digest(payload)


@contextmanager
def _node_configuration(workspace):
    previous = os.environ.get("MUSIC_NODE_EXE")
    path = workspace.home / ".local/automation.json"
    if path.exists():
        config = parse_json(path.read_bytes())
        if not isinstance(config, dict):
            raise MusicError("自动化配置格式无效。")
        node = config.get("node_executable")
        if node:
            if not isinstance(node, str) or not Path(node).is_absolute() or not Path(node).is_file():
                raise MusicError("配置的 Node 路径不可用。")
            os.environ["MUSIC_NODE_EXE"] = node
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("MUSIC_NODE_EXE", None)
        else:
            os.environ["MUSIC_NODE_EXE"] = previous


def _spool_root(workspace):
    directory = workspace.home / ".local/tag-collection/spool"
    directory.mkdir(parents=True, exist_ok=True)
    if not directory.resolve().is_relative_to(workspace.home):
        raise MusicError("歌曲标签暂存目录必须位于当前资料目录内。")
    return directory


def _persist(workspace, document, person):
    """Durable response first, then formal raw archive and transactional import."""
    document = dict(document, collector_person_key=person)
    path = _spool_root(workspace) / ("capture-" + uuid.uuid4().hex + ".json")
    atomic_write(path, json_bytes(document))
    result = import_song_tags(workspace, path, person=person)
    path.unlink()
    return result


def _recover_spool(workspace, person):
    recovered, failed = 0, False
    directory = _spool_root(workspace)
    for path in sorted(directory.glob("capture-*.json")):
        if not path.resolve().is_relative_to(directory.resolve()):
            raise MusicError("歌曲标签暂存路径超出指定目录。")
        document = parse_json(path.read_bytes())
        reject_credentials(document)
        if document.get("collector_person_key") != person:
            continue
        result = import_song_tags(workspace, path, person=person)
        failed = failed or result.get("status") == "partial" or any(
            row.get("status") == "failed" or row.get("stop_reason") for row in document.get("songs", []))
        recovered += result.get("inserted_records", 0)
        path.unlink()
    return dict(recovered=recovered, failed=bool(failed))


def _failure_reason(error=None, body=None):
    if isinstance(error, LoginRequired):
        return "login_required"
    code = body.get("code") if isinstance(body, dict) else None
    if code in (301, 401):
        return "login_required"
    if code in RISK_CODES:
        return "rate_limited"
    if error is not None:
        match = re.search(r"状态码\s+(-?\d+)", str(error))
        if match and int(match[1]) in RISK_CODES:
            return "rate_limited"
    if isinstance(body, dict):
        message = str(body.get("message", body.get("msg", ""))).lower()
        if any(word in message for word in ("频繁", "风控", "验证码", "captcha", "risk")):
            return "rate_limited"
    return "upstream_error"


def collect_song_tags(workspace, person="me", limit=None, delay=3, notify=None):
    """Collect one bounded batch; stop on failures and defer on shared lock contention.

    The exclusive tag-collect lock prevents duplicate workers. The collect lock is
    released after every request/import so core collection can run between songs.
    Successful missing fields and entirely empty labels remain permanently cached.
    """
    identifier(person, "person")
    _limit(limit)
    if isinstance(delay, bool) or not isinstance(delay, (int, float)) or not math.isfinite(delay) or delay < 3 or delay > 60:
        raise MusicError("歌曲标签请求间隔应在 3–60 秒之间。")
    workspace.initialize()
    with workspace.lock("tag-collect"):
        recovery = _recover_spool(workspace, person)
        plan = plan_song_tags(workspace, person, limit)
        progress = dict(format="collector-music-tag-progress", version=1, started_at_utc=now(),
            total=plan["total"], cached_before=plan["cached"], selected=plan["selected"],
            attempted=0, ok=0, empty=0, failed=0, not_attempted=plan["selected"],
            remaining=plan["pending"], pending_import=0, recovered=recovery["recovered"], status="running", stop_reason=None)

        def report(status=None, reason=None):
            if status:
                progress["status"], progress["stop_reason"] = status, reason
            progress["updated_at_utc"] = now()
            progress["not_attempted"] = plan["selected"] - progress["attempted"]
            progress["remaining"] = plan_song_tags(workspace, person)["pending"]
            atomic_write(workspace.home / ".local/tag-collection/progress.json", json_bytes(progress))
            if notify:
                notify(dict(progress))
            return dict(progress)

        report()
        if recovery["failed"]:
            return report("stopped", "recovered_failed_attempt")
        if not plan["song_ids"]:
            return report("complete")
        try:
            account, cookie, session_hash = _session(workspace, person)
        except LoginRequired:
            return report("stopped", "login_required")
        with _node_configuration(workspace):
            try:
                time.sleep(delay)
                with workspace.lock("collect"):
                    response = bridge("login_status", {"cookie": cookie})
                    body = response.get("body") if isinstance(response, dict) else None
                    if not isinstance(body, dict):
                        return report("stopped", "invalid_login_response")
                    verified = account_from_status(body)
                    if verified != account:
                        return report("stopped", "account_mismatch")
            except WorkspaceBusy:
                return report("deferred", "collect_busy")
            except MusicError as error:
                return report("stopped", _failure_reason(error=error))
            for track_id in plan["song_ids"]:
                # Deliberately leave a request-free, unlocked window for core
                # collection rather than holding its lock during pacing sleeps.
                time.sleep(delay)
                session_path = workspace.home / ".local/session.json"
                if not session_path.exists() or digest(session_path.read_bytes()) != session_hash:
                    return report("stopped", "session_changed")
                try:
                    with workspace.lock("collect"):
                        observed = now()
                        capture = dict(song_id=track_id, observed_at_utc=observed,
                            status="failed", method="song_wiki_summary")
                        stop_reason = None
                        try:
                            body = clean_body(bridge("song_wiki_summary", {"cookie": cookie, "id": track_id})["body"])
                            reject_credentials(body)
                            capture["body"] = body
                            if isinstance(body, dict) and body.get("code") == 200:
                                capture["status"] = "ok"
                                try:
                                    normalize_song_tags(body)
                                except MusicError:
                                    stop_reason = "invalid_response"
                            else:
                                stop_reason = _failure_reason(body=body)
                        except MusicError as error:
                            stop_reason = _failure_reason(error=error)
                        except (KeyError, TypeError, ValueError):
                            stop_reason = "invalid_response"
                        if stop_reason:
                            capture.update(error=stop_reason, stop_reason=stop_reason)
                        progress["attempted"] += 1
                        progress["last_song_id"] = track_id
                        progress["pending_import"] += 1
                        document = dict(format=FORMAT, version=1, account_key=account,
                            adapter_version=ADAPTER_VERSION, collected_at_utc=now(), songs=[capture])
                        result = _persist(workspace, document, person)
                        state = result.get("songs", {}).get(track_id)
                        if result.get("status") == "duplicate":
                            with closing(workspace.connect(readonly=True)) as conn:
                                row = conn.execute("SELECT status FROM song_tags WHERE import_id=? AND platform_track_id=?",
                                    (result["import_id"], track_id)).fetchone()
                            state = row[0] if row else None
                        if state not in ("ok", "empty", "failed"):
                            raise MusicError("歌曲标签导入未返回确定状态；已保留归档。")
                        progress["pending_import"] -= 1
                        progress[state] += 1
                except WorkspaceBusy:
                    return report("deferred", "collect_busy")
                report()
                if state == "failed" or stop_reason:
                    return report("stopped", stop_reason or "invalid_response")
        return report("complete" if not progress["remaining"] else "limited")


def adopt_tag_evidence(workspace, evidence_dir, *, account_key, person="me"):
    """Adopt a verified legacy sample with explicit account attribution.

    Legacy samples did not store an account ID. The caller supplies the known
    account; provenance states this attribution rather than claiming it came
    from the original response. All original observation timestamps are retained.
    """
    identifier(person, "person")
    directory = Path(evidence_dir).resolve()
    if not directory.is_relative_to((workspace.home / ".local/validation").resolve()):
        raise MusicError("仅接受当前项目私有验证目录中的原始标签证据。")
    workspace.initialize()
    with workspace.lock("tag-collect"):
        account, _, _ = _session(workspace, person, account_key)
        summary_bytes = (directory / "summary.json").read_bytes()
        summary = parse_json(summary_bytes)
        reject_credentials(summary)
        if not isinstance(summary, dict) or summary.get("format") != "collector-music-tag-coverage" or summary.get("version") != 1:
            raise MusicError("标签抽查摘要格式不匹配。")
        completed = utc_time(summary.get("checked_at_utc"))
        results = summary.get("results")
        if not isinstance(results, list):
            raise MusicError("标签抽查结果列表缺失。")
        captures, seen = [], set()
        with closing(workspace.connect(readonly=True)) as conn:
            catalog = set(catalog_tracks(conn, person)["song_ids"])
        for index, row in enumerate(results, 1):
            if not isinstance(row, dict):
                raise MusicError("标签抽查条目无效。")
            if row.get("status") != "ok":
                continue
            track_id = song_id(row.get("song_id"))
            if track_id in seen or track_id not in catalog:
                raise MusicError("标签抽查含重复或不属于本库的歌曲 ID。")
            seen.add(track_id)
            path = directory / "responses" / (str(index).zfill(2) + ".json")
            if not path.resolve().is_relative_to(directory):
                raise MusicError("标签原始响应路径超出验证目录。")
            response_bytes = path.read_bytes()
            response = parse_json(response_bytes)
            reject_credentials(response)
            if not isinstance(response, dict) or response.get("method") != "song_wiki_summary" or song_id(response.get("song_id")) != track_id:
                raise MusicError("标签原始响应的方法或歌曲 ID 不匹配。")
            observed = utc_time(response.get("observed_at_utc"))
            if observed != utc_time(row.get("observed_at_utc")) or datetime.fromisoformat(observed) > datetime.fromisoformat(completed):
                raise MusicError("标签原始响应的观测时间与摘要不匹配。")
            _, normalized = normalize_song_tags(response.get("body"))
            if any(normalized[key] != row.get(key) for key in ("fields", "available", "basic_block_present")):
                raise MusicError("标签原始响应内容与抽查摘要不匹配。")
            captures.append(dict(song_id=track_id, observed_at_utc=observed, status="ok", body=response["body"],
                provenance=dict(kind="adopted_from_sample", method="song_wiki_summary",
                    account_attribution="caller_confirmed_bound_account", summary_sha256=digest(summary_bytes),
                    response_sha256=digest(response_bytes), original_observed_at_utc=observed)))
        adopted = 0
        for capture in captures:
            with closing(workspace.connect(readonly=True)) as conn:
                cached = conn.execute("SELECT 1 FROM song_tags WHERE source=? AND platform_track_id=? AND status IN ('ok','empty') LIMIT 1",
                    (SOURCE, capture["song_id"])).fetchone()
            if cached:
                continue
            document = dict(format=FORMAT, version=1, account_key=account,
                adapter_version=summary.get("adapter_version", "unknown"), collected_at_utc=completed, songs=[capture])
            adopted += _persist(workspace, document, person).get("inserted_records", 0)
        return dict(status="ok", validated=len(captures), adopted=adopted, cached=len(captures) - adopted)
