from __future__ import annotations

import unittest

from collector_music.analytics import AnalyticsIndex


def observation(key="a", time="2026-10-01T16:30:00Z", **fields):
    return dict(dict(source="pano", event_kind="scrobble", recording_key=key,
        occurred_at_utc=time, platform_track_id=None, track="同名歌曲", artist="歌手 A", album="专辑"), **fields)


class AnalyticsTests(unittest.TestCase):
    def index(self, items, timezone="Asia/Shanghai"):
        return AnalyticsIndex(items, timezone)

    def query(self, items, **options):
        return self.index(items).query(today="2026-10-03", source="pano", **options)

    def test_full_history_over_record_limit_and_source_action_separation(self):
        items = [observation()] * 6001 + [
            observation(event_kind="love"), observation(event_kind="unlove"),
            observation(source="netease_recent", event_kind="recent_observation", artist='["其他艺人"]'),
            observation(source="pano", event_kind="recent_observation")]
        data = self.query(items, period="all")
        self.assertEqual(data["summary"]["total"], 6001)
        self.assertEqual(data["summary"]["unique_tracks"], 1)
        self.assertEqual(data["summary"]["repeat_records"], 6000)
        self.assertEqual(data["hours"][0], 6001)
        self.assertEqual(data["heatmap"][4][0], 6001)
        self.assertEqual(data["artists"][0]["share"], 100)
        self.assertEqual(data["daily"], [dict(date="2026-10-02", records=6001)])

    def test_local_day_custom_inclusive_boundaries_and_distinct_versions(self):
        data = self.query([
            observation(time="2026-10-01T15:59:59Z"),
            observation(time="2026-10-01T16:00:00Z"),
            observation("b", "2026-10-02T15:59:59Z"),
            observation(time="2026-10-02T16:00:00Z")
        ], period="custom", start="2026-10-02", end="2026-10-02")
        s = data["summary"]
        self.assertEqual((s["total"], s["active_days"], s["unique_tracks"]), (2, 1, 2))
        self.assertEqual(s["diversity_percent"], 100)
        self.assertEqual(s["daily_average"], 2)
        self.assertEqual(s["night_percent"], 50)
        self.assertEqual(data["comparison"]["total"], 1)
        self.assertEqual(data["comparison"]["change_percent"], 100)

    def test_dst_fall_back_counts_both_observations_in_same_local_hour(self):
        index = self.index([observation(time=t) for t in ["2026-11-01T05:30:00Z", "2026-11-01T06:30:00Z"]], "America/New_York")
        data = index.query(today="2026-11-01", source="pano", period="all")
        self.assertEqual(data["hours"][1], 2)
        self.assertEqual(data["heatmap"][6][1], 2)
        self.assertEqual(data["summary"]["active_days"], 1)

    def test_missing_bad_naive_and_future_timestamps_do_not_become_midnight(self):
        data = self.query([observation(time=t) for t in [None, "bad", "2026-10-03T01:00:00", "2026-10-03T16:00:00Z"]])
        self.assertEqual(data["excluded"], dict(invalid_time=3, future=1))
        self.assertEqual(data["summary"]["total"], 0)
        self.assertEqual(data["hours"], [0] * 24)
        self.assertIsNone(data["summary"]["daily_average"])
        self.assertIsNone(data["summary"]["diversity_percent"])
        self.assertIsNone(data["summary"]["peak_date"])
        self.assertIsNone(data["comparison"]["change_percent"])

    def test_streaks_peak_ties_and_exact_artist_drilldown(self):
        data = self.query([observation(time=f"2026-09-{day:02d}T01:00:00Z") for day in [26, 27, 29, 30]] +
            [observation("b", "2026-10-01T00:00:00Z", artist="歌手 A / 歌手 B")], period="all")
        self.assertEqual(data["summary"]["longest_streak"], 3)
        self.assertEqual(data["summary"]["peak_date"], "2026-09-26")
        self.assertEqual(data["summary"]["unique_artists"], 2)
        self.assertIsNone(data["comparison"])
        details = self.query([observation(), observation("b", artist="歌手 A / 歌手 B"),
            observation("c", artist="歌手 A2")], artist="歌手 A")
        self.assertEqual(details["summary"]["total"], 1)
        self.assertEqual(len(details["tracks"]), 1)

    def test_artist_json_and_unknown_labels_and_top_ten_share_denominator(self):
        items = [observation(str(i), artist=f'歌手 {i}') for i in range(12)] + [observation('unknown', artist='')]
        data = self.query(items)
        self.assertEqual(len(data["artists"]), 10)
        self.assertEqual(len(data["tracks"]), 10)
        self.assertEqual(data["summary"]["unique_artists"], 12)
        self.assertEqual(data["summary"]["unknown_artist_records"], 1)
        self.assertEqual(data["artists"][0]["share"], 7.7)
        index = self.index([observation(source="netease_recent", event_kind="recent_observation", artist='["A", "B"]')])
        self.assertEqual(index.query(today="2026-10-03")["artists"][0]["artist"], "A / B")

    def test_invalid_filters_rejected(self):
        for options in [dict(source="all"), dict(period="0"), dict(period="custom"),
                        dict(period="custom", start="2026-10-03", end="2026-10-02"),
                        dict(period="custom", start="2026-10-01", end="2026-10-04"),
                        dict(period="custom", start="20261001", end="2026-10-03"),
                        dict(period="30", start="2026-10-01")]:
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.index([]).query(today="2026-10-03", **options)
