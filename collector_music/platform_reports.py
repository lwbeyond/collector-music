"""Platform-authored reports, kept separate from observed playback facts."""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import math

from .common import MusicError, identifier, json_text, parse_json, stable_id, utc_ms, utc_time
from .netease import platform_id, reject_credentials
from .store import begin_import, change, finish_import, insert_row

REPORT_METHODS = {
    "monthly": ("listen_data_report", {"type": "month"}),
    "realtime": ("listen_data_realtime_report", {"type": "month"}),
    "preference": ("style_preference", {}),
    "total": ("listen_data_total", {}),
}
FORMAT = "collector-music-netease-reports"
ZONE = ZoneInfo("Asia/Shanghai")


def numeric(value, *, percent=False):
    if value is None:
        return None
    if percent and isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            raise MusicError("平台报告比例不是有效数字。") from None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise MusicError("平台报告数值必须为非负有限数字。")
    if percent and value > 100:
        raise MusicError("平台报告比例超出百分比范围。")
    return value


def object_or_empty(value):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise MusicError("平台报告区块结构无效。")
    return value


def array_or_empty(value):
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise MusicError("平台报告列表结构无效。")
    return value


def text(value):
    if value is not None and not isinstance(value, str):
        raise MusicError("平台报告名称字段无效。")
    return value


def breakdown(block, key, count_key=None):
    result = []
    for item in array_or_empty(object_or_empty(block).get("sections")):
        name = text(item.get(key))
        if name is None:
            raise MusicError("平台报告分类名称缺失。")
        row = dict(name=name, percent=numeric(item.get("percent"), percent=True))
        if count_key:
            row["count" if key == "age" else "song_count"] = numeric(item.get(count_key))
        result.append(row)
    return result


