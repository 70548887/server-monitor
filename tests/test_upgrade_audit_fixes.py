"""2026-10-13 审核缺陷：新进程恢复、重启环境、真实 Redis 和配置竞态。"""
import asyncio
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import uuid

import httpx
import pytest

from app.middleware import security
from app.repositories.redis_repository import RedisRepository
from app.services.update_runtime import RuntimeStore, atomic_json, file_hashes, read_json
from scripts.run_server import ServerManager
from test_node_reload import reload_state, row

SOURCE = Path(__file__).resolve().parents[1]


def installed_copy(root):
    root.mkdir()
    for name in file_hashes(SOURCE):
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(SOURCE / name, target)
    return root


def interrupted_install(root):
    root = installed_copy(root)
    store = RuntimeStore(root)
    original = file_hashes(root)
    backup = store.backup(sys.executable)
    # 合法新版本的 __init__ 已替换，但它依赖的新文件还没复制，等价于中途断电。
    (root / "app/__init__.py").write_text('__version__ = "99.0.0"\nfrom .new_build import BUILD\n')
    atomic_json(store.home / "transaction.json", {"backup":backup["name"], "changed_files":list(original) + ["app/new_build.py"]})
    return store, original


def launch_help(root, port=8080):
    return subprocess.run([sys.executable, "scripts/run_server.py", "--port", str(port), "--help"],
                          cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)


def test_fresh_launcher_recovers_before_importing_broken_package(tmp_path):
    store, original = interrupted_install(tmp_path / "installed")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    result = launch_help(store.root, port)
    assert result.returncode == 0, result.stderr
    assert file_hashes(store.root) == original
    assert not (store.home / "transaction.json").exists()
    assert store.read()["rolled_back"]


def test_fresh_recovery_refuses_existing_listener(tmp_path):
    store, _ = interrupted_install(tmp_path / "installed")
    before = file_hashes(store.root)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        result = launch_help(store.root, listener.getsockname()[1])
    assert result.returncode != 0
    assert (store.home / "transaction.json").exists()
    assert file_hashes(store.root) == before


def test_fresh_recovery_refuses_corrupt_backup(tmp_path):
    store, _ = interrupted_install(tmp_path / "installed")
    before = file_hashes(store.root)
    journal = read_json(store.home / "transaction.json")
    (store.home / "backups" / journal["backup"] / "code.zip").write_bytes(b"corrupt")
    result = launch_help(store.root)
    assert result.returncode != 0
    assert (store.home / "transaction.json").exists()
    assert file_hashes(store.root) == before


def test_pending_recovery_blocks_new_operations(tmp_path):
    from app.services.update_runtime import UpdateBusy
    from test_updates import project
    root = project(tmp_path / "installed")
    manager = ServerManager(root)
    journal = {"backup":"pending", "changed_files":[]}
    atomic_json(manager.store.home / "transaction.json", journal)
    manager.store.status("error", "recovery incomplete")
    with pytest.raises(UpdateBusy):
        manager.store.ensure_idle()
    manager.execute({"action":"apply"})
    assert read_json(manager.store.home / "transaction.json") == journal
    assert "事务" in manager.store.read()["message"]


def test_real_launcher_restart_reloads_dotenv(tmp_path):
    root = installed_copy(tmp_path / "installed")
    (root / "app/main.py").write_text('''import os
from fastapi import FastAPI
from app import __version__
from app.services.config import load_env_file
load_env_file()
app = FastAPI()
@app.get('/healthz')
def health():
    return {'status':'healthy', 'version':__version__, 'instance_id':os.getenv('MONITOR_INSTANCE'),
            'log_level':os.getenv('LOG_LEVEL'), 'pid':os.getpid(), 'optional':os.getenv('AUDIT_OPTIONAL'),
            'explicit':os.getenv('AUDIT_EXPLICIT')}
@app.get('/healthz/ready')
def ready(): return {'ready':True}
''')
    (root / ".env").write_text('LOG_LEVEL=WARNING\nAUDIT_OPTIONAL=present\nAUDIT_EXPLICIT=file\n')
    env = {k:v for k,v in os.environ.items() if k not in {"LOG_LEVEL", "ALLOWED_HOSTS", "AUDIT_OPTIONAL"}}
    env["AUDIT_EXPLICIT"] = "external"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with (root / "launcher.log").open("wb") as log:
        process = subprocess.Popen([sys.executable, "scripts/run_server.py", "--port", str(port)], cwd=root,
                                   env=env, stdout=log, stderr=log)
        store = RuntimeStore(root)
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=1, trust_env=False) as client:
                def wait_for(predicate):
                    end = time.monotonic() + 15
                    while time.monotonic() < end:
                        assert process.poll() is None, (root / "launcher.log").read_text(errors="replace")
                        try:
                            data = client.get("/healthz").json()
                            if predicate(data):
                                return data
                        except (httpx.HTTPError, ValueError):
                            pass
                        time.sleep(.1)
                    pytest.fail("Launcher/restart timed out")
                before = wait_for(lambda data: True)
                assert before["log_level"] == "WARNING"
                (root / ".env").write_text('LOG_LEVEL=DEBUG\nAUDIT_EXPLICIT=changed-file\n')
                session = read_json(store.home / "manager.json")["session"]
                atomic_json(store.home / "command.json", {"action":"restart", "session":session, "actor":"test"})
                after = wait_for(lambda data: data["pid"] != before["pid"] and store.read().get("state") == "complete")
                assert after["log_level"] == "DEBUG"
                assert after["optional"] is None
                assert after["explicit"] == "external"
        finally:
            # The manager owns a child on Windows; stop only this verified process tree.
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
            else:
                process.terminate()
            process.wait(timeout=10)


