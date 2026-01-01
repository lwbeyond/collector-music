"""Source-backed song labels; no model inference or name-based identity matching."""
from __future__ import annotations

from datetime import datetime
import math
from pathlib import Path
import re

from .common import MusicError, identifier, json_text, parse_json, stable_id, utc_time
from .netease import platform_id, reject_credentials
from .store import begin_import, change, finish_import, insert_row

FORMAT = 'collector-music-netease-song-tags'
SOURCE = 'netease'
FIELD_TITLES = {'曲风': 'genre', '语种': 'language', 'BPM': 'bpm', '推荐标签': 'recommended_tags'}


def song_id(value):
    """Canonical numeric platform ID, never a title or an inferred match."""
    return str(int(platform_id(value)))


def extract_labels(body):
    """Read the song encyclopedia BASIC block, excluding related recommendations."""
    if not isinstance(body, dict) or body.get('code') != 200:
        raise MusicError('歌曲百科返回非成功状态。')
    data = body.get('data')
    if not isinstance(data, dict) or not isinstance(data.get('blocks'), list):
        raise MusicError('歌曲百科 blocks 格式不符。')

    def object_or_empty(value):
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise MusicError('歌曲百科字段格式不符。')
        return value

    def list_or_empty(value):
        if value is None:
            return []
        if not isinstance(value, list):
            raise MusicError('歌曲百科字段列表格式不符。')
        return value

    fields = {field: [] for field in FIELD_TITLES.values()}
    found_basic = False
    for block in data['blocks']:
        if not isinstance(block, dict) or block.get('code') != 'SONG_PLAY_ABOUT_SONG_BASIC':
            continue
        found_basic = True
        for creative in list_or_empty(block.get('creatives')):
            if not isinstance(creative, dict):
                continue
            ui = object_or_empty(creative.get('uiElement'))
            title = object_or_empty(ui.get('mainTitle')).get('title')
            if not isinstance(title, str):
                continue
            field = FIELD_TITLES.get(title)
            if field is None:
                continue
            values = []
            for resource in list_or_empty(creative.get('resources')):
                if isinstance(resource, dict):
                    resource_ui = object_or_empty(resource.get('uiElement'))
                    values.append(object_or_empty(resource_ui.get('mainTitle')).get('title'))
            for link in list_or_empty(ui.get('textLinks')):
                if isinstance(link, dict):
                    values.append(link.get('text'))
            for value in values:
                if isinstance(value, str) and value.strip() and value.strip() not in ('暂无', '未知', '--'):
                    value = value.strip()
                    if value not in fields[field]:
                        fields[field].append(value)
    bpm = []
    for value in fields['bpm']:
        if re.fullmatch(r'\d+(?:\.\d+)?', value):
            number = float(value)
            if math.isfinite(number) and number > 0 and number not in bpm:
                bpm.append(number)
    fields['bpm_raw'] = fields['bpm']
    fields['bpm'] = bpm
    return dict(basic_block_present=found_basic, fields=fields,
        available={field: bool(fields[field]) for field in FIELD_TITLES.values()})


def normalize_song_tags(body):
    normalized = extract_labels(body)
    return ('ok' if any(normalized['available'].values()) else 'empty'), normalized


def catalog_tracks(conn, person='me'):
    """Distinct known NetEase IDs across all archived observations, rankings and likes.

    Source counts overlap. Unmatched Pano counts use recording keys and are not
    identified musical works; no title/artist matching is performed here.
    """
    identifier(person, 'person')
    queries = {
        'observations': "SELECT DISTINCT platform_track_id FROM observations WHERE person_key=? AND source='netease_recent' AND platform_track_id IS NOT NULL",
        'rankings': "SELECT DISTINCT platform_track_id FROM ranking_items WHERE person_key=?",
        'likes_history': "SELECT DISTINCT entity_id FROM library_items WHERE person_key=? AND scope='likes'",
    }
    pools = {name: {song_id(row[0]) for row in conn.execute(sql, (person,)).fetchall()}
             for name, sql in queries.items()}
    ids = set().union(*pools.values())
    unmatched = conn.execute("""SELECT count(DISTINCT recording_key),count(*) FROM observations
        WHERE person_key=? AND source='pano' AND platform_track_id IS NULL""", (person,)).fetchone()
    return dict(song_ids=sorted(ids, key=int), total=len(ids),
        sources={name: len(pool) for name, pool in pools.items()},
        unmatched_pano_count=unmatched[0], unmatched_pano_observations=unmatched[1])


def pending_song_ids(conn, person='me'):
    """Successful empty responses count as fetched; later failures keep prior cache."""
    catalog = catalog_tracks(conn, person)
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='song_tags'").fetchone():
        return catalog['song_ids']
    cached = {song_id(row[0]) for row in conn.execute("""SELECT DISTINCT platform_track_id FROM song_tags
        WHERE source=? AND status IN ('ok','empty')""", (SOURCE,)).fetchall()}
    return [key for key in catalog['song_ids'] if key not in cached]


