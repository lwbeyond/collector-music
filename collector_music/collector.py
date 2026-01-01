from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import time
import webbrowser
from contextlib import closing
from pathlib import Path

from .common import LoginRequired, MusicError, atomic_write, json_bytes, now, parse_json
from .netease import SENSITIVE_KEYS, import_netease, platform_id, song_fields

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ADAPTER_VERSION = "4.40.1"
SONG_DETAIL_BATCH = 200


def bridge(method, params=None):
    node = os.environ.get("MUSIC_NODE_EXE") or shutil.which("node")
    if not node or not (PROJECT_ROOT / "node_modules/@neteasecloudmusicapienhanced/api").exists():
        raise MusicError("网易云适配器尚未安装；在项目目录运行 npm.cmd ci --ignore-scripts。")
    flags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
    temporary = PROJECT_ROOT / ".local/node-tmp"
    temporary.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ, TMP=str(temporary), TEMP=str(temporary), TMPDIR=str(temporary))
    try:
        result = subprocess.run([node, str(PROJECT_ROOT / "adapters/netease.mjs")],
            input=json_bytes(dict(method=method, params=params or {})), capture_output=True,
            timeout=50, creationflags=flags, cwd=PROJECT_ROOT, env=environment)
    except subprocess.TimeoutExpired as exc:
        raise MusicError("网易云请求超时，本轮停止；登录态和已有数据保持。") from exc
    try:
        response = parse_json(result.stdout)
    except MusicError as exc:
        raise MusicError("适配器没有返回有效响应；未输出可能包含凭据的上游日志。") from exc
    if result.returncode or response.get("ok") is not True:
        if response.get("code") in (301, 401):
            raise LoginRequired("网易云登录已失效，请重新扫码。")
        raise MusicError(f"网易云请求失败，上游状态码 {response.get('code', 0)}。")
    return response


def account_from_status(body):
    data = body.get("data", body)
    if not isinstance(data, dict) or data.get("code") != 200:
        if isinstance(data, dict) and data.get("code") in (301, 401):
            raise LoginRequired("登录状态无效，请重新扫码。")
        raise MusicError("登录状态检查失败，稍后重试。")
    account = data.get("account")
    if not isinstance(account, dict):
        raise LoginRequired("没有已登录的网易云账号。")
    return platform_id(account.get("id"))


def login(workspace, timeout=180, open_browser=True, notify=print):
    workspace.initialize()
    key_body = bridge("login_qr_key", {"type": 1})["body"]
    try:
        key = key_body["data"]["unikey"]
    except (KeyError, TypeError) as exc:
        raise MusicError("扫码 key 响应格式变化，需要检查上游适配器。") from exc
    qr_body = bridge("login_qr_create", {"key": key, "qrimg": True})["body"]
    data_url = qr_body.get("data", {}).get("qrimg", "")
    if not data_url.startswith("data:image/png;base64,"):
        raise MusicError("未取得扫码图片。")
    png = base64.b64decode(data_url.split(",", 1)[1], validate=True)
    qr_path = workspace.home / ".local/login-qr.png"
    atomic_write(qr_path, png)
    page = workspace.home / ".local/login.html"
    atomic_write(page, ('<!doctype html><meta charset="utf-8"><title>音乐数据 · 网易云登录</title>'
        '<style>body{font-family:system-ui;text-align:center;margin:64px;background:#f7f5ef}img{width:280px}</style>'
        '<h1>用网易云 App 扫码</h1><img src="login-qr.png"><p>登录凭据只保存在本机 .local。</p>'
        '<p>扫码完成后查看命令行回执；图片过期时重新运行登录。</p>').encode("utf-8"))
    notify(f"扫码图片：{qr_path}")
    if open_browser:
        webbrowser.open(page.as_uri())
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(3)
        response = bridge("login_qr_check", {"key": key, "noCookie": True})
        body = response["body"]
        code = body.get("code")
        if code == 800:
            raise MusicError("二维码已过期，请重新运行 login。")
        if code in (801, 802):
            continue
        if code != 803:
            raise MusicError(f"扫码检查返回未识别状态 {code}，停止轮询。")
        cookie = body.get("cookie")
        if not cookie:
            cookies = response.get("cookies") or []
            cookie = "; ".join(c.split(";", 1)[0] for c in cookies)
        if not isinstance(cookie, str) or not cookie:
            raise MusicError("扫码成功但缺少会话，未保存空凭据。")
        account = account_from_status(bridge("login_status", {"cookie": cookie})["body"])
        session_path = workspace.home / ".local/session.json"
        atomic_write(session_path, json_bytes(dict(account_key=account, cookie=cookie, saved_at_utc=now())))
        session_path.chmod(0o600)
        return dict(status="logged_in", account_key=account, credential_path=str(session_path))
    raise MusicError("等待扫码超时；没有修改已有会话。")


def clean_body(value):
    if isinstance(value, dict):
        return {k: clean_body(v) for k,v in value.items() if k.lower() not in SENSITIVE_KEYS}
    if isinstance(value, list):
        return [clean_body(v) for v in value]
    return value


