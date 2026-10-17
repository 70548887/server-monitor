"""一键请求到独立管理器真实应用/回滚的隔离联调，不访问外网或业务数据库。"""
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import socket
import time

import httpx
import pytest

from app.services.update_runtime import atomic_json, file_hashes, package_version
from app.services.updates import API_URL, UpdateService
from scripts.run_server import ServerManager
from test_update_manager import MINIMAL_APP
from test_update_pipeline import host_runtime
from test_updates import bundle, metadata, project


HEALTH_APP = MINIMAL_APP.replace(
    '"instance_id":os.getenv("MONITOR_INSTANCE")',
    '"instance_id":os.getenv("MONITOR_INSTANCE"),"pid":os.getpid()',
)
HEALTH_FAILURE_APP = HEALTH_APP + '''
@app.on_event("startup")
async def reject_test_startup():
    raise RuntimeError("fixture new version cannot start")
'''


async def wait_for(predicate, timeout=20):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(.05)
    raise AssertionError("isolated update pipeline did not reach the expected state")


def record_evidence(name, data, manager=None):
    """显式启用才写入本机验收记录，文件不含管理器会话或代理凭据。"""
    directory = os.getenv("UPDATE_PIPELINE_EVIDENCE_DIR")
    if not directory:
        return
    folder = Path(directory)
    folder.mkdir(parents=True, exist_ok=True)
    atomic_json(folder / (name + ".json"), data)
    if manager:
        worker = manager.store.home / "worker.log"
        if worker.is_file():
            (folder / (name + "-worker.log")).write_text(worker.read_text(encoding="utf-8"), encoding="utf-8")


@asynccontextmanager
async def live_pipeline(tmp_path, monkeypatch, *, failure=None):
    # 脚本执行的 cwd、配置、.updates 及 uvicorn 服务均局限于这个临时项目。
    root = project(tmp_path / "pipeline-install")
    # zip 成员固定为 LF，避免 Windows 换行转换误触发真实依赖安装。
    (root / "requirements.lock").write_bytes(b"fastapi==0.139.0\n")
    (root / "app/main.py").write_text(HEALTH_APP, encoding="utf-8")
    (root / "configs").mkdir()
    config = "application: isolated-pipeline\nnotifications: disabled\nnodes: []\n"
    (root / "configs/app.yaml").write_text(config, encoding="utf-8")
    (root / ".env").write_text("PIPELINE_CONFIG_MARKER=preserved\n", encoding="utf-8")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    manager = ServerManager(root, port=port)
    host_runtime(monkeypatch)
    # 服务任务在 pytest 进程中，需与真实管理器的 child 使用同一会话。
    monkeypatch.setenv("MONITOR_UPDATE_SESSION", manager.session)
    payload = bundle(extras={
        "app/main.py": HEALTH_FAILURE_APP if failure == "health" else HEALTH_APP,
        "scripts/bootstrap_database.py": 'raise RuntimeError("fixture schema precondition failed")\n' if failure == "schema" else '# isolated migration: no database or network\n',
    })
    release, checksum = metadata(payload)
    seen = []
    def transport(request):
        seen.append(str(request.url))
        if str(request.url) == API_URL:
            return httpx.Response(200, json=release)
        if request.url.path.endswith("SHA256SUMS.txt"):
            return httpx.Response(200, content=checksum)
        return httpx.Response(200, content=payload, headers={"content-length": str(len(payload))})
    service = UpdateService(root, "1.0.0", httpx.MockTransport(transport))
    transitions = []
    # 仅记录状态写入，不替换 manager 的任何执行、校验、备份或进程实现。
    for store in (service.store, manager.store):
        write = store.status
        def trace(state, message, progress=0, _write=write, **extra):
            transitions.append(state)
            return _write(state, message, progress, **extra)
        monkeypatch.setattr(store, "status", trace)
    runner = asyncio.create_task(asyncio.to_thread(manager.run))
    try:
        await wait_for(lambda: manager.child is not None)
        assert await asyncio.to_thread(manager.healthy, "1.0.0", 12)
        assert service.capability()["can_apply"]
        yield service, manager, seen, transitions, config
    finally:
        if service.task and not service.task.done():
            try:
                await asyncio.wait_for(service.task, 10)
            except asyncio.TimeoutError:
                service.task.cancel()
        manager.stopping = True
        try:
            assert await asyncio.wait_for(runner, 20) == 0
        finally:
            await asyncio.to_thread(manager.stop_child)


