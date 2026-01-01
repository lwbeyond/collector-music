"""Bound automatic copies; archived source observations remain authoritative."""
import json
from pathlib import Path

from ._retention import checked, prune


def prune_publications(workspace, *, grace_seconds=3600, apply=True):
    lake = workspace.data / "lake"
    pointer = json.loads(checked(workspace.home, lake / "current.json").read_bytes())
    from .lake import resolve_version
    current = resolve_version(lake)
    checked(workspace.home, current)
    return prune(workspace.home, lake / "versions", {pointer["generation"]},
                 r"[0-9]{8}T[0-9]{6}-[a-f0-9]{12}",
                 grace_seconds=grace_seconds, apply=apply)


def prune_backups(workspace, successful, *, apply=True):
    directory = workspace.data / "backups/automatic"
    successful = checked(workspace.home, Path(successful))
    if successful.parent != directory or not successful.is_file():
        raise ValueError("Only a verified automatic backup may replace earlier copies")
    return prune(workspace.home, directory, {successful.name},
                 r"music-[0-9]{8}T[0-9]{12}Z\.sqlite", grace_seconds=0, apply=apply)
