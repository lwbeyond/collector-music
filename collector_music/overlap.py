"""Conservative display-only cross-source candidates; never merges play events."""
from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import datetime
import unicodedata

RULE_VERSION = "metadata-time-candidate-1"
WINDOW_SECONDS = 10
DURATION_TOLERANCE_MS = 2000


def normalized(value):
    return " ".join(unicodedata.normalize("NFC", value or "").casefold().split())


def identity(row):
    parts = (row.get("person_key"), normalized(row.get("track")),
             normalized(row.get("artist")), normalized(row.get("album")))
    return parts if all(parts) else None


def event_time(row):
    try:
        value = datetime.fromisoformat(row["occurred_at_utc"].replace("Z", "+00:00"))
        return value.timestamp() if value.utcoffset() is not None else None
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


def candidates(records):
    """Input artists are display labels. Results reference unchanged observation IDs."""
    recent = defaultdict(list)
    for row in records:
        key, time = identity(row), event_time(row)
        if row.get("source") == "netease_recent" and row.get("event_kind") == "recent_observation" and key and time is not None:
            recent[key].append((time, row))
    indexed = {}
    for key, values in recent.items():
        values.sort(key=lambda item: (item[0], item[1]["id"]))
        indexed[key] = ([item[0] for item in values], values)
    links = []
    degree = defaultdict(int)
    for row in records:
        if row.get("source") != "pano" or row.get("event_kind") != "scrobble" or row.get("player_package") != "com.netease.cloudmusic":
            continue
        key, time = identity(row), event_time(row)
        if key not in indexed or time is None:
            continue
        times, values = indexed[key]
        for other_time, other in values[bisect_left(times, time-WINDOW_SECONDS):bisect_right(times, time+WINDOW_SECONDS)]:
            durations = (row.get("track_duration_ms"), other.get("track_duration_ms"))
            if not all(type(value) in (int, float) and value > 0 for value in durations):
                continue
            if abs(durations[0]-durations[1]) > DURATION_TOLERANCE_MS:
                continue
            if row.get("platform_track_id") and row["platform_track_id"] != other.get("platform_track_id"):
                continue
            link = dict(left_id=row["id"], right_id=other["id"],
                        time_delta_ms=round(abs(time-other_time)*1000), rule_version=RULE_VERSION)
            links.append(link)
            degree[row["id"]] += 1
            degree[other["id"]] += 1
    for link in links:
        link["status"] = "candidate" if degree[link["left_id"]] == degree[link["right_id"]] == 1 else "ambiguous"
    return sorted(links, key=lambda link: (link["left_id"], link["right_id"]))
