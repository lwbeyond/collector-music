"""Copyable reader for a fixed music Parquet publication. Requires duckdb."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

DATASETS = {"imports", "observations", "observation_evidence", "ranking_items",
            "library_items", "coverage", "changes", "daily_music_features"}
DATASETS_BY_VERSION = {"1.0.0": DATASETS, "1.1.0": DATASETS | {"platform_reports"},
                       "1.2.0": DATASETS | {"platform_reports", "song_tags"}}
ANALYSIS_VIEW_VERSION = "1.2.0"

# Derived conveniences over one pinned generation, never written back to facts.
ANALYSIS_VIEWS = {
    "analysis_imports": """SELECT * FROM {s}.imports WHERE {cutoff}""",
    "latest_source_attempt": """SELECT c.* FROM {s}.coverage c
        JOIN {s}.analysis_imports i ON i.id=c.import_id
        QUALIFY row_number() OVER (PARTITION BY c.person_key,c.source,c.account_key,c.device_key,c.scope
            ORDER BY c.observed_at_utc DESC,c.import_id DESC)=1""",
    "latest_successful_source": """SELECT c.* FROM {s}.coverage c
        JOIN {s}.analysis_imports i ON i.id=c.import_id
        WHERE c.transport_status IN ('ok','parsed','pending_tail')
        QUALIFY row_number() OVER (PARTITION BY c.person_key,c.source,c.account_key,c.device_key,c.scope
            ORDER BY c.observed_at_utc DESC,c.import_id DESC)=1""",
    "library_snapshots": """SELECT c.*,i.observed_at_utc AS capture_completed_at_utc,
        (c.transport_status='ok') AS relationship_complete FROM {s}.coverage c
        JOIN {s}.analysis_imports i ON i.id=c.import_id
        WHERE c.source='netease' AND c.scope IN ('likes','playlists')""",
    "library_membership_history": """SELECT l.* FROM {s}.library_items l
        JOIN {s}.analysis_imports i ON i.id=l.import_id""",
    "ranking_history": """SELECT r.* FROM {s}.ranking_items r
        JOIN {s}.analysis_imports i ON i.id=r.import_id""",
    "library_changes": """WITH snapshots AS (
        SELECT person_key,account_key,scope,import_id,observed_at_utc,capture_completed_at_utc,
            lag(import_id) OVER w AS previous_import_id,
            lag(observed_at_utc) OVER w AS previous_observed_at_utc
        FROM {s}.library_snapshots WHERE relationship_complete
        WINDOW w AS (PARTITION BY person_key,account_key,scope ORDER BY observed_at_utc,import_id)
        ), added AS (
        SELECT c.person_key,c.account_key,c.scope,l.entity_id,
            CASE WHEN c.previous_import_id IS NULL THEN 'baseline' ELSE 'observed_added' END AS change_kind,
            c.previous_observed_at_utc AS interval_start_utc,c.capture_completed_at_utc AS interval_end_utc,
            c.previous_import_id,c.import_id
        FROM snapshots c JOIN {s}.library_items l ON l.import_id=c.import_id AND l.scope=c.scope
        WHERE NOT EXISTS (SELECT 1 FROM {s}.library_items p WHERE p.import_id=c.previous_import_id
            AND p.scope=c.scope AND p.entity_id=l.entity_id)
        ), removed AS (
        SELECT c.person_key,c.account_key,c.scope,p.entity_id,'observed_removed' AS change_kind,
            c.previous_observed_at_utc AS interval_start_utc,c.capture_completed_at_utc AS interval_end_utc,
            c.previous_import_id,c.import_id
        FROM snapshots c JOIN {s}.library_items p ON p.import_id=c.previous_import_id AND p.scope=c.scope
        WHERE NOT EXISTS (SELECT 1 FROM {s}.library_items l WHERE l.import_id=c.import_id
            AND l.scope=c.scope AND l.entity_id=p.entity_id)
        ) SELECT * FROM added UNION ALL SELECT * FROM removed""",
    "current_likes": """SELECT l.person_key,l.account_key,l.entity_id AS platform_track_id,
        l.position,l.observed_at_utc,l.import_id,
        json_extract_string(l.payload_json,'$.song.name') AS track,
        json_extract_string(coalesce(json_extract(l.payload_json,'$.song.ar'),
            json_extract(l.payload_json,'$.song.artists')),'$[*].name') AS artist_names,
        json_extract_string(coalesce(json_extract(l.payload_json,'$.song.ar'),
            json_extract(l.payload_json,'$.song.artists')),'$[*].id') AS artist_ids,
        coalesce(json_extract_string(l.payload_json,'$.song.al.name'),
            json_extract_string(l.payload_json,'$.song.album.name')) AS album,
        coalesce(json_extract_string(l.payload_json,'$.song.al.id'),
            json_extract_string(l.payload_json,'$.song.album.id')) AS album_id,
        try_cast(coalesce(json_extract_string(l.payload_json,'$.song.dt'),
            json_extract_string(l.payload_json,'$.song.duration')) AS BIGINT) AS track_duration_ms,
        try_cast(json_extract_string(l.payload_json,'$.song.pop') AS DOUBLE) AS reported_popularity,
        coalesce(json_extract_string(l.payload_json,'$.metadata_status'),'not_requested') AS metadata_status,
        (a.transport_status <> 'ok') AS snapshot_stale
        FROM {s}.library_items l
        JOIN {s}.latest_successful_source c ON c.import_id=l.import_id AND c.scope=l.scope
        JOIN {s}.latest_source_attempt a ON a.person_key=c.person_key AND a.source=c.source
            AND a.account_key=c.account_key AND a.scope=c.scope
        WHERE l.scope='likes'""",
    "current_playlists": """SELECT l.person_key,l.account_key,l.entity_id AS playlist_id,
        l.label AS name,l.position,l.observed_at_utc,l.import_id,
        CASE WHEN json_extract_string(l.payload_json,'$.creator.userId')=l.account_key THEN 'created'
            WHEN try_cast(json_extract_string(l.payload_json,'$.subscribed') AS BOOLEAN) THEN 'subscribed'
            ELSE 'unknown' END AS relationship,
        try_cast(json_extract_string(l.payload_json,'$.trackCount') AS BIGINT) AS reported_track_count,
        try_cast(json_extract_string(l.payload_json,'$.subscribedCount') AS BIGINT) AS reported_subscription_count,
        try_cast(json_extract_string(l.payload_json,'$.playCount') AS BIGINT) AS reported_play_count,
        'not_collected' AS membership_status,(a.transport_status <> 'ok') AS snapshot_stale
        FROM {s}.library_items l
        JOIN {s}.latest_successful_source c ON c.import_id=l.import_id AND c.scope=l.scope
        JOIN {s}.latest_source_attempt a ON a.person_key=c.person_key AND a.source=c.source
            AND a.account_key=c.account_key AND a.scope=c.scope
        WHERE l.scope='playlists'""",
    "current_rankings": """SELECT r.*,(a.transport_status <> 'ok') AS snapshot_stale
        FROM {s}.ranking_items r
        JOIN {s}.latest_successful_source c ON c.import_id=r.import_id AND c.scope=r.scope
        JOIN {s}.latest_source_attempt a ON a.person_key=c.person_key AND a.source=c.source
            AND a.account_key=c.account_key AND a.scope=c.scope""",
    "pano_listening_events": """SELECT o.*,try_cast(o.occurred_at_utc AS TIMESTAMPTZ) AS event_time_utc
        FROM {s}.observations o JOIN {s}.analysis_imports i ON i.id=o.import_id
        WHERE o.source='pano' AND o.event_kind='scrobble'""",
    "netease_recent_observations": """SELECT o.*,try_cast(o.occurred_at_utc AS TIMESTAMPTZ) AS event_time_utc
        FROM {s}.observations o JOIN {s}.analysis_imports i ON i.id=o.import_id
        WHERE o.source='netease_recent' AND o.event_kind='recent_observation'""",
}


REPORT_VIEWS = {
    "platform_report_history": """SELECT r.*,i.observed_at_utc AS capture_completed_at_utc
        FROM {s}.platform_reports r JOIN {s}.analysis_imports i ON i.id=r.import_id""",
    "latest_platform_report_attempt": """SELECT * FROM {s}.platform_report_history
        QUALIFY row_number() OVER (PARTITION BY person_key,account_key,report_kind
            ORDER BY observed_at_utc DESC,import_id DESC,id DESC)=1""",
    "current_platform_reports": """WITH successful AS (
        SELECT * FROM {s}.platform_report_history WHERE status IN ('ok','empty')
        QUALIFY row_number() OVER (PARTITION BY person_key,account_key,report_kind
            ORDER BY observed_at_utc DESC,import_id DESC,id DESC)=1)
        SELECT r.*,(a.status='failed') AS snapshot_stale,
            a.observed_at_utc AS latest_attempt_at_utc
        FROM successful r JOIN {s}.latest_platform_report_attempt a
          ON a.person_key=r.person_key AND a.account_key=r.account_key AND a.report_kind=r.report_kind""",
}


SONG_TAG_VIEWS = {
    "song_tag_history": """SELECT t.*,i.observed_at_utc AS capture_completed_at_utc
        FROM {s}.song_tags t JOIN {s}.analysis_imports i ON i.id=t.import_id""",
    "latest_song_tag_attempt": """SELECT * FROM {s}.song_tag_history
        QUALIFY row_number() OVER (PARTITION BY source,platform_track_id
            ORDER BY observed_at_utc DESC,import_id DESC,id DESC)=1""",
    "current_song_tags": """WITH successful AS (
        SELECT * FROM {s}.song_tag_history WHERE status IN ('ok','empty')
        QUALIFY row_number() OVER (PARTITION BY source,platform_track_id
            ORDER BY observed_at_utc DESC,import_id DESC,id DESC)=1)
        SELECT t.*,(a.status='failed') AS snapshot_stale,
            a.observed_at_utc AS latest_attempt_at_utc,a.status AS latest_attempt_status,
            json_extract_string(t.normalized_json,'$.fields.genre[*]') AS genre,
            json_extract_string(t.normalized_json,'$.fields.language[*]') AS language,
            try_cast(json_extract_string(t.normalized_json,'$.fields.bpm[*]') AS DOUBLE[]) AS bpm,
            json_extract_string(t.normalized_json,'$.fields.bpm_raw[*]') AS bpm_raw,
            json_extract_string(t.normalized_json,'$.fields.recommended_tags[*]') AS recommended_tags
        FROM successful t JOIN {s}.latest_song_tag_attempt a
          ON a.source=t.source AND a.platform_track_id=t.platform_track_id""",
}


class MusicSnapshot:
    def __init__(self, lake, generation=None, as_of=None, *, expected_manifest_sha256=None,
                 expected_database_id=None, expected_view_version=None):
        self.as_of = None
        if as_of is not None:
            value = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
            if value.utcoffset() is None:
                raise ValueError("as_of must include a timezone")
            self.as_of = value.astimezone(timezone.utc).isoformat(timespec="milliseconds")
        lake = Path(lake).resolve()
        pointer = None
        if generation is None:
            pointer = json.loads((lake/"current.json").read_text("utf-8"))
            generation = pointer["generation"]
        if not isinstance(generation, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", generation):
            raise ValueError("Invalid generation")
        self.directory = (lake/"versions"/generation).resolve()
        if not self.directory.is_relative_to(lake):
            raise ValueError("Snapshot path outside lake")
        content = (self.directory/"manifest.json").read_bytes()
        self.manifest_sha256 = hashlib.sha256(content).hexdigest()
        if expected_manifest_sha256 is not None and self.manifest_sha256 != expected_manifest_sha256:
            raise ValueError("Manifest checksum differs from pinned analysis input")
        if pointer and (pointer["manifest_path"] != f"versions/{generation}/manifest.json" or
                        hashlib.sha256(content).hexdigest() != pointer["manifest_sha256"]):
            raise ValueError("Manifest checksum mismatch")
        self.manifest = json.loads(content)
        self.database_id = self.manifest.get("database_id")
        if not isinstance(self.database_id, str) or not self.database_id:
            raise ValueError("Missing database identity")
        if expected_database_id is not None and self.database_id != expected_database_id:
            raise ValueError("Database identity changed; consume a complete new library")
        if expected_view_version is not None and expected_view_version != ANALYSIS_VIEW_VERSION:
            raise ValueError("Analysis view version changed")
        if self.manifest.get("format") != "collector-music-lake" or self.manifest.get("version") != 1 or self.manifest.get("generation") != generation:
            raise ValueError("Unsupported music snapshot")
        contract = self.manifest.get("schema_version")
        if contract not in DATASETS_BY_VERSION:
            raise ValueError("Unsupported data contract")
        if self.manifest.get("metric_version") != "1.0.0":
            raise ValueError("Unsupported metric contract")
        if set(self.manifest["datasets"]) != DATASETS_BY_VERSION[contract]:
            raise ValueError("Incomplete music snapshot")
        self.generation = generation
        self.analysis_view_version = ANALYSIS_VIEW_VERSION
        self.paths = {}
        for name, item in self.manifest["datasets"].items():
            if not re.fullmatch(r"[a-z][a-z0-9_]*", name) or item["path"] != name + ".parquet":
                raise ValueError("Unexpected dataset path")
            path = (self.directory/item["path"]).resolve()
            if not path.is_relative_to(self.directory):
                raise ValueError("Dataset path outside snapshot")
            if hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
                raise ValueError(f"Corrupt dataset: {name}")
            self.paths[name] = path

    def input_receipt(self):
        """Persist alongside consumer SQL and explicit account/device/person mappings.

        This is input metadata, never proof of continuous listening coverage.
        """
        return {
            "source_project": "collector-music", "database_id": self.database_id,
            "generation": self.generation, "manifest_sha256": self.manifest_sha256,
            "schema_version": self.manifest["schema_version"],
            "metric_version": self.manifest["metric_version"],
            "analysis_view_version": self.analysis_view_version, "as_of": self.as_of,
            "reader_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "coverage": "observed_only", "precise_netease_ordering": "unverified",
        }

    def attach(self, connection, schema="music"):
        if not re.fullmatch(r"[a-z][a-z0-9_]*", schema):
            raise ValueError("Use a simple lowercase schema name")
        # A new schema prevents silently replacing another source or snapshot.
        connection.execute("BEGIN TRANSACTION")
        try:
            connection.execute(f'CREATE SCHEMA "{schema}"')
            for name, path in self.paths.items():
                item = self.manifest["datasets"][name]
                if hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
                    raise ValueError(f"Corrupt dataset: {name}")
                relation = connection.read_parquet(str(path))
                actual = [{"name": c, "type": str(t)} for c,t in zip(relation.columns, relation.types)]
                if actual != item["schema"] or relation.count("*").fetchone()[0] != item["rows"]:
                    raise ValueError(f"Dataset schema or count mismatch: {name}")
                connection.execute(f'CREATE TABLE "{schema}"."{name}" AS SELECT * FROM read_parquet(?)', [str(path)])
            for name, sql in ANALYSIS_VIEWS.items():
                cutoff = "TRUE" if self.as_of is None else "observed_at_utc <= '" + self.as_of + "'"
                connection.execute(f'CREATE VIEW "{schema}"."{name}" AS ' + sql.format(s='"' + schema + '"', cutoff=cutoff))
            if "platform_reports" in self.paths:
                for name, sql in REPORT_VIEWS.items():
                    connection.execute(f'CREATE VIEW "{schema}"."{name}" AS ' + sql.format(s='"' + schema + '"'))
            if "song_tags" in self.paths:
                for name, sql in SONG_TAG_VIEWS.items():
                    connection.execute(f'CREATE VIEW "{schema}"."{name}" AS ' + sql.format(s='"' + schema + '"'))
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        return self
