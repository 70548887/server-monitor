"""Real local HTTP + temporary MySQL: late probe after changing a URL."""
import asyncio
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import pytest
from test_mysql_integration import mysql_store
from app.routes import nodes
from app.models.node import NodeModel
from app.services.config import AppConfig
from app.services.heartbeat import HeartbeatService

pytestmark = pytest.mark.skipif(os.getenv("RUN_MYSQL_INTEGRATION") != "1", reason="Requires isolated test database")


async def test_real_late_old_url_result_is_discarded(mysql_store, monkeypatch):
    engine, sessions, repo = mysql_store
    started, release = threading.Event(), threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            started.set()
            release.wait(8)
            self.send_response(503 if self.path == "/old" else 200)
            self.end_headers()
            self.wfile.write(b"local audit endpoint")

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    cfg = AppConfig(nodes=[])
    monkeypatch.setattr(nodes, "get_config", lambda: cfg)
    monkeypatch.setattr(nodes, "_get_db_repository", lambda: repo)
    monkeypatch.setattr(nodes, "_node_status_cache", {})
    old_url = f"http://127.0.0.1:{server.server_port}/old"
    new_url = f"http://127.0.0.1:{server.server_port}/new"
    async with sessions() as s:
        s.add(NodeModel(name="audit-race", url=old_url, enabled=True, timeout_ms=10000))
        await s.commit()
    service = HeartbeatService()
    task = None
    try:
        await nodes.reload_node_config()
        task = asyncio.create_task(service.check_node(cfg.nodes[0]))
        assert await asyncio.to_thread(started.wait, 3)
        assert await repo.update_node("audit-race", {"url": new_url})
        await nodes.reload_node_config()
        clean = nodes._node_status_cache["audit-race"].model_dump(mode="json")
        assert clean["url"] == new_url and clean["last_check_at"] is None
        release.set()
        result = await task
        assert result.url == old_url and result.status_code == 503
        # Exact production callback used by app.main.on_heartbeat_results.
        assert not nodes.update_node_status_from_result(result.node_name, result)
        after = nodes._node_status_cache["audit-race"].model_dump(mode="json")
        print(json.dumps({"case":"late_old_url_result_after_reload", "before":clean,
                          "actual_probed_url":result.url, "after":after}, ensure_ascii=False))
        assert after["url"] == new_url and after["last_status_code"] is None
        assert after["consecutive_failures"] == 0
    finally:
        release.set()
        if task and not task.done():
            await task
        await service.close()
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join(timeout=3)