def normalize_report(kind, body):
    """Return status, normalized data and exact upstream UTC bounds.

    Duration units are intentionally unknown. The live sample's displayed peak
    minutes differs from its daily raw value; no seconds/minutes conversion is
    justified by the evidence. Preserve values for later contract verification.
    """
    if kind not in REPORT_METHODS or not isinstance(body, dict) or body.get("code") != 200:
        raise MusicError("平台报告请求未成功。")
    if "data" not in body:
        raise MusicError("平台报告响应缺少 data 字段。")
    data = object_or_empty(body.get("data"))
    if not data:
        return "empty", {}, None, None
    if kind == "preference":
        if "tagPreferenceVos" not in data:
            raise MusicError("平台曲风偏好列表缺失。")
        tags = []
        for item in array_or_empty(data["tagPreferenceVos"]):
            name = text(item.get("tagName"))
            if name is None:
                raise MusicError("平台曲风偏好名称缺失。")
            tags.append(dict(name=name, percent=numeric(item.get("ratio"), percent=True)))
        return "ok" if tags else "empty", {"tags": tags}, None, None
    if kind == "total":
        if "totalDuration" not in data:
            raise MusicError("平台累计时长字段缺失。")
        duration = numeric(data["totalDuration"])
        return "empty" if duration is None else "ok", {
            "metrics": {"duration_raw": duration, "duration_unit": "unknown", "duration_minutes": None}}, None, None
    if data.get("type") != "month":
        raise MusicError("平台返回了不同类型的报告。")
    start = utc_ms(data.get("startTime"))
    end = utc_ms(data.get("endTime"))
    first = datetime.fromisoformat(start).astimezone(ZONE)
    last = datetime.fromisoformat(end).astimezone(ZONE)
    if first > last or (first.year, first.month) != (last.year, last.month):
        raise MusicError("平台月报时间范围无效。")
    if "listenTimeDistributionBlock" not in data and "listenTimeBlock" not in data:
        raise MusicError("平台月报缺少收听数据区块。")
    distribution = object_or_empty(data.get("listenTimeDistributionBlock"))
    listen = object_or_empty(data.get("listenTimeBlock"))
    if not distribution and not listen:
        return "empty", dict(period_label=first.strftime("%Y-%m"),
            period_start=first.date().isoformat(), period_end=last.date().isoformat()), start, end
    daily = []
    seen = set()
    for item in array_or_empty(distribution.get("durationDetails")):
        try:
            day = date.fromisoformat(item["period"])
        except (KeyError, ValueError, TypeError):
            raise MusicError("平台日报日期无效。") from None
        if not first.date() <= day <= last.date() or day in seen:
            raise MusicError("平台日报日期超出报告范围或重复。")
        seen.add(day)
        if item.get("reachLimit") is not None and not isinstance(item["reachLimit"], bool):
            raise MusicError("平台日报限制标记无效。")
        daily.append(dict(date=day.isoformat(), duration_raw=numeric(item.get("duration")),
            duration_minutes=None, podcast_duration_raw=numeric(item.get("podcastDuration")),
            audiobook_duration_raw=numeric(item.get("audiobookDuration")), reach_limit=item.get("reachLimit")))
    daily.sort(key=lambda row: row["date"])
    duration = numeric(distribution.get("playDuration", listen.get("playDuration")))
    normalized = dict(period_label=first.strftime("%Y-%m"), period_start=first.date().isoformat(),
        period_end=last.date().isoformat(), metrics=dict(duration_raw=duration,
            duration_unit="unknown", duration_minutes=None, listen_days=numeric(distribution.get("listenDays"))),
        daily=daily, genres=breakdown(data.get("topStyleBlock"), "genreName"),
        languages=breakdown(data.get("topLanguageBlock"), "language", "playSongNum"),
        ages=breakdown(data.get("topAgeBlock"), "age", "playSongNum"),
        top_song=None, top_artist=None)
    song = object_or_empty(data.get("topSongBlock"))
    if song:
        artists = array_or_empty(song.get("artists"))
        normalized["top_song"] = dict(id=platform_id(song["songId"]) if song.get("songId") is not None else None,
            name=text(song.get("songName")), artist=" / ".join(text(a.get("artistName")) or "" for a in artists),
            play_count=numeric(song.get("playCount")))
    artist = object_or_empty(data.get("topArtistBlock"))
    if artist:
        artist_id = artist.get("artistId", artist.get("artisId"))
        normalized["top_artist"] = dict(id=platform_id(artist_id) if artist_id is not None else None,
            name=text(artist.get("artistName")), play_count=numeric(artist.get("playCount")))
    return "ok", normalized, start, end


def import_reports(workspace, path: Path, *, person="me"):
    identifier(person, "person")
    if path.stat().st_size > 64 * 1024 * 1024:
        raise MusicError("平台报告快照超过 64 MiB。")
    payload = path.read_bytes()
    document = parse_json(payload)
    reject_credentials(document)
    if not isinstance(document, dict) or document.get("format") != FORMAT or document.get("version") != 1:
        raise MusicError("平台报告快照契约不匹配。")
    account = platform_id(document.get("account_key"))
    collected = utc_time(document.get("collected_at_utc"))
    workspace.initialize()
    manifest = workspace.archive(payload, kind="netease_reports", person=person, account=account,
        collected_at=collected, settings={"adapter_version": document.get("adapter_version")})
    return ingest_reports(workspace, manifest, payload)


