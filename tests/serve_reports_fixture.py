"""Small disposable publication for platform-report browser checks."""
from __future__ import annotations

import json
import tempfile
import threading
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
    (ROOT / '.tmp').mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=ROOT / '.tmp') as temporary:
        workspace = Workspace(Path(temporary) / 'workspace').initialize()
        snapshot = Path(temporary) / 'snapshot.json'
        current = datetime.now(ZoneInfo('Asia/Shanghai'))
        document = netease_document()
        document['collected_at_utc'] = current.isoformat()
        for capture in document['scopes'].values():
            capture['observed_at_utc'] = current.isoformat()
        document['scopes']['recent']['body']['data']['list'][0]['playTime'] = int(current.timestamp() * 1000)
        snapshot.write_bytes(json_bytes(document))
        import_netease(workspace, snapshot)
        recent = current - timedelta(days=1)
        log = Path(temporary) / 'pano.jsonl'
        log.write_bytes(line(dict(pano_row(), timeMs=int(recent.timestamp() * 1000), timeHuman=recent.strftime('%Y-%m-%d %H:%M'))))
        import_pano(workspace, log, device='phone', stream='fixture')
        publish(workspace)
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler_for(DashboardStore(workspace.home)))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        print(json.dumps({'url': f'http://127.0.0.1:{server.server_port}'}), flush=True)
        try:
            input()
        except EOFError:
            pass
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    main()