def enrich_likes(body, cookie, delay):
    """Metadata failure must not discard the complete relationship ID list."""
    ids = body.get("ids")
    if not isinstance(ids, list):
        raise MusicError("红心 ID 清单缺失")
    wanted = [platform_id(value) for value in ids]
    if len(wanted) != len(set(wanted)):
        raise MusicError("红心清单存在重复 ID")
    songs, pages, resolved = [], [], set()
    error = None
    for offset in range(0, len(wanted), SONG_DETAIL_BATCH):
        batch = wanted[offset:offset + SONG_DETAIL_BATCH]
        try:
            time.sleep(delay)
            response = clean_body(bridge("song_detail", {"cookie": cookie, "ids": ",".join(batch)})["body"])
            pages.append(dict(requested_ids=batch, body=response))
            if response.get("code") in (301, 401):
                raise LoginRequired("网易云登录已失效，请重新扫码。")
            if response.get("code") != 200 or not isinstance(response.get("songs"), list):
                raise MusicError("歌曲详情响应格式错误")
            batch_ids, batch_songs = set(), []
            for song in response["songs"]:
                song_id = song_fields(song)["platform_track_id"]
                if song_id not in batch or song_id in batch_ids:
                    raise MusicError("歌曲详情返回了未请求或重复的 ID")
                batch_ids.add(song_id)
                batch_songs.append(song)
            songs.extend(batch_songs)
            resolved.update(batch_ids)
        except MusicError as exc:
            error = str(exc)
            if isinstance(exc, LoginRequired):
                body["detail_needs_login"] = True
            break
    missing = [value for value in wanted if value not in resolved]
    body.update(songs=songs, detail_pages=pages, missing_detail_ids=missing,
                detail_status="ok" if not missing else "partial", detail_error=error)
    return bool(error)


def collect(workspace, person="me", scopes=("recent", "week", "all", "likes", "playlists"), delay=3):
    if delay < 3:
        raise MusicError("采集请求间隔不得小于 3 秒。")
    if not scopes or not set(scopes).issubset({"recent", "week", "all", "likes", "playlists"}):
        raise MusicError("未知采集范围。")
    path = workspace.home / ".local/session.json"
    if not path.exists():
        raise LoginRequired("尚未登录；请运行 login 并用网易云 App 扫码。")
    session = parse_json(path.read_bytes())
    cookie = session.get("cookie")
    account = platform_id(session.get("account_key"))
    if not isinstance(cookie, str) or not cookie:
        raise LoginRequired("本机会话无效，请重新登录。")
    verified = account_from_status(bridge("login_status", {"cookie": cookie})["body"])
    if verified != account:
        raise LoginRequired("登录账号与本机会话绑定不符，停止采集。")
    captured = {}
    stopped = False
    for scope in dict.fromkeys(scopes):
        observed = now()
        if stopped:
            captured[scope] = dict(status="failed", observed_at_utc=observed, error="此前请求失败，本轮停止")
            continue
        try:
            if scope == "playlists":
                pages, items, seen = [], [], set()
                offset = 0
                for _ in range(100):
                    time.sleep(delay)
                    body = clean_body(bridge("user_playlist", {"cookie": cookie, "uid": account, "limit": 100, "offset": offset})["body"])
                    pages.append(body)
                    rows = body.get("playlist")
                    if body.get("code") != 200 or not isinstance(rows, list):
                        raise MusicError("歌单分页失败")
                    for row in rows:
                        item_id = platform_id(row.get("id"))
                        if item_id in seen:
                            raise MusicError("歌单分页重复")
                        seen.add(item_id)
                        items.append(row)
                    if body.get("more") is False:
                        break
                    if body.get("more") is not True or not rows:
                        raise MusicError("歌单分页终点不明确")
                    offset += len(rows)
                else:
                    raise MusicError("歌单分页超过安全边界")
                body = dict(code=200, playlist=items, more=False, original_pages=pages)
            else:
                method, params = {
                    "recent": ("record_recent_song", {"limit": 100}),
                    "week": ("user_record", {"uid": account, "type": 1}),
                    "all": ("user_record", {"uid": account, "type": 0}),
                    "likes": ("likelist", {"uid": account}),
                }[scope]
                time.sleep(delay)
                body = clean_body(bridge(method, {"cookie": cookie, **params})["body"])
                if body.get("code") != 200:
                    captured[scope] = dict(status="failed", observed_at_utc=observed, body=body)
                    stopped = True
                    continue
                if scope == "likes":
                    stopped = enrich_likes(body, cookie, delay)
            captured[scope] = dict(status="ok", observed_at_utc=observed, completed_at_utc=now(), body=body)
        except MusicError as exc:
            captured[scope] = dict(status="failed", observed_at_utc=observed, error=str(exc))
            if isinstance(exc, LoginRequired):
                captured[scope]["needs_login"] = True
            if scope == "playlists":
                captured[scope]["pages"] = pages
            stopped = True
    document = dict(format="collector-music-netease", version=1, adapter_version=ADAPTER_VERSION,
                    account_key=account, collected_at_utc=now(), scopes=captured)
    snapshot = workspace.home / ".local/last-collection.json"
    atomic_write(snapshot, json_bytes(document))
    result = import_netease(workspace, snapshot, person=person)
    result["scopes"] = {name: data["status"] for name,data in captured.items()}
    result["needs_login"] = any(data.get("needs_login") or
        data.get("body", {}).get("code") in (301, 401) or
        data.get("body", {}).get("detail_needs_login") for data in captured.values())
    if "likes" in captured and captured["likes"].get("body", {}).get("detail_status"):
        result["likes_detail_status"] = captured["likes"]["body"]["detail_status"]
    return result


