"""One catalog row per known NetEase ID, over the same published snapshot."""
from __future__ import annotations

import json
from collections import Counter

from .song_tags import latest_song_tags, song_id

FIELDS = ('genre', 'language', 'recommended_tags')


def song_catalog(conn, *, tags_available):
    metadata = []
    ids = {song_id(row[0]) for row in conn.execute("""SELECT platform_track_id FROM observations
        WHERE source='netease_recent' AND platform_track_id IS NOT NULL
        UNION SELECT platform_track_id FROM ranking_items
        UNION SELECT entity_id FROM library_items WHERE scope='likes'""").fetchall()}
    for table, condition in (('observations', "source='netease_recent' AND platform_track_id IS NOT NULL"),
                             ('ranking_items', 'TRUE')):
        album = 'album' if table == 'observations' else 'NULL'
        for key, track, artist, album, observed, row_id in conn.execute(f"""SELECT platform_track_id,
            track,artist,{album},observed_at_utc,id FROM {table} WHERE {condition}
            QUALIFY row_number() OVER (PARTITION BY platform_track_id ORDER BY observed_at_utc DESC,id DESC)=1""").fetchall():
            try:
                names = json.loads(artist)
                artist = ' / '.join(names) if isinstance(names, list) else artist
            except (ValueError, TypeError):
                pass
            metadata.append((observed, row_id, song_id(key), track, artist, album))
    # An older ID-only snapshot must not hide newer known song metadata.
    for key, payload, observed, row_id in conn.execute("""SELECT entity_id,payload_json,observed_at_utc,id
        FROM library_items WHERE scope='likes' AND json_extract_string(payload_json,'$.song.name') IS NOT NULL
        QUALIFY row_number() OVER (PARTITION BY entity_id ORDER BY observed_at_utc DESC,id DESC)=1""").fetchall():
        song = json.loads(payload)['song']
        album = song.get('al', song.get('album')) or {}
        artist = ' / '.join(item['name'] for item in song.get('ar', song.get('artists', [])))
        metadata.append((observed, row_id, song_id(key), song['name'], artist, album.get('name')))
    known = {}
    for _, _, key, track, artist, album in sorted(metadata, key=lambda item: item[:2], reverse=True):
        if track:
            known.setdefault(key, dict(track=track, artist=artist, album=album))
    labels = latest_song_tags(conn) if tags_available else {}
    catalog = []
    for key in sorted(ids, key=int):
        label = labels.get(key, {})
        fields = label.get('fields', {})
        catalog.append(dict(platform_track_id=key, **known.get(key, dict(track=None, artist=None, album=None)),
            tags={field: fields.get(field, []) for field in FIELDS},
            tag_status=label.get('status', 'uncollected'), tag_data_status=label.get('data_status'),
            tag_observed_at=label.get('observed_at'), tag_latest_attempt_at=label.get('latest_attempt_at')))
    return catalog


def catalog_facets(catalog):
    facets = {}
    for field in FIELDS:
        counts = Counter(label for row in catalog for label in set(row['tags'][field]))
        facets[field] = [dict(value=label, songs=counts[label]) for label in sorted(counts)]
    return dict(total=len(catalog), facets=facets,
        states=dict(Counter(row['tag_status'] for row in catalog)))


def matching_songs(catalog, genre='', language='', tag='', q=''):
    selected = dict(genre=genre, language=language, recommended_tags=tag)
    needle = q.casefold().strip()
    return {row['platform_track_id']: row for row in catalog
        if all(not value or value in row['tags'][field] for field, value in selected.items())
        and (not needle or needle in ' '.join(str(row.get(key) or '')
            for key in ('track', 'artist', 'album', 'platform_track_id')).casefold())}
