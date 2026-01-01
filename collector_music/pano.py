from __future__ import annotations

from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .common import MusicError, digest, identifier, json_text, now, parse_json, required_text, stable_id, utc_ms
from .store import Workspace, begin_import, change, finish_import, insert_row

MAX_LOG_BYTES = 256 * 1024 * 1024


def import_pano(workspace: Workspace, path: Path, *, person="me", device, stream,
                source_timezone=None, collector_version="unknown", rule_note="unknown"):
    for value, label in ((person, "person"), (device, "device"), (stream, "stream")):
        identifier(value, label)
    if source_timezone:
        try:
            ZoneInfo(source_timezone)
        except ZoneInfoNotFoundError as exc:
            raise MusicError("未知的来源时区。") from exc
    if path.stat().st_size > MAX_LOG_BYTES:
        raise MusicError("日志超过首版 256 MiB 限制，请保留原文件并按独立日志实例归档。")
    payload = path.read_bytes()
    workspace.initialize()
    manifest = workspace.archive(payload, kind="pano", person=person, device=device, stream=stream,
        settings=dict(source_timezone=source_timezone, collector_version=collector_version,
                      rule_note=rule_note))
    return ingest_pano(workspace, manifest, payload)


def ingest_pano(workspace, manifest, payload):
    person, device, stream = (manifest[k] for k in ("person_key", "device_key", "stream_name"))
    for value in (person, device, stream):
        identifier(value)
    stream_id = stable_id("pano", person, device, stream)
    complete_end = payload.rfind(b"\n") + 1
    with workspace.transaction() as conn:
        existing = begin_import(conn, manifest)
        if existing:
            return dict(import_id=manifest["id"], status="duplicate", inserted_records=0,
                        pending_bytes=existing["pending_bytes"], raw_ref=manifest["raw_ref"])
        state = conn.execute("SELECT * FROM streams WHERE id=?", (stream_id,)).fetchone()
        offset = state["committed_bytes"] if state else 0
        if state and complete_end < offset:
            _, previous = workspace.read_capture(workspace.raw / state["last_raw_ref"])
            if previous[:complete_end] == payload[:complete_end] and len(payload) <= len(previous) and previous[:len(payload)] == payload:
                return finish_import(conn, manifest, 0, len(payload)-complete_end, "older_prefix")
            raise MusicError("日志被截断或改写；未推进水位。轮换文件需使用新的 stream，并人工核对重叠。")
        if state and digest(payload[:offset]) != state["prefix_sha256"]:
            raise MusicError("已导入日志前缀变化；未覆盖旧记录。请检查复制完整性或使用独立 stream。")
        if not state:
            insert_row(conn, "streams", dict(id=stream_id, person_key=person, device_key=device,
                stream_name=stream, committed_bytes=0, prefix_sha256=digest(b""), last_raw_ref=manifest["raw_ref"]))
        position = offset
        count = 0
        timestamps = []
        for line in payload[offset:complete_end].splitlines(keepends=True):
            pointer = f"byte:{position}"
            record_id = f"{stream_id}:{position}"
            position += len(line)
            if not line.strip():
                continue
            row = parse_json(line)
            if not isinstance(row, dict):
                raise MusicError(f"{pointer} 不是 JSON 对象。")
            event = row.get("event", "scrobble")
            if event not in ("scrobble", "love", "unlove"):
                raise MusicError(f"{pointer} 含不支持的事件类型。")
            track = required_text(row.get("track"), "track")
            artist = required_text(row.get("artist"), "artist")
            timestamp = utc_ms(row.get("timeMs"))
            duration = row.get("durationMs")
            if duration is not None and (isinstance(duration, bool) or not isinstance(duration, int) or duration < 0):
                raise MusicError("durationMs 必须是非负整数或 null。")
            for key in ("album", "albumArtist", "mediaPlayerPackage", "mediaPlayerName", "mediaPlayerVersion"):
                if row.get(key) is not None and not isinstance(row[key], str):
                    raise MusicError(f"{key} 必须是文本或 null。")
            observation_id = stable_id("pano", record_id)
            recording_key = "pano-label:" + stable_id(track, artist, row.get("album"), row.get("albumArtist"))
            insert_row(conn, "observations", dict(id=observation_id, person_key=person, source="pano",
                account_key=None, device_key=device, stream_id=stream_id, source_record_id=record_id,
                event_kind=event, recording_key=recording_key, platform_track_id=None, track=track,
                artist=artist, album=row.get("album"), album_artist=row.get("albumArtist"),
                track_duration_ms=duration, actual_listened_seconds=None, occurred_at_utc=timestamp,
                observed_at_utc=manifest["observed_at_utc"], ingested_at_utc=now(),
                source_timezone=manifest["settings"].get("source_timezone"), time_precision="millisecond",
                time_basis="collector_play_start" if event == "scrobble" else "collector_action_time",
                player_package=row.get("mediaPlayerPackage"), player_name=row.get("mediaPlayerName"),
                player_version=row.get("mediaPlayerVersion"), import_id=manifest["id"], raw_pointer=pointer,
                quality_flags=json_text(["observed_only", "platform_identity_unresolved", "actual_duration_unknown"]),
                payload_json=json_text(row)))
            insert_row(conn, "observation_evidence", dict(observation_id=observation_id,
                       import_id=manifest["id"], raw_pointer=pointer))
            change(conn, "observation", observation_id, "insert", manifest["id"])
            timestamps.append(timestamp)
            count += 1
        pending = len(payload) - complete_end
        conn.execute("UPDATE streams SET committed_bytes=?,prefix_sha256=?,last_raw_ref=? WHERE id=?",
                     (complete_end, digest(payload[:complete_end]), manifest["raw_ref"], stream_id))
        insert_row(conn, "coverage", dict(id=stable_id(manifest["id"], "pano"), import_id=manifest["id"],
            person_key=person, source="pano", account_key=None, device_key=device, scope="local_log",
            status="observed_only", transport_status="pending_tail" if pending else "parsed",
            observed_at_utc=manifest["observed_at_utc"], min_event_at_utc=min(timestamps, default=None),
            max_event_at_utc=max(timestamps, default=None), record_count=count,
            detail="日志范围仅证明实际记录；无后台存活、全设备连续覆盖或实际聆听时长保证。"))
        return finish_import(conn, manifest, count, pending, "pending_tail" if pending else "imported")