def ingest_reports(workspace, manifest, payload):
    document = parse_json(payload)
    reject_credentials(document)
    if not isinstance(document, dict) or document.get("format") != FORMAT or document.get("version") != 1:
        raise MusicError("平台报告快照契约不匹配。")
    account = platform_id(document.get("account_key"))
    if manifest["kind"] != "netease_reports" or account != manifest["account_key"] or utc_time(document.get("collected_at_utc")) != manifest["collected_at"]:
        raise MusicError("平台报告归档身份或采集时间不匹配。")
    person = identifier(manifest["person_key"], "person")
    reports = document.get("reports")
    if not isinstance(reports, dict) or not reports or not set(reports).issubset(REPORT_METHODS):
        raise MusicError("平台报告范围无效。")
    statuses = {}
    with workspace.transaction() as conn:
        if begin_import(conn, manifest):
            return dict(import_id=manifest["id"], status="duplicate", inserted_records=0)
        bound = conn.execute("SELECT person_key FROM source_accounts WHERE source='netease' AND account_key=?", (account,)).fetchone()
        if bound and bound[0] != person:
            raise MusicError("此网易云账号已绑定另一个 person_key。")
        conn.execute("INSERT OR IGNORE INTO source_accounts VALUES('netease',?,?)", (account, person))
        for kind, capture in reports.items():
            if not isinstance(capture, dict) or capture.get("status") not in ("ok", "failed"):
                raise MusicError("平台报告采集状态无效。")
            observed = utc_time(capture.get("observed_at_utc"))
            if datetime.fromisoformat(observed) > datetime.fromisoformat(manifest["collected_at"]):
                raise MusicError("平台报告观测时间晚于采集完成时间。")
            normalized, start, end = {}, None, None
            state = "failed"
            if capture["status"] == "ok":
                try:
                    state, normalized, start, end = normalize_report(kind, capture.get("body"))
                except MusicError as error:
                    normalized = {"error": str(error)}
            key = stable_id(manifest["id"], kind)
            insert_row(conn, "platform_reports", dict(id=key, import_id=manifest["id"], person_key=person,
                account_key=account, report_kind=kind, observed_at_utc=observed, status=state,
                period_start_utc=start, period_end_utc=end,
                normalized_json=json_text(normalized), payload_json=json_text(capture)))
            change(conn, "platform_report", key, state, manifest["id"])
            statuses[kind] = state
        result = finish_import(conn, manifest, len(statuses), status="partial" if "failed" in statuses.values() else "imported")
    return dict(result, reports=statuses)


def dashboard_reports(conn, available=True):
    """Return latest usable snapshots, without identities or raw/private fields."""
    result = dict(status="empty", account_count=0, monthly=[], realtime=None, preference=None, total=None, issues=[])
    if not available:
        return result
    cursor = conn.execute("SELECT * FROM platform_reports ORDER BY observed_at_utc DESC,import_id DESC,id DESC")
    names = [column[0] for column in cursor.description]
    records = [dict(zip(names, row)) for row in cursor.fetchall()]
    if not records:
        return result
    accounts = {row["account_key"] for row in records}
    account = records[0]["account_key"]
    result["account_count"] = len(accounts)
    records = [row for row in records if row["account_key"] == account]
    if len(accounts) > 1:
        result["issues"].append("当前展示最近采集账号的报告；多个账号不合并。")

    def entry(row, latest):
        value = parse_json(row["normalized_json"])
        return dict(value, id=row["id"], kind=row["report_kind"], observed_at=row["observed_at_utc"],
            latest_attempt_at=latest["observed_at_utc"], data_status=row["status"],
            status="stale" if latest["status"] == "failed" else row["status"])

    failures = False
    any_success = False
    for kind in REPORT_METHODS:
        items = [row for row in records if row["report_kind"] == kind]
        if not items:
            continue
        latest = items[0]
        successes = [row for row in items if row["status"] in ("ok", "empty")]
        if latest["status"] == "failed":
            failures = True
            result["issues"].append({"kind": kind, "status": "stale" if successes else "unavailable",
                "latest_attempt_at": latest["observed_at_utc"]})
        if not successes:
            continue
        any_success = True
        if kind != "monthly":
            result[kind] = entry(successes[0], latest)
        elif successes[0]["status"] == "empty" and not successes[0]["period_start_utc"]:
            result["monthly"] = [entry(successes[0], latest)]
        else:
            seen = set()
            for row in successes:
                if not row["period_start_utc"]:
                    continue
                key = (row["period_start_utc"], row["period_end_utc"])
                if key in seen:
                    continue
                seen.add(key)
                result["monthly"].append(entry(row, latest))
            result["monthly"].sort(key=lambda row: row.get("period_start", ""), reverse=True)
    result["status"] = "ready" if any_success else "unavailable" if failures else "empty"
    return result