def latest_song_tags(conn):
    """Global per-song metadata cache with failure state, safe for dashboard consumers."""
    cursor = conn.execute("""SELECT platform_track_id,observed_at_utc,status,normalized_json,import_id,id
        FROM song_tags WHERE source=?
        ORDER BY observed_at_utc DESC,import_id DESC,id DESC""", (SOURCE,))
    names = [item[0] for item in cursor.description]
    latest, successes = {}, {}
    for row in cursor.fetchall():
        record = dict(zip(names, row))
        key = record['platform_track_id']
        latest.setdefault(key, record)
        if record['status'] in ('ok', 'empty'):
            successes.setdefault(key, record)
    result = {}
    for key, attempt in latest.items():
        good = successes.get(key)
        value = parse_json(good['normalized_json']) if good else {}
        result[key] = dict(value, source=SOURCE, platform_track_id=key,
            status=('stale' if attempt['status'] == 'failed' else good['status']) if good else 'unavailable',
            data_status=good['status'] if good else None,
            observed_at=good['observed_at_utc'] if good else None,
            latest_attempt_at=attempt['observed_at_utc'])
    return result


def validate_document(document):
    reject_credentials(document)
    if not isinstance(document, dict) or document.get('format') != FORMAT or document.get('version') != 1:
        raise MusicError('歌曲标签快照契约不匹配。')
    account = platform_id(document.get('account_key'))
    collected = utc_time(document.get('collected_at_utc'))
    songs = document.get('songs')
    if not isinstance(songs, list) or not songs:
        raise MusicError('歌曲标签快照必须包含非空歌曲尝试列表。')
    seen = set()
    for capture in songs:
        if not isinstance(capture, dict) or capture.get('status') not in ('ok', 'failed'):
            raise MusicError('歌曲标签采集状态无效。')
        key = song_id(capture.get('song_id'))
        if key in seen:
            raise MusicError('同一歌曲标签快照含重复平台歌曲 ID。')
        seen.add(key)
        observed = utc_time(capture.get('observed_at_utc'))
        if datetime.fromisoformat(observed) > datetime.fromisoformat(collected):
            raise MusicError('歌曲标签观测时间晚于采集完成时间。')
    return account, collected, songs


def import_song_tags(workspace, path: Path, *, person='me'):
    identifier(person, 'person')
    if path.stat().st_size > 64 * 1024 * 1024:
        raise MusicError('歌曲标签快照超过 64 MiB。')
    payload = path.read_bytes()
    document = parse_json(payload)
    account, collected, _ = validate_document(document)
    workspace.initialize()
    manifest = workspace.archive(payload, kind='netease_song_tags', person=person, account=account,
        collected_at=collected, settings={'adapter_version': document.get('adapter_version')})
    return ingest_song_tags(workspace, manifest, payload)


def ingest_song_tags(workspace, manifest, payload):
    document = parse_json(payload)
    account, collected, songs = validate_document(document)
    if (manifest['kind'] != 'netease_song_tags' or account != manifest['account_key']
            or collected != manifest['collected_at']):
        raise MusicError('歌曲标签归档身份或采集时间不匹配。')
    person = identifier(manifest['person_key'], 'person')
    statuses = {}
    with workspace.transaction() as conn:
        if begin_import(conn, manifest):
            return dict(import_id=manifest['id'], status='duplicate', inserted_records=0)
        bound = conn.execute("SELECT person_key FROM source_accounts WHERE source='netease' AND account_key=?", (account,)).fetchone()
        if bound and bound[0] != person:
            raise MusicError('此网易云账号已绑定另一个 person_key。')
        conn.execute("INSERT OR IGNORE INTO source_accounts VALUES('netease',?,?)", (account, person))
        for capture in songs:
            track_id = song_id(capture['song_id'])
            state, normalized = 'failed', {}
            if capture['status'] == 'ok':
                try:
                    state, normalized = normalize_song_tags(capture.get('body'))
                except MusicError as error:
                    normalized = {'error': str(error)}
            key = stable_id(manifest['id'], SOURCE, track_id)
            insert_row(conn, 'song_tags', dict(id=key, import_id=manifest['id'], person_key=person,
                account_key=account, source=SOURCE, platform_track_id=track_id,
                observed_at_utc=utc_time(capture['observed_at_utc']), status=state,
                normalized_json=json_text(normalized), payload_json=json_text(capture)))
            change(conn, 'song_tags', key, state, manifest['id'])
            statuses[track_id] = state
        result = finish_import(conn, manifest, len(statuses),
            status='partial' if 'failed' in statuses.values() else 'imported')
    return dict(result, songs=statuses)
