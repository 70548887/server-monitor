"""整个真实应用在临时库/端口中的升级与回滚，不替换迁移或健康检查实现。"""
import asyncio
import json
import os
from pathlib import Path
import shutil
import socket
import uuid

import httpx
import pytest
from sqlalchemy import select

from test_mysql_integration import mysql_store
from app.models.node import NodeModel
from app.services.config import get_config
from app.services.update_runtime import atomic_json, file_hashes, package_version
from scripts.run_server import ServerManager

pytestmark = pytest.mark.skipif(os.getenv("RUN_MYSQL_INTEGRATION") != "1", reason="显式启用完整应用升级测试")


async def test_full_application_upgrade_migration_and_rollback(mysql_store, tmp_path, monkeypatch):
    source = Path(__file__).resolve().parents[1]
    root = tmp_path / "installed"
    root.mkdir()
    for name in file_hashes(source):
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / name, target)
    baseline_version = package_version(root)
    (root / "configs").mkdir()
    # 不使用业务配置：调度只读取这个临时库，通知关闭，节点禁用。
    app_yaml = ("app:\n  name: upgrade-integration\ndatabase: {}\n"
                "scheduler:\n  interval_seconds: 3600\nalerting:\n  enabled: false\n"
                "retention:\n  enabled: false\nlogging:\n  file:\n    enabled: false\n")
    (root / "configs/app.yaml").write_text(app_yaml, encoding="utf-8")
    (root / "configs/nodes.yaml").write_text("nodes: []\n", encoding="utf-8")
    (root / ".env").write_text("UPGRADE_CONFIG_MARKER=preserve-me\n", encoding="utf-8")
    engine, sessions, _ = mysql_store
    db = get_config().database
    for key in list(os.environ):
        if key.startswith(("MYSQL_", "REDIS_", "ADMIN_", "DEFAULT_", "MONITOR_")) or key in ("ALLOWED_HOSTS", "CORS_ALLOWED_ORIGINS", "COOKIE_SECURE"):
            monkeypatch.delenv(key)
    for key, value in {"MYSQL_HOST": db.mysql_host, "MYSQL_PORT": str(db.mysql_port),
                       "MYSQL_USER": db.mysql_user, "MYSQL_PASSWORD": db.mysql_password,
                       "MYSQL_DATABASE": engine.url.database, "REDIS_HOST": "",
                       "ADMIN_USERNAME": "upgrade-test", "ADMIN_PASSWORD": uuid.uuid4().hex,
                       "COOKIE_SECURE": "false", "ALLOWED_HOSTS": "127.0.0.1", "LOG_LEVEL": "ERROR"}.items():
        monkeypatch.setenv(key, value)
    async with sessions() as session:
        session.add(NodeModel(name="preserved-node", url="http://fixture.invalid", enabled=False))
        await session.commit()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    manager = ServerManager(root, port=port)
    manager.start_child()
    try:
        assert await asyncio.to_thread(manager.healthy, baseline_version, 15)
        old_pid = manager.child.pid
        ident = uuid.uuid4().hex
        stage = manager.store.home / "staging" / ident
        for name in file_hashes(root):
            target = stage / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / name, target)
        # 测试版只改变版本号，以同一套真实程序验证全部交接阶段。
        (stage / "app/__init__.py").write_text('__version__ = "99.0.0"\n', encoding="utf-8")
        atomic_json(manager.store.home / "staged.json", {"id": ident, "version": "99.0.0", "files": file_hashes(stage)})
        await asyncio.to_thread(manager.execute, {"action": "apply", "actor": "project-integration"})
        assert manager.store.read()["state"] == "complete", manager.store.read()["message"]
        assert manager.child.pid != old_pid
        assert await asyncio.to_thread(manager.healthy, "99.0.0", 5)
        backup = manager.store.read()["last_backup"]
        await asyncio.to_thread(manager.execute, {"action": "rollback", "actor": "project-integration", "backup": backup})
        assert manager.store.read()["state"] == "complete", manager.store.read()["message"]
        assert await asyncio.to_thread(manager.healthy, baseline_version, 5)
        assert (root / "configs/app.yaml").read_text(encoding="utf-8") == app_yaml
        assert (root / ".env").read_text(encoding="utf-8") == "UPGRADE_CONFIG_MARKER=preserve-me\n"
        async with sessions() as session:
            rows = (await session.execute(select(NodeModel))).scalars().all()
            assert len(rows) == 1 and rows[0].name == "preserved-node"
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client:
            assert (await client.get("/login")).status_code == 200
    finally:
        await asyncio.to_thread(manager.stop_child)
