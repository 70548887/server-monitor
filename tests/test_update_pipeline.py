"""一键升级在服务端串行执行，覆盖管理器门禁、代理路由、失败停止和页面断开。"""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time

import httpx
import pytest

from app.services import updates
from app.services.update_network import save_update_proxy
from app.services.update_runtime import UpdateBusy, UpdateError, atomic_json, file_hashes, package_version, read_json
from test_updates import API_URL, bundle, metadata, service


PROXY = "http://pipeline-user:pipeline-password@proxy.example.test:3128"
SESSION = "pipeline-manager-session"


def host_runtime(monkeypatch):
    # CI可能运行在容器中；这组测试明确构造原生宿主机部署。
    real_path = Path
    class HostMarker:
        def exists(self):
            return False
    monkeypatch.setattr(updates, "Path", lambda value: HostMarker() if str(value) == "/.dockerenv" else real_path(value))
    monkeypatch.delenv("MONITOR_DEPLOYMENT", raising=False)


def managed_service(tmp_path, monkeypatch, **kwargs):
    svc, seen = service(tmp_path, **kwargs)
    host_runtime(monkeypatch)
    monkeypatch.setenv("MONITOR_UPDATE_SESSION", SESSION)
    atomic_json(svc.store.home / "manager.json", {"session": SESSION, "heartbeat": time.time()})
    return svc, seen


async def complete(svc):
    await svc.start("upgrade", "pipeline-admin")
    await svc.task
    return svc.status()


async def test_upgrade_verifies_package_and_queues_one_apply_under_one_lock(tmp_path, monkeypatch):
    svc, seen = managed_service(tmp_path, monkeypatch)
    original_files = file_hashes(svc.store.root)
    transitions = []
    write_status = svc.store.status
    def track(state, message, progress=0, **extra):
        transitions.append(state)
        return write_status(state, message, progress, **extra)
    monkeypatch.setattr(svc.store, "status", track)
    started = await svc.start("upgrade", "pipeline-admin")
    workflow = started["workflow"]
    assert started["state"] == "checking" and workflow["mode"] == "upgrade"
    await svc.task
    status = svc.status()
    assert status["state"] == "queued"
    assert status["workflow"] == workflow
    assert transitions.index("checking") < transitions.index("available") < transitions.index("downloading") < transitions.index("ready") < transitions.index("queued")
    staged = read_json(svc.store.home / "staged.json")
    directory = svc.store.home / "staging" / staged["id"]
    assert package_version(directory) == "1.1.0"
    assert file_hashes(directory) == staged["files"]
    assert staged["sha256"] == hashlib.sha256(bundle()).hexdigest()
    command = read_json(svc.store.home / "command.json")
    assert command["action"] == "apply" and command["actor"] == "pipeline-admin"
    assert command["session"] == SESSION and command["workflow_id"] == workflow["id"]
    assert status["operation_id"] == command["id"]
    assert len(seen) == 4  # 检查、下载前重新确认Release、校验清单、升级包。
    assert file_hashes(svc.store.root) == original_files
    assert not (svc.store.home / "transaction.json").exists()
    history = [json.loads(line) for line in (svc.store.home / "history.jsonl").read_text(encoding="utf-8").splitlines()]
    assert sum(item["action"] == "apply" and item["result"] == "queued" for item in history) == 1
    with svc.store.lock():
        pass  # 完成排队后已把锁移交给管理器。


async def test_upgrade_continues_after_request_client_closes_and_status_refreshes(tmp_path, monkeypatch):
    from app.routes import updates as routes
    from test_routes import make_authed_client
    svc, seen = managed_service(tmp_path, monkeypatch)
    downloading, resume = asyncio.Event(), asyncio.Event()
    original_transport = svc.transport
    async def transport(request):
        if request.url.path.endswith(".zip"):
            downloading.set()
            await resume.wait()
        return await original_transport.handle_async_request(request)
    svc.transport = httpx.MockTransport(transport)
    monkeypatch.setattr(routes, "get_update_service", lambda: svc)
    async with make_authed_client(monkeypatch) as client:
        response = await client.post("/api/system/update/upgrade", headers={"X-Update-Request": "1"})
        assert response.status_code == 202
        workflow = response.json()["workflow"]
    await downloading.wait()
    try:
        # 模拟刷新面板，用新service读取同一状态与锁，而非依赖原浏览器的JS任务。
        refreshed = updates.UpdateService(svc.store.root, "1.0.0", svc.transport)
        snapshot = refreshed.status()
        assert snapshot["state"] == "downloading" and snapshot["workflow"] == workflow
        assert len(seen) == 3
    finally:
        resume.set()
        await svc.task
    assert svc.status()["state"] == "queued"
    assert read_json(svc.store.home / "command.json")["workflow_id"] == workflow["id"]


