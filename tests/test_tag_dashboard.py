from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from collector_music.common import json_bytes
from collector_music.dashboard import DashboardStore, PublicationChanged
from collector_music.lake import publish
from collector_music.netease import import_netease
from collector_music.pano import import_pano
from collector_music.song_tags import import_song_tags
from collector_music.store import Workspace
from test_pipeline import ROOT, netease_document, line, pano_row


def make_tag_fixture(workspace, today):
    """Same-title IDs, repeated evidence, unplayed likes and explicit tag states."""
    current = datetime.fromisoformat(today).replace(tzinfo=timezone.utc)
    document = netease_document()
    songs = [dict(id=key, name='Same title' if key < 125 else f'Song {key}',
        ar=[dict(id=1, name='Artist A' if key != 124 else 'Artist B')], al=dict(id=2, name='Album'), dt=120000)
        for key in range(123, 139)]
    entries = [dict(playTime=int((current - timedelta(days=day)).timestamp() * 1000), data=deepcopy(songs[key - 123]))
               for key, day in [(123, 1), (123, 2), (124, 1), (126, 1), (127, 2), (128, 1), (123, 10)]]
    document['scopes']['recent']['body']['data']['list'] = entries
    document['scopes']['likes']['body'].update(ids=[song['id'] for song in songs], songs=songs, detail_status='ok')
    path = workspace.home / 'snapshot.json'
    path.write_bytes(json_bytes(document))
    import_netease(workspace, path)
    document['collected_at_utc'] = '2026-10-03T01:00:00Z'
    path.write_bytes(json_bytes(document))
    import_netease(workspace, path)  # Repeated evidence must not inflate plays/catalog.
    pano = workspace.home / 'pano.jsonl'
    pano.write_bytes(line(dict(pano_row(), timeMs=entries[0]['playTime'], track='Same title', artist='Artist A')))
    import_pano(workspace, pano, device='fixture', stream='fixture')

    def tagged(key, fields):
        creatives = [dict(uiElement=dict(mainTitle=dict(title=title)),
            resources=[dict(uiElement=dict(mainTitle=dict(title=value))) for value in values])
            for title, values in fields.items()]
        return dict(song_id=str(key), status='ok', observed_at_utc='2026-10-03T01:00:00Z',
            body=dict(code=200, data=dict(blocks=[dict(code='SONG_PLAY_ABOUT_SONG_BASIC', creatives=creatives)])))

    captures = [tagged(123, {'曲风': ['流行', '摇滚', '流行'], '语种': ['英语'], '推荐标签': ['放松', '活力']}),
        tagged(124, {'曲风': ['流行'], '语种': ['汉语'], '推荐标签': ['活力']}),
        tagged(125, {'曲风': ['爵士'], '语种': ['英语'], '推荐标签': ['放松']}), tagged(126, {}),
        dict(song_id='128', status='failed', observed_at_utc='2026-10-03T01:00:00Z', error='fixture')]
    path = workspace.home / 'tags.json'
    path.write_bytes(json_bytes(dict(format='collector-music-netease-song-tags', version=1, account_key='123456',
        collected_at_utc='2026-10-03T01:01:00Z', songs=captures)))
    import_song_tags(workspace, path)
    publish(workspace)


class TagDashboardTests(unittest.TestCase):
    def setUp(self):
        (ROOT / '.tmp').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / '.tmp')
        self.workspace = Workspace(Path(self.temp.name)).initialize()
        today = datetime.now().date().isoformat()
        make_tag_fixture(self.workspace, today)
        self.store = DashboardStore(self.workspace.home)
        self.data = self.store.read()

    def tearDown(self):
        self.temp.cleanup()

    def query(self, **filters):
        return self.store.analyze({key: [value] for key, value in dict(generation=self.data['generation'], period='7', **filters).items()})

    def test_catalog_is_unique_across_sources_and_includes_unplayed_songs(self):
        data = self.query()
        self.assertEqual(data['catalog_count'], 16)
        self.assertEqual(len({row['platform_track_id'] for row in data['catalog']}), 16)
        self.assertEqual(data['summary']['total'], 6)
        self.assertEqual(data['catalog_observed'], 5)
        self.assertEqual(self.data['song_catalog']['facets']['genre'],
            [{'value':'摇滚','songs':1}, {'value':'流行','songs':2}, {'value':'爵士','songs':1}])
        self.assertEqual(self.query(genre='爵士')['summary']['total'], 0)
        self.assertEqual(self.query(genre='爵士')['catalog_count'], 1)

    def test_filters_intersect_without_multiplying_multilabel_plays(self):
        data = self.query(genre='流行', language='英语', tag='活力')
        self.assertEqual(data['summary']['total'], 2)
        self.assertEqual(sum(data['hours']), 2)
        self.assertEqual(sum(map(sum, data['heatmap'])), 2)
        self.assertEqual(data['catalog_count'], 1)
        self.assertEqual(data['catalog'][0]['platform_track_id'], '123')
        self.assertEqual(data['comparison']['total'], 1)
        self.assertEqual(self.query(genre='流行')['summary']['total'], 3)
        self.assertEqual(self.query(genre='流行', language='汉语')['summary']['total'], 1)
        self.assertEqual(self.query(genre='爵士', tag='活力')['catalog_count'], 0)

    def test_states_and_unlinked_pano_remain_distinct(self):
        by_id = {row['platform_track_id']: row for row in self.query()['catalog']}
        self.assertEqual(by_id['126']['tag_status'], 'empty')
        self.assertEqual(by_id['127']['tag_status'], 'uncollected')
        self.assertEqual(by_id['128']['tag_status'], 'unavailable')
        self.assertEqual(self.query(source='pano')['summary']['total'], 1)
        data = self.query(source='pano', genre='流行')
        self.assertEqual(data['summary']['total'], 0)
        self.assertEqual(data['tag_unlinked_records'], 1)

    def test_search_identity_and_request_validation(self):
        self.assertEqual(self.query(q='same TITLE')['catalog_count'], 2)
        self.assertEqual(self.query(q='123')['summary']['total'], 2)
        self.assertEqual(self.query(q='<script>')['catalog_count'], 0)
        self.assertEqual(self.query(source='pano', q='   ')['summary']['total'], 1)
        with self.assertRaises(ValueError):
            self.query(genre='x' * 201)
        with self.assertRaises(ValueError):
            self.store.analyze(dict(generation=[self.data['generation']], genre=['流行','摇滚']))
        with self.assertRaises(PublicationChanged):
            self.store.analyze(dict(generation=['old'], genre=['流行']))
