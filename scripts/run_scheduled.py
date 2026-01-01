"""Windowless Task Scheduler entry; credentials and song contents never go to logs."""
from pathlib import Path
import sys

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from collector_music.automation import run_tick
from collector_music.common import MusicError, WorkspaceBusy, atomic_write, json_bytes, now
from collector_music.store import Workspace


def main():
    try:
        result = run_tick(Workspace(PROJECT))
        return 2 if result["status"] == "warning" else 0
    except WorkspaceBusy:
        # The active job owns its receipt. A concurrent tick must not overwrite
        # it with a failure merely because the local mutex is doing its job.
        return 0
    except Exception as error:
        message = str(error) if isinstance(error, MusicError) else type(error).__name__
        atomic_write(PROJECT / ".local/automation/last-run.json",
                     json_bytes(dict(status="failed", checked_at_utc=now(), error=message)))
        return 1


if __name__ == "__main__":
    sys.exit(main())