async def test_upgrade_all_requests_use_saved_proxy_and_hide_credentials(tmp_path, monkeypatch):
    svc, seen = managed_service(tmp_path, monkeypatch)
    save_update_proxy(svc.store.root, True, PROXY)
    clients = []
    original_client = httpx.AsyncClient
    def capture(**options):
        clients.append(options)
        return original_client(**options)
    monkeypatch.setattr(httpx, "AsyncClient", capture)
    result = await complete(svc)
    assert result["state"] == "queued" and len(seen) == len(clients) == 4
    assert all(item["proxy"] == PROXY and item["trust_env"] is False for item in clients)
    for path in (svc.store.home / "status.json", svc.store.home / "history.jsonl", svc.store.home / "command.json"):
        content = path.read_text(encoding="utf-8")
        assert "pipeline-user" not in content and "pipeline-password" not in content


async def test_no_update_stops_without_downloading_or_queueing(tmp_path, monkeypatch):
    svc, seen = managed_service(tmp_path, monkeypatch)
    payload = bundle("1.0.0")
    release, _ = metadata(payload, "1.0.0")
    svc.transport = httpx.MockTransport(lambda request: (seen.append(request), httpx.Response(200, json=release))[1])
    result = await complete(svc)
    assert result["state"] == "up_to_date" and result["workflow"]["mode"] == "upgrade"
    assert len(seen) == 1 and str(seen[0].url) == API_URL
    assert not (svc.store.home / "command.json").exists()
    assert not list((svc.store.home / "downloads").iterdir())


@pytest.mark.parametrize("fault", ["checksum", "package", "network"])
async def test_upgrade_failure_stops_without_apply(tmp_path, monkeypatch, fault):
    kwargs = {}
    if fault == "checksum":
        kwargs["checksum_override"] = b"0" * 64 + b"  server-monitor-v1.1.0.zip\n"
    elif fault == "package":
        kwargs["payload"] = b"invalid archive but matching SHA256"
    svc, seen = managed_service(tmp_path, monkeypatch, **kwargs)
    original = file_hashes(svc.store.root)
    if fault == "network":
        def failed(request):
            raise httpx.ConnectError("network unavailable")
        svc.transport = httpx.MockTransport(failed)
    result = await complete(svc)
    assert result["state"] == "error" and result["error"]
    assert result["failed_state"] == ("checking" if fault == "network" else "downloading")
    assert not (svc.store.home / "command.json").exists()
    assert not (svc.store.home / "staged.json").exists()
    assert file_hashes(svc.store.root) == original
    with svc.store.lock():
        pass


async def test_manager_loss_at_handoff_stops_before_queue(tmp_path, monkeypatch):
    svc, _ = managed_service(tmp_path, monkeypatch)
    actual_download = svc.download
    async def download_then_lose_manager():
        await actual_download()
        atomic_json(svc.store.home / "manager.json", {"session": SESSION, "heartbeat": time.time() - 30})
    monkeypatch.setattr(svc, "download", download_then_lose_manager)
    result = await complete(svc)
    assert result["state"] == "error" and "run_server.py" in result["error"]
    assert not (svc.store.home / "command.json").exists()
    assert read_json(svc.store.home / "staged.json")["version"] == "1.1.0"


@pytest.mark.parametrize("action", ["upgrade", "check", "download", "backup", "apply", "restart"])
async def test_upgrade_rejects_competing_actions(tmp_path, monkeypatch, action):
    svc, _ = managed_service(tmp_path, monkeypatch)
    started, resume = asyncio.Event(), asyncio.Event()
    check = svc.check
    async def pause_check():
        started.set()
        await resume.wait()
        await check()
    monkeypatch.setattr(svc, "check", pause_check)
    await svc.start("upgrade")
    await started.wait()
    try:
        with pytest.raises(UpdateBusy):
            if action in {"apply", "restart"}:
                svc.request_operation(action, "competitor")
            else:
                await svc.start(action, "competitor")
        assert svc.status()["state"] == "checking"
    finally:
        resume.set()
        await svc.task
    assert svc.status()["state"] == "queued"


async def test_unmanaged_upgrade_returns_setup_and_never_downloads(tmp_path, monkeypatch):
    svc, seen = service(tmp_path)
    host_runtime(monkeypatch)
    monkeypatch.setenv("MONITOR_UPDATE_SESSION", "unmatched-secret")
    snapshot = svc.status()
    capability = snapshot["capability"]
    assert capability["code"] == "manager_required" and not capability["can_apply"]
    setup = capability["setup"]
    assert setup["working_directory"] == str(svc.store.root)
    args = [sys.executable, "scripts/run_server.py", "--host", "0.0.0.0", "--port", "8080"]
    assert setup["command"] == (subprocess.list2cmdline(args) if os.name == "nt" else shlex.join(args))
    assert "MONITOR_UPDATE_SESSION" not in json.dumps(capability)
    assert "unmatched-secret" not in json.dumps(snapshot)
    with pytest.raises(UpdateError, match="run_server.py"):
        await svc.start("upgrade")
    assert seen == [] and svc.task is None
    assert not (svc.store.home / "command.json").exists()


async def test_upgrade_disk_and_running_version_mismatch_never_downloads(tmp_path, monkeypatch):
    svc, seen = managed_service(tmp_path, monkeypatch)
    svc.version = "0.9.0"
    with pytest.raises(UpdateError, match="磁盘代码与运行版本不一致"):
        await svc.start("upgrade")
    assert seen == [] and not (svc.store.home / "command.json").exists()


