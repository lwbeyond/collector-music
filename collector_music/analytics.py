"""Full-publication listening analytics; never sum different evidence sources."""
from __future__ import annotations

import json
from collections import Counter
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .song_catalog import matching_songs


SOURCES = {"pano": "scrobble", "netease_recent": "recent_observation"}
PERIODS = {"7", "30", "90", "365", "all", "custom"}


class AnalysisQueryError(ValueError):
    """Invalid user-supplied filters, distinct from publication read errors."""


def artist_label(value, source="netease"):
    if source != "pano":
        try:
            names = json.loads(value)
            if isinstance(names, list) and all(isinstance(x, str) for x in names):
                return " / ".join(names) or "艺人未提供"
        except (ValueError, TypeError):
            pass
    return value or "艺人未提供"


class AnalyticsIndex:
    """Compact counts by source, local day/hour and source recording identity.

    Built once from all verified observations. Repeated plays remain counts,
    actions are excluded, and metadata never crosses source identities.
    """

    def __init__(self, observations, timezone, catalog=None):
        zone = ZoneInfo(timezone)
        self.timezone = timezone
        self.buckets = {source: Counter() for source in SOURCES}
        self.tracks = {}
        self.invalid_times = Counter()
        self.catalog = catalog or []
        for item in observations:
            source = item["source"]
            if source not in SOURCES or item["event_kind"] != SOURCES[source]:
                continue
            try:
                timestamp = datetime.fromisoformat(item["occurred_at_utc"])
                if timestamp.tzinfo is None:
                    raise ValueError("Timestamp must have a timezone")
                local = timestamp.astimezone(zone)
            except (TypeError, ValueError, OverflowError):
                self.invalid_times[source] += 1
                continue
            key = item["recording_key"]
            identity = (source, key)
            # Input is newest first: keep the latest label for each recording.
            if identity not in self.tracks:
                self.tracks[identity] = dict(
                    recording_key=key, platform_track_id=item["platform_track_id"],
                    track=item["track"], artist=artist_label(item["artist"], source), album=item["album"])
            self.buckets[source][(local.date(), local.hour, key)] += 1

    def query(self, *, today, source="netease_recent", period="30", start=None, end=None, artist=None,
              genre='', language='', tag='', q=''):
        if source not in SOURCES or period not in PERIODS:
            raise AnalysisQueryError("请选择有效的来源和时间范围。")
        filters = dict(genre=genre, language=language, tag=tag, q=q)
        if any(not isinstance(value, str) or len(value) > 200 for value in filters.values()):
            raise AnalysisQueryError("歌曲筛选参数应为不超过 200 字的文本。")
        filters = {key: value.strip() for key, value in filters.items()}
        selected = matching_songs(self.catalog, **filters)
        filtered = any(filters.values())
        today = date.fromisoformat(today)
        buckets = self.buckets[source]
        available = [day for day, _, _ in buckets if day <= today]
        if period == "custom":
            try:
                first, last = date.fromisoformat(start), date.fromisoformat(end)
                if first.isoformat() != start or last.isoformat() != end:
                    raise ValueError()
            except (ValueError, TypeError):
                raise AnalysisQueryError("请提供有效的起止日期（YYYY-MM-DD）。") from None
            if first > last or last > today:
                raise AnalysisQueryError("开始日期不能晚于结束日期，结束日期不能晚于今天。")
        else:
            if start is not None or end is not None:
                raise AnalysisQueryError("起止日期仅用于自定义范围。")
            last = today
            first = min(available, default=today - timedelta(days=29)) if period == "all" else today - timedelta(days=int(period) - 1)
        days = (last - first).days + 1
        daily, hours, artists, tracks = Counter(), [0] * 24, Counter(), Counter()
        heatmap = [[0] * 24 for _ in range(7)]
        previous_total, previous_days = 0, set()
        previous_end = first.toordinal() - 1
        previous_start = previous_end - days + 1
        song_counts, unlinked = Counter(), 0
        for (day, hour, key), n in buckets.items():
            track = self.tracks[source, key]
            label = track["artist"]
            track_id = track['platform_track_id'] if source == 'netease_recent' else None
            if first <= day <= last and not track_id:
                unlinked += n
            if filtered and track_id not in selected:
                continue
            if artist is not None and label != artist:
                continue
            if first <= day <= last:
                daily[day.isoformat()] += n
                hours[hour] += n
                heatmap[day.weekday()][hour] += n
                tracks[key] += n
                if track_id:
                    song_counts[track_id] += n
                if label != "艺人未提供":
                    artists[label] += n
            elif period != "all" and previous_start <= day.toordinal() <= previous_end:
                previous_total += n
                previous_days.add(day)
        total = sum(hours)
        ordered_days = sorted(daily)
        peak = min(daily, key=lambda d: (-daily[d], d)) if daily else None
        streak, longest, previous = 0, 0, None
        for day in ordered_days:
            current = date.fromisoformat(day).toordinal()
            streak = streak + 1 if previous is not None and current == previous + 1 else 1
            longest, previous = max(longest, streak), current
        ranked_artists = sorted(artists.items(), key=lambda pair: (-pair[1], pair[0]))
        ranked_tracks = sorted(tracks.items(), key=lambda pair: (-pair[1], self.tracks[source, pair[0]]["track"], pair[0]))
        comparison = None
        if period != "all" and previous_start >= 1:
            comparison = dict(start=date.fromordinal(previous_start).isoformat(), end=date.fromordinal(previous_end).isoformat(),
                total=previous_total, active_days=len(previous_days), difference=total - previous_total,
                change_percent=round((total - previous_total) / previous_total * 100, 1) if previous_total else None)
        catalog = [dict(row, records=song_counts[row['platform_track_id']]) for row in selected.values()
                   if artist is None or row['artist'] == artist]
        catalog.sort(key=lambda row: (-row['records'], row['track'] or '', row['platform_track_id']))
        return dict(source=source, period=period, start=first.isoformat(), end=last.isoformat(), timezone=self.timezone,
            filters=filters, catalog=catalog, catalog_count=len(catalog),
            catalog_observed=sum(row['records'] > 0 for row in catalog), tag_unlinked_records=unlinked,
            artist=artist, available_start=min(available).isoformat() if available else None,
            available_end=max(available).isoformat() if available else None,
            excluded=dict(invalid_time=self.invalid_times[source], future=sum(n for (d, _, _), n in buckets.items() if d > today)),
            summary=dict(total=total, calendar_days=days, active_days=len(daily), unique_tracks=len(tracks),
                unique_artists=len(artists), daily_average=round(total / len(daily), 1) if daily else None,
                diversity_percent=round(len(tracks) / total * 100, 1) if total else None,
                repeat_records=total - len(tracks), night_records=sum(hours[:6]),
                night_percent=round(sum(hours[:6]) / total * 100, 1) if total else None,
                longest_streak=longest, peak_date=peak, peak_count=daily[peak] if peak else 0,
                unknown_artist_records=total - sum(artists.values())),
            daily=[dict(date=d, records=daily[d]) for d in ordered_days],
            hours=hours, heatmap=heatmap,
            artists=[dict(artist=name, records=n, share=round(n / total * 100, 1)) for name, n in ranked_artists[:10]],
            tracks=[dict(self.tracks[source, key], records=n, share=round(n / total * 100, 1)) for key, n in ranked_tracks[:10]],
            comparison=comparison)