async def request_upgrade(service, monkeypatch):
    from app.routes import updates as routes
    from test_routes import make_authed_client
    monkeypatch.setattr(routes, "get_update_service", lambda: service)
    # HTTP 客户端只发一次请求；关闭以后后台任务和 manager.run() 独立推进。
    async with make_authed_client(monkeypatch) as client:
        response = await client.post("/api/system/update/upgrade", headers={"X-Update-Request": "1"})
        assert response.status_code == 202, response.text
        submitted = response.json()
    assert submitted["workflow"]["mode"] == "upgrade"
    await service.task
    return submitted["workflow"]


async def test_one_request_real_manager_applies_restarts_and_preserves_configuration(tmp_path, monkeypatch):
    async with live_pipeline(tmp_path, monkeypatch) as (service, manager, seen, transitions, config):
        old_pid = manager.child.pid
        original = file_hashes(manager.root)
        workflow = await request_upgrade(service, monkeypatch)
        completed = await wait_for(lambda: (state if (state := manager.store.read()).get("state") in {"complete", "error"} else None))
        assert completed["state"] == "complete", completed
        assert completed["workflow"] == workflow and not completed["rolled_back"]
        assert "queued" in transitions and "applying" in transitions and "restarting" in transitions
        assert transitions.index("checking") < transitions.index("queued") < transitions.index("applying") < transitions.index("complete")
        assert package_version(manager.root) == "1.1.0"
        assert manager.child.pid != old_pid and manager.child.poll() is None
        assert await asyncio.to_thread(manager.healthy, "1.1.0", 5)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{manager.port}", trust_env=False) as client:
            health = (await client.get("/healthz")).json()
            assert health["version"] == "1.1.0" and health["pid"] == manager.child.pid
            assert (await client.get("/healthz/ready")).status_code == 200
        backup = manager.store.backup_manifest(completed["last_backup"])
        assert backup["version"] == "1.0.0" and backup["files"] == original
        assert (manager.root / "configs/app.yaml").read_text(encoding="utf-8") == config
        assert (manager.root / ".env").read_text(encoding="utf-8") == "PIPELINE_CONFIG_MARKER=preserved\n"
        assert len(seen) == 4
        assert not (manager.store.home / "command.json").exists()
        assert not (manager.store.home / "transaction.json").exists()
        assert not (manager.store.home / "staged.json").exists()
        refreshed = UpdateService(manager.root, "1.1.0", service.transport).status()
        assert refreshed["state"] == "complete" and refreshed["current_version"] == "1.1.0"
        assert not refreshed["release"]["has_update"] and refreshed["workflow"] == workflow
        record_evidence("manager-apply-success", {
            "state": completed["state"], "transitions": transitions, "requests": seen,
            "old_pid": old_pid, "new_pid": manager.child.pid, "version": health["version"],
            "health": "healthy", "ready_http_status": 200, "configuration_preserved": True,
            "backup_version": backup["version"], "workflow_survived_restart": refreshed["workflow"] == workflow,
            "command_consumed": True, "transaction_removed": True,
        }, manager)


@pytest.mark.parametrize("failure", ["schema", "health"])
async def test_one_request_real_manager_rolls_back_and_recovers_old_health(tmp_path, monkeypatch, failure):
    async with live_pipeline(tmp_path, monkeypatch, failure=failure) as (service, manager, seen, transitions, config):
        old_pid = manager.child.pid
        original = file_hashes(manager.root)
        workflow = await request_upgrade(service, monkeypatch)
        failed = await wait_for(lambda: (state if (state := manager.store.read()).get("state") in {"complete", "error"} else None), 25)
        assert failed["state"] == "error" and failed["rolled_back"], failed
        assert failed["workflow"] == workflow and "rolling_back" in transitions
        assert package_version(manager.root) == "1.0.0" and file_hashes(manager.root) == original
        assert manager.child.pid != old_pid and manager.child.poll() is None
        assert await asyncio.to_thread(manager.healthy, "1.0.0", 5)
        assert (manager.root / "configs/app.yaml").read_text(encoding="utf-8") == config
        assert (manager.root / ".env").read_text(encoding="utf-8") == "PIPELINE_CONFIG_MARKER=preserved\n"
        assert not (manager.store.home / "command.json").exists()
        assert not (manager.store.home / "transaction.json").exists()
        assert len(seen) == 4
        assert "数据库结构" in failed["error"] if failure == "schema" else "新版本健康检查未通过" in failed["error"]
        record_evidence("manager-rollback-" + failure, {
            "state": failed["state"], "error": failed["error"], "rolled_back": failed["rolled_back"],
            "transitions": transitions, "requests": seen, "old_pid": old_pid, "restored_pid": manager.child.pid,
            "restored_version": package_version(manager.root), "health": "healthy", "configuration_preserved": True,
            "files_restored_exactly": file_hashes(manager.root) == original,
            "workflow_preserved": failed["workflow"] == workflow, "command_consumed": True, "transaction_removed": True,
        }, manager)