async def test_saved_disabled_proxy_stops_upgrade_without_direct_fallback(tmp_path, monkeypatch):
    svc, seen = managed_service(tmp_path, monkeypatch)
    save_update_proxy(svc.store.root, True, PROXY)
    save_update_proxy(svc.store.root, False)
    with pytest.raises(UpdateError, match="代理"):
        await svc.start("upgrade")
    assert seen == [] and not (svc.store.home / "command.json").exists()


@pytest.mark.parametrize("action", ["check", "download", "backup"])
async def test_new_manual_operation_clears_old_upgrade_workflow(tmp_path, monkeypatch, action):
    svc, _ = managed_service(tmp_path, monkeypatch)
    svc.store.status("up_to_date", "previous upgrade finished", workflow={"mode": "upgrade", "id": "old-workflow"})
    await svc.start(action)
    assert svc.status().get("workflow") is None
    await svc.task
    assert svc.status().get("workflow") is None


async def test_cached_manual_check_also_clears_old_upgrade_workflow(tmp_path, monkeypatch):
    svc, seen = managed_service(tmp_path, monkeypatch)
    svc.store.status("up_to_date", "previous upgrade finished", workflow={"mode": "upgrade", "id": "old-workflow"})
    svc._last_check = time.monotonic()
    result = await svc.start("check")
    assert result.get("workflow") is None and seen == [] and svc.task is None


@pytest.mark.parametrize("kind", ["container", "read_only", "managed"])
def test_capability_reports_specific_gate_and_setup_only_for_manager_required(tmp_path, monkeypatch, kind):
    svc, _ = managed_service(tmp_path, monkeypatch)
    if kind == "container":
        monkeypatch.setenv("MONITOR_DEPLOYMENT", "docker")
    elif kind == "read_only":
        monkeypatch.setattr(updates.os, "access", lambda *args: False)
    capability = svc.capability()
    assert capability["code"] == kind and capability["setup"] is None
    assert capability["can_apply"] is (kind == "managed")


async def test_upgrade_endpoint_keeps_admin_header_origin_guards_and_readable_errors(tmp_path, monkeypatch):
    from app.routes import updates as routes
    from test_routes import make_client, make_authed_client
    svc, seen = service(tmp_path)
    host_runtime(monkeypatch)
    monkeypatch.delenv("MONITOR_UPDATE_SESSION", raising=False)
    monkeypatch.setattr(routes, "get_update_service", lambda: svc)
    async with make_client() as client:
        assert (await client.post("/api/system/update/upgrade", headers={"X-Update-Request": "1"})).status_code == 401
    async with make_authed_client(monkeypatch) as client:
        assert (await client.post("/api/system/update/upgrade")).status_code == 403
        assert (await client.post("/api/system/update/upgrade", headers={"X-Update-Request": "1", "Origin": "http://foreign.invalid"})).status_code == 403
        response = await client.post("/api/system/update/upgrade", headers={"X-Update-Request": "1"})
        assert response.status_code == 400 and "run_server.py" in response.json()["detail"]
    assert seen == []


@pytest.mark.parametrize("state", ["available", "ready"])
async def test_cancelled_pipeline_recovers_intermediate_state_and_allows_retry(tmp_path, monkeypatch, state):
    svc, _ = managed_service(tmp_path, monkeypatch)
    paused = asyncio.Event()
    download = svc.download
    async def pause_download():
        if state == "ready":
            await download()
        paused.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(svc, "download", pause_download)
    await svc.start("upgrade")
    await paused.wait()
    active = svc.status()
    assert active["state"] == state and active["workflow"]["mode"] == "upgrade"
    svc.task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await svc.task
    # 新服务实例读取同一磁盘状态，模拟应用进程重启后刷新页面。
    reloaded = updates.UpdateService(svc.store.root, svc.version, svc.transport)
    recovered = reloaded.status()
    assert recovered["state"] == "error" and recovered["workflow"] is None
    assert "中断" in recovered["message"]
    await reloaded.start("upgrade")
    assert reloaded.status()["failed_state"] is None
    await reloaded.task
    assert reloaded.status()["state"] == "queued"


@pytest.mark.parametrize("state", ["available", "ready"])
@pytest.mark.parametrize("blocker", ["command.json", "transaction.json"])
def test_intermediate_pipeline_is_not_reset_while_manager_handoff_exists(tmp_path, monkeypatch, state, blocker):
    svc, _ = managed_service(tmp_path, monkeypatch)
    workflow = {"mode": "upgrade", "id": "handoff"}
    svc.store.status(state, "handoff in progress", workflow=workflow)
    atomic_json(svc.store.home / blocker, {"id": "pending"})
    snapshot = svc.status()
    assert snapshot["state"] == state and snapshot["workflow"] == workflow


@pytest.mark.parametrize("state", ["available", "ready"])
def test_manual_terminal_state_survives_status_refresh(tmp_path, monkeypatch, state):
    svc, _ = managed_service(tmp_path, monkeypatch)
    svc.store.status(state, "manual step finished", workflow=None)
    assert svc.status()["state"] == state
