"""Disposable publication for browser analytics tests; no personal data required."""
from __future__ import annotations

import json
import tempfile
import threading
import sys
from time import perf_counter
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

from collector_music.common import json_bytes
from collector_music.dashboard import DashboardStore, handler_for
from collector_music.lake import publish
from collector_music.netease import import_netease
from collector_music.pano import import_pano
from collector_music.store import Workspace
from test_pipeline import ROOT, line, netease_document, pano_row


def main():
    started = perf_counter()
    (ROOT / ".tmp").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as temporary:
        root = Path(temporary)
        workspace = Workspace(root / "workspace").initialize()
        today = datetime.now(ZoneInfo("Asia/Shanghai")).replace(hour=0, minute=30, second=0, microsecond=0)

        def event(days, **values):
            return dict(pano_row(), timeMs=int((today - timedelta(days=days)).timestamp() * 1000),
                **values)

        payload = line(event(40, track="Old song", artist="Old only"))
        payload += line(event(8, track="Previous song", artist="Previous artist")) * 2
        payload += line(event(2, track="Bulk song", artist="Bulk artist")) * 5001
        payload += line(event(1, track="Bulk song", artist="Bulk artist"))
        payload += line(event(1, track="Second song", artist="Bulk artist"))
        payload += line(event(0, event="love"))
        log = root / "pano.jsonl"
        log.write_bytes(payload)
        import_pano(workspace, log, device="phone", stream="fixture")
        print(f"Analytics fixture imported ({perf_counter() - started:.1f}s)", file=sys.stderr, flush=True)
        document = netease_document()
        document["scopes"]["recent"]["body"]["data"]["list"][0]["playTime"] = event(1)["timeMs"]
        snapshot = root / "snapshot.json"
        snapshot.write_bytes(json_bytes(document))
        import_netease(workspace, snapshot)
        # Exercise the production native bulk publication path.
        publish(workspace)
        print(f"Analytics fixture published ({perf_counter() - started:.1f}s)", file=sys.stderr, flush=True)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(DashboardStore(workspace.home)))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        print(json.dumps(dict(url=f"http://127.0.0.1:{server.server_port}", today=today.date().isoformat())), flush=True)
        try:
            input()  # Parent closes stdin on completion, allowing temporary data cleanup.
        except EOFError:
            pass
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    main()
