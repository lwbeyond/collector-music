import unittest
from collector_music.overlap import candidates


def event(id, source, **changes):
    row = dict(id=id, person_key='me', source=source,
        event_kind='scrobble' if source=='pano' else 'recent_observation',
        player_package='com.netease.cloudmusic' if source=='pano' else None,
        track='Fixture Song', artist='Fixture Artist', album='Fixture Album',
        track_duration_ms=200000, occurred_at_utc='2026-01-01T12:00:00.000+00:00',
        platform_track_id=None if source=='pano' else '1000001')
    return dict(row, **changes)


class OverlapTests(unittest.TestCase):
    def test_near_identical_metadata_is_only_a_candidate_not_an_event_rewrite(self):
        left=event('p','pano')
        right=event('n','netease_recent',occurred_at_utc='2026-01-01T12:00:04.806+00:00')
        links=candidates([left,right])
        self.assertEqual(len(links),1)
        self.assertEqual((links[0]['status'],links[0]['time_delta_ms']),('candidate',4806))
        self.assertIsNone(left['platform_track_id'])
        self.assertEqual(left['source'],'pano')

    def test_eighteen_minute_difference_and_actual_repeats_are_not_collapsed(self):
        left=event('p','pano',track='Another Fixture',occurred_at_utc='2026-01-01T13:18:28.692+00:00')
        right=event('n','netease_recent',track='Another Fixture',occurred_at_utc='2026-01-01T13:00:00.000+00:00')
        self.assertEqual(candidates([left,right]),[])
        self.assertEqual(candidates([event('p1','pano'),event('p2','pano')]),[])

    def test_ambiguous_multiple_devices_are_retained_as_multiple_candidates(self):
        links=candidates([event('phone','pano'),event('tablet','pano'),event('n','netease_recent')])
        self.assertEqual(len(links),2)
        self.assertTrue(all(link['status']=='ambiguous' for link in links))

    def test_missing_evidence_conflicting_identity_and_other_players_do_not_match(self):
        for changes in [dict(person_key='someone-else'),dict(album=None),dict(artist='Another artist'),
                        dict(player_package='another.player'),dict(event_kind='love'),
                        dict(track_duration_ms=None),dict(track_duration_ms=123000),
                        dict(occurred_at_utc=None),dict(occurred_at_utc='2026-01-01T12:00:00'),
                        dict(platform_track_id='another-version')]:
            with self.subTest(changes=changes):
                self.assertEqual(candidates([event('p','pano',**changes),event('n','netease_recent')]),[])
