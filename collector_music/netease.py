from __future__ import annotations

from pathlib import Path

from .common import MusicError, identifier, json_text, now, parse_json, stable_id, utc_ms, utc_time
from .store import Workspace, begin_import, change, finish_import, insert_row

SCOPES = {"recent", "week", "all", "likes", "playlists"}
SENSITIVE_KEYS = {"cookie", "cookies", "set-cookie", "authorization", "music_u", "__csrf"}


def reject_credentials(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in SENSITIVE_KEYS:
                raise MusicError("快照含认证字段，拒绝放入原始数据归档；请使用本项目采集器生成数据快照。")
            reject_credentials(item)
    elif isinstance(value, list):
        for item in value:
            reject_credentials(item)


def platform_id(value):
    if isinstance(value, bool) or not isinstance(value, (int, str)) or not str(value).isdigit() or int(value) <= 0:
        raise MusicError("网易云 ID 必须为正整数或其文本形式。")
    return str(value)


def number(value):
    if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
        raise MusicError("上游统计值必须为数字或 null。")
    return value


def song_fields(song):
    if not isinstance(song, dict):
        raise MusicError("歌曲对象格式不符。")
    track_id = platform_id(song.get("id"))
    name = song.get("name")
    if not isinstance(name, str):
        raise MusicError("歌曲名称字段缺失。")
    artists = song.get("ar", song.get("artists", []))
    if not isinstance(artists, list) or any(not isinstance(a, dict) or not isinstance(a.get("name"), str) for a in artists):
        raise MusicError("艺人列表字段格式不符。")
    album = song.get("al", song.get("album"))
    if album is not None and (not isinstance(album, dict) or not isinstance(album.get("name"), str)):
        raise MusicError("专辑字段格式不符。")
    duration = song.get("dt", song.get("duration"))
    if duration is not None and (isinstance(duration, bool) or not isinstance(duration, int) or duration < 0):
        raise MusicError("歌曲时长格式不符。")
    return dict(platform_track_id=track_id, recording_key=f"netease:track:{track_id}", track=name,
                artist=json_text([a["name"] for a in artists]), album=album["name"] if album else None,
                album_artist=None, track_duration_ms=duration)


def import_netease(workspace: Workspace, path: Path, *, person="me"):
    identifier(person, "person")
    if path.stat().st_size > 64 * 1024 * 1024:
        raise MusicError("快照超过 64 MiB 首版限制。")
    payload = path.read_bytes()
    document = parse_json(payload)
    if not isinstance(document, dict) or document.get("format") != "collector-music-netease" or document.get("version") != 1:
        raise MusicError("请导入 collector-music-netease v1 快照。")
    reject_credentials(document)
    account = platform_id(document.get("account_key"))
    collected = utc_time(document.get("collected_at_utc"))
    workspace.initialize()
    manifest = workspace.archive(payload, kind="netease", person=person, account=account,
                                 collected_at=collected, settings={"adapter_version": document.get("adapter_version")})
    return ingest_netease(workspace, manifest, payload)


def ingest_netease(workspace, manifest, payload):
    document = parse_json(payload)
    reject_credentials(document)
    if document.get("format") != "collector-music-netease" or document.get("version") != 1:
        raise MusicError("快照契约不匹配。")
    account = platform_id(document.get("account_key"))
    if account != manifest["account_key"] or utc_time(document.get("collected_at_utc")) != manifest["collected_at"]:
        raise MusicError("快照账号或时间与归档清单不一致。")
    person = identifier(manifest["person_key"])
    scopes = document.get("scopes")
    if not isinstance(scopes, dict) or not scopes or not set(scopes).issubset(SCOPES):
        raise MusicError("快照 scope 缺失或不受支持。")
    with workspace.transaction() as conn:
        previous = begin_import(conn, manifest)
        if previous:
            return dict(import_id=manifest["id"], status="duplicate", inserted_records=0)
        binding = conn.execute("SELECT person_key FROM source_accounts WHERE source='netease' AND account_key=?", (account,)).fetchone()
        if binding and binding[0] != person:
            raise MusicError("此网易云账号已绑定另一个 person_key，拒绝混合身份。")
        conn.execute("INSERT OR IGNORE INTO source_accounts VALUES('netease',?,?)", (account, person))
        total_inserted = 0
        failures = 0
        for scope, capture in scopes.items():
            if not isinstance(capture, dict):
                raise MusicError("scope 记录不是对象。")
            observed = utc_time(capture.get("observed_at_utc"))
            status = capture.get("status")
            if status not in ("ok", "failed", "partial"):
                raise MusicError("scope status 无效。")
            body = capture.get("body")
            times = []
            row_count = 0
            detail = "平台窗口快照，不代表终生或全设备连续覆盖。"
            coverage_status = "observed_only"
            transport = status
            if status != "ok":
                failures += 1
                coverage_status = "unavailable" if status == "failed" else "partial"
                detail = "此范围未完成，响应保存在原始归档；未将其解释为空数据或关系移除。"
            else:
                if not isinstance(body, dict) or body.get("code") != 200:
                    raise MusicError(f"{scope} 声称成功，但没有 code=200 的对象响应。")
                if scope == "recent":
                    data = body.get("data")
                    if not isinstance(data, dict) or not isinstance(data.get("list"), list):
                        raise MusicError("recent 缺少 data.list，拒绝猜测上游结构。")
                    for index, item in enumerate(data["list"]):
                        if not isinstance(item, dict):
                            raise MusicError("recent 条目不是对象。")
                        fields = song_fields(item.get("data"))
                        timestamp = utc_ms(item["playTime"]) if item.get("playTime") is not None else None
                        source_id = stable_id(account, fields["platform_track_id"], timestamp) if timestamp else stable_id(manifest["id"], scope, index)
                        observation_id = stable_id("netease_recent", source_id)
                        pointer = f"/scopes/recent/body/data/list/{index}"
                        exists = conn.execute("SELECT 1 FROM observations WHERE id=?", (observation_id,)).fetchone()
                        if not exists:
                            insert_row(conn, "observations", dict(id=observation_id, person_key=person,
                                source="netease_recent", account_key=account, device_key=None, stream_id=None,
                                source_record_id=source_id, event_kind="recent_observation", **fields,
                                actual_listened_seconds=None, occurred_at_utc=timestamp, observed_at_utc=observed,
                                ingested_at_utc=now(), source_timezone=None,
                                time_precision="millisecond" if timestamp else "unknown",
                                time_basis="upstream_playTime_unverified" if timestamp else "unknown",
                                player_package=None, player_name=None, player_version=None,
                                import_id=manifest["id"], raw_pointer=pointer,
                                quality_flags=json_text(["observed_only", "upstream_time_semantics_unverified",
                                    "timestamp_track_identity_heuristic", "actual_duration_unknown"]), payload_json=json_text(item)))
                            change(conn, "observation", observation_id, "insert", manifest["id"])
                            total_inserted += 1
                        conn.execute("INSERT OR IGNORE INTO observation_evidence VALUES(?,?,?)",
                                     (observation_id, manifest["id"], pointer))
                        if timestamp:
                            times.append(timestamp)
                    row_count = len(data["list"])
                    detail += " playTime 意义待本人样本验证；按账号/曲目/时间去重可能折叠同毫秒重复。"
                elif scope in ("week", "all"):
                    key = "weekData" if scope == "week" else "allData"
                    rows = body.get(key)
                    if not isinstance(rows, list):
                        raise MusicError(f"{scope} 缺少 {key}。")
                    for index, item in enumerate(rows):
                        if not isinstance(item, dict):
                            raise MusicError("榜单条目不是对象。")
                        fields = song_fields(item.get("song"))
                        insert_row(conn, "ranking_items", dict(id=stable_id(manifest["id"], scope, index),
                            import_id=manifest["id"], person_key=person, account_key=account, scope=scope,
                            position=index+1, platform_track_id=fields["platform_track_id"], track=fields["track"],
                            artist=fields["artist"], reported_play_count=number(item.get("playCount")),
                            reported_score=number(item.get("score")), observed_at_utc=observed, payload_json=json_text(item)))
                    row_count = len(rows)
                    total_inserted += row_count
                    detail = "排行汇总快照；不同采集日的计数不相加，不转换为逐次听歌。"
                elif scope in ("likes", "playlists"):
                    rows = body.get("ids" if scope == "likes" else "playlist")
                    if not isinstance(rows, list):
                        raise MusicError(f"{scope} 清单缺失。")
                    if scope == "playlists" and body.get("more") is not False:
                        raise MusicError("playlists 未明确到达 more=false，不能标为完整采集。")
                    songs = {}
                    if scope == "likes" and "songs" in body:
                        if not isinstance(body["songs"], list) or body.get("detail_status") not in ("ok", "partial"):
                            raise MusicError("红心歌曲详情结构不符。")
                        expected = {platform_id(value) for value in rows}
                        for song in body["songs"]:
                            song_id = song_fields(song)["platform_track_id"]
                            if song_id not in expected or song_id in songs:
                                raise MusicError("红心详情包含未请求或重复的歌曲 ID。")
                            songs[song_id] = song
                        if body["detail_status"] == "ok" and set(songs) != expected:
                            raise MusicError("红心详情声称完整，但有 ID 未取得详情。")
                        if body["detail_status"] != "ok":
                            failures += 1
                    seen = set()
                    for index, item in enumerate(rows):
                        entity = platform_id(item if scope == "likes" else item.get("id") if isinstance(item, dict) else None)
                        if entity in seen:
                            raise MusicError("关系列表存在重复 ID，需检查分页稳定性。")
                        seen.add(entity)
                        payload = item
                        if scope == "likes" and "songs" in body:
                            payload = dict(id=entity, song=songs.get(entity),
                                           metadata_status="ok" if entity in songs else "unavailable")
                        insert_row(conn, "library_items", dict(id=stable_id(manifest["id"], scope, entity),
                            import_id=manifest["id"], person_key=person, account_key=account, scope=scope,
                            entity_id=entity, position=index+1, label=item.get("name") if isinstance(item, dict) else None,
                            observed_at_utc=observed, payload_json=json_text(payload)))
                    row_count = len(rows)
                    total_inserted += row_count
                    coverage_status = "snapshot_only"
                    detail = "当前可见关系快照；未推断首次喜欢、历史移除或变化发生时刻。"
                    if scope == "likes" and "songs" in body:
                        detail += f" 红心 ID {len(rows)} 个，歌曲详情 {len(songs)} 个；未取得详情的 ID 仍保留。"
            insert_row(conn, "coverage", dict(id=stable_id(manifest["id"], scope), import_id=manifest["id"],
                person_key=person, source="netease", account_key=account, device_key=None, scope=scope,
                status=coverage_status, transport_status=transport, observed_at_utc=observed,
                min_event_at_utc=min(times, default=None), max_event_at_utc=max(times, default=None),
                record_count=row_count, detail=detail))
        return finish_import(conn, manifest, total_inserted, status="partial" if failures else "imported")
