from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
import json
import tempfile
import threading
from zoneinfo import ZoneInfo

from collector_music.dashboard import DashboardStore, handler_for
from collector_music.store import Workspace
from test_tag_dashboard import make_tag_fixture, ROOT


if __name__ == '__main__':
    (ROOT / '.tmp').mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=ROOT / '.tmp') as directory:
        workspace = Workspace(Path(directory)).initialize()
        make_tag_fixture(workspace, datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat())
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler_for(DashboardStore(workspace.home)))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        print(json.dumps(dict(url=f'http://127.0.0.1:{server.server_port}')), flush=True)
        try:
            input()
        except EOFError:
            pass
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