def record_report_failure(workspace, person="me", *, account=None, observed_at=None,
                          needs_login=False, failure_stage="login_status"):
    """Record a failed/skipped report attempt only for a previously bound account.

    Callers hold the collect lock. This performs no requests and cannot create a
    new account binding. A login-pause timestamp gives repeat attempts the same
    identity if the process stops between importing evidence and saving state.
    """
    from .platform_reports import FORMAT, REPORT_METHODS, import_reports
    if failure_stage not in ("login_status", "login_pause"):
        raise MusicError("未知报告失败阶段。")
    if not workspace.db.is_file():
        return None
    if account is None:
        path = workspace.home / ".local/session.json"
        if not path.is_file():
            return None
        try:
            session = parse_json(path.read_bytes())
            if not isinstance(session, dict):
                return None
            account = platform_id(session.get("account_key"))
        except MusicError:
            return None
    else:
        account = platform_id(account)
    with closing(workspace.connect(readonly=True)) as conn:
        bound = conn.execute("SELECT person_key FROM source_accounts WHERE source='netease' AND account_key=?",
            (account,)).fetchone()
    if not bound or bound[0] != person:
        return None
    observed = observed_at or now()
    captured = {kind: dict(status="failed", observed_at_utc=observed,
        method=method, params=params, needs_login=needs_login,
        failure_stage=failure_stage, request_attempted=False,
        error="账号登录已暂停，本轮未读取报告" if failure_stage == "login_pause" else
              "登录校验失败，等待重新登录" if needs_login else "登录校验请求失败，本轮未读取报告")
        for kind, (method, params) in REPORT_METHODS.items()}
    document = dict(format=FORMAT, version=1, adapter_version=ADAPTER_VERSION,
        account_key=account, collected_at_utc=observed if failure_stage == "login_pause" else now(), reports=captured)
    snapshot = workspace.home / ".local/last-reports.json"
    atomic_write(snapshot, json_bytes(document))
    result = import_reports(workspace, snapshot, person=person)
    return dict(result, needs_login=needs_login)


def collect_reports(workspace, person="me", delay=3):
    """Read four platform report endpoints; failures do not discard other reports."""
    from .platform_reports import FORMAT, REPORT_METHODS, import_reports
    if delay < 3:
        raise MusicError("采集请求间隔不得小于 3 秒。")
    path = workspace.home / ".local/session.json"
    if not path.exists():
        raise LoginRequired("尚未登录；请运行 login 并用网易云 App 扫码。")
    session = parse_json(path.read_bytes())
    cookie = session.get("cookie")
    account = platform_id(session.get("account_key"))
    if not isinstance(cookie, str) or not cookie:
        raise LoginRequired("本机会话无效，请重新登录。")

    def archive_attempts(captured, needs_login):
        document = dict(format=FORMAT, version=1, adapter_version=ADAPTER_VERSION,
            account_key=account, collected_at_utc=now(), reports=captured)
        snapshot = workspace.home / ".local/last-reports.json"
        atomic_write(snapshot, json_bytes(document))
        result = import_reports(workspace, snapshot, person=person)
        return dict(result, needs_login=needs_login)

    preflight_observed = now()
    try:
        verified = account_from_status(bridge("login_status", {"cookie": cookie})["body"])
    except MusicError as error:
        result = record_report_failure(workspace, person, account=account,
            observed_at=preflight_observed, needs_login=isinstance(error, LoginRequired))
        if result is not None:
            return result
        raise
    if verified != account:
        raise LoginRequired("登录账号与本机会话绑定不符，停止采集。")
    captured = {}
    needs_login = False
    for kind, (method, params) in REPORT_METHODS.items():
        observed = now()
        if needs_login:
            captured[kind] = dict(status="failed", observed_at_utc=observed,
                needs_login=True, error="登录已失效，本轮停止")
            continue
        try:
            time.sleep(delay)
            body = clean_body(bridge(method, {"cookie": cookie, **params})["body"])
            if not isinstance(body, dict):
                raise MusicError("平台报告响应不是对象。")
            needs_login = body.get("code") in (301, 401)
            captured[kind] = dict(status="ok" if body.get("code") == 200 else "failed",
                observed_at_utc=observed, completed_at_utc=now(), method=method,
                params=params, body=body, needs_login=needs_login)
        except MusicError as error:
            needs_login = isinstance(error, LoginRequired)
            captured[kind] = dict(status="failed", observed_at_utc=observed,
                method=method, params=params, needs_login=needs_login, error=str(error))
    return archive_attempts(captured, needs_login)