async def test_actual_redis_outage_enforces_memory_limits(monkeypatch):
    from redis import asyncio as aioredis
    from redis.backoff import NoBackoff
    from redis.retry import Retry
    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        repo = RedisRepository()
        repo._client = aioredis.Redis(host="127.0.0.1", port=held.getsockname()[1],
                                     socket_timeout=.1, socket_connect_timeout=.1, retry=Retry(NoBackoff(), 0))
        monkeypatch.setattr(security, "get_redis_repository", lambda: repo)
        store = security.RateLimitStore()
        try:
            limited = [await store.is_rate_limited("test:login-submit", 10, 60) for _ in range(12)]
            assert limited == [False] * 10 + [True] * 2
            assert not store._redis_available
            assert [await store.record_login_failure("127.0.0.99") for _ in range(5)] == [False]*4 + [True]
            assert (await store.is_ip_locked("127.0.0.99"))[0]
        finally:
            await repo._client.aclose()


@pytest.mark.skipif(os.getenv("RUN_REDIS_INTEGRATION") != "1", reason="Explicit isolated Redis keys")
async def test_successful_login_clears_existing_redis_keys(monkeypatch):
    repo = RedisRepository()
    client = await repo._get_client()
    store = security.RateLimitStore()
    store._prefix = "audit-regression-" + uuid.uuid4().hex + ":"
    ip = "127.0.0.99"
    monkeypatch.setattr(security, "get_redis_repository", lambda: repo)
    keys = (f"{repo._prefix}node:{store._prefix}login_fail:{ip}:errors",
            f"{repo._prefix}alert:suppress:{store._prefix}login_lock:{ip}")
    try:
        for _ in range(4):
            assert not await store.record_login_failure(ip)
        assert int(await client.get(keys[0])) == 4
        await store.clear_login_failures(ip)
        assert not await client.exists(*keys)
        assert not await store.record_login_failure(ip)
        assert not (await store.is_ip_locked(ip))[0]
        for _ in range(4):
            await store.record_login_failure(ip)
        locked, remaining = await store.is_ip_locked(ip)
        assert locked and 890 <= remaining <= 900
        await store.clear_login_failures(ip)
        assert not await client.exists(*keys)
    finally:
        await client.delete(*keys)
        await client.aclose()


async def test_memory_failures_survive_request_cleanup_and_expire(monkeypatch):
    store = security.RateLimitStore()
    store._redis_available = False
    now = [10000.0]
    monkeypatch.setattr(security.time, "time", lambda: now[0])
    store._last_cleanup = now[0]
    for _ in range(4):
        await store.record_login_failure("test")
    now[0] += 61
    await store.is_rate_limited("test:login-submit")
    assert await store.record_login_failure("test")
    now[0] += 901
    assert not (await store.is_ip_locked("test"))[0]
    assert not await store.record_login_failure("test")


@pytest.mark.parametrize("change", ["url", "expect", "disabled", "deleted", "roundtrip"])
async def test_late_probe_rejected_after_config_change(reload_state, change):
    from app.routes import nodes
    from app.services.heartbeat import HeartbeatService
    import httpx
    cfg, _, repo = reload_state
    repo.get_all_nodes.return_value = [row()]
    await nodes.reload_node_config()
    service = HeartbeatService()
    entered, released = asyncio.Event(), asyncio.Event()
    async def endpoint(request):
        entered.set()
        await released.wait()
        return httpx.Response(503)
    service._client = httpx.AsyncClient(transport=httpx.MockTransport(endpoint))
    probe = asyncio.create_task(service.check_node(cfg.nodes[0]))
    try:
        await entered.wait()
        changes = {"url":{"url":"http://replacement.invalid"}, "expect":{"expect":{"status":200}},
                   "disabled":{"enabled":False}, "roundtrip":{"url":"http://replacement.invalid"}}
        repo.get_all_nodes.return_value = [] if change == "deleted" else [row(**changes[change])]
        await nodes.reload_node_config()
        if change == "roundtrip":
            repo.get_all_nodes.return_value = [row()]
            await nodes.reload_node_config()
        released.set()
        result = await probe
        before = {k:v.model_dump() for k,v in nodes._node_status_cache.items()}
        assert not nodes.update_node_status_from_result(result.node_name, result)
        assert {k:v.model_dump() for k,v in nodes._node_status_cache.items()} == before
    finally:
        released.set()
        await probe
        await service.close()


async def test_unchanged_probe_survives_metadata_reload(reload_state):
    from app.routes import nodes
    from app.services.heartbeat import HeartbeatService
    cfg, _, repo = reload_state
    await nodes.reload_node_config()
    service = HeartbeatService()
    service._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(204, text="ready")))
    try:
        result = await service.check_node(cfg.nodes[0])
        repo.get_all_nodes.return_value = [row(region="changed-metadata")]
        await nodes.reload_node_config()
        assert nodes.update_node_status_from_result(result.node_name, result)
        assert nodes._node_status_cache[result.node_name].last_status_code == 204
    finally:
        await service.close()
