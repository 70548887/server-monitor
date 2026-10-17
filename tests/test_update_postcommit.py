"""提交后的状态/清理/历史 I/O 故障不得丢失健康子进程或终止管理器。"""
import asyncio
from pathlib import Path
import threading
from types import SimpleNamespace

import httpx
import pytest

from app.services.update_runtime import UpdateError, atomic_json, package_version
from app.services.updates import UpdateService
from scripts.run_server import ServerManager
from test_update_manager import manager, stage
from test_update_pipeline_manager import live_pipeline, request_upgrade, wait_for


EVIDENCE = Path("logs/update-postcommit-fix-20261017")


async def verify_health_pid(mgr, version):
    assert mgr.child is not None and mgr.child.poll() is None
    assert await asyncio.to_thread(mgr.healthy, version, 5)
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{mgr.port}", trust_env=False) as client:
        response = await client.get("/healthz")
        response.raise_for_status()
        health = response.json()
    assert health["version"] == version and health["pid"] == mgr.child.pid
    return health


@pytest.mark.parametrize("persistent", [False, True], ids=["complete-once", "complete-persistent"])
async def test_real_manager_survives_postcommit_status_failure_and_accepts_restart(tmp_path, monkeypatch, persistent):
    tracked = []
    evidence = {"persistent_failure": persistent}
    try:
        async with live_pipeline(tmp_path, monkeypatch) as (svc, mgr, _, transitions, config):
            tracked.append(mgr.child)
            initial_pid = mgr.child.pid
            start_child = mgr.start_child
            def track_child():
                start_child()
                tracked.append(mgr.child)
            monkeypatch.setattr(mgr, "start_child", track_child)
            write_status = mgr.store.status
            failed_write = threading.Event()
            fault = {"enabled": True, "attempts": 0, "committed_child": None}
            def fail_complete(state, message, progress=0, **extra):
                if state == "complete" and fault["enabled"]:
                    fault["attempts"] += 1
                    fault["committed_child"] = mgr.child
                    failed_write.set()
                    if not persistent:
                        fault["enabled"] = False
                    raise OSError("fixture postcommit status write unavailable")
                return write_status(state, message, progress, **extra)
            monkeypatch.setattr(mgr.store, "status", fail_complete)
            try:
                await request_upgrade(svc, monkeypatch)
                await wait_for(failed_write.is_set, 20)
                committed_child = fault["committed_child"]
                assert committed_child is not None and committed_child.pid != initial_pid
                if persistent:
                    await asyncio.sleep(.8)
                    assert mgr.child is committed_child, "postcommit I/O must not start another child"
                    await verify_health_pid(mgr, "1.1.0")
                    assert mgr.store.read()["state"] != "error"
                    assert (mgr.store.home / "manager.json").exists()
                    # 修复磁盘条件后，管理器必须自动补写终态，解除永久 busy。
                    fault["enabled"] = False
                result = await wait_for(lambda: (value if (value := mgr.store.read()).get("state") in {"complete", "error"} else None), 8)
                evidence.update(state_after_fault=result["state"], failed_write_attempts=fault["attempts"],
                                committed_pid=committed_child.pid, manager_child_pid=mgr.child.pid)
                assert result["state"] == "complete", result
                assert mgr.child is committed_child and mgr.child.poll() is None
                assert len(tracked) == 2, "exactly initial and replacement processes must exist"
                await verify_health_pid(mgr, "1.1.0")
                assert not (mgr.store.home / "transaction.json").exists()
                assert package_version(mgr.root) == "1.1.0"
                assert (mgr.root / "configs/app.yaml").read_text(encoding="utf-8") == config
                assert (mgr.root / ".env").read_text(encoding="utf-8") == "PIPELINE_CONFIG_MARKER=preserved\n"
                follower = UpdateService(mgr.root, "1.1.0", svc.transport)
                queued = follower.request_operation("restart", "postcommit-regression")
                assert queued["state"] == "queued"
                restarted = await wait_for(lambda: (value if (value := mgr.store.read()).get("state") in {"complete", "error"} and value.get("operation_id") == queued["operation_id"] else None), 15)
                assert restarted["state"] == "complete", restarted
                assert mgr.child.pid != committed_child.pid and committed_child.poll() is not None
                await verify_health_pid(mgr, "1.1.0")
                assert len(tracked) == 3
                evidence.update(restart_accepted=True, restart_pid=mgr.child.pid, configuration_preserved=True,
                                transaction_removed=True, transitions=transitions)
            finally:
                fault["enabled"] = False
                evidence.update(tracked_pids=[proc.pid for proc in tracked],
                                tracked_returncodes=[proc.poll() for proc in tracked])
                EVIDENCE.mkdir(parents=True, exist_ok=True)
                atomic_json(EVIDENCE / ("complete-persistent.json" if persistent else "complete-once.json"), evidence)
    finally:
        # 旧实现可能已丢失新健康进程引用；只精确清理本测试创建的 Popen。
        for proc in tracked:
            if proc.poll() is None:
                proc.terminate()
                try:
                    await asyncio.to_thread(proc.wait, 8)
                except Exception:
                    proc.kill()
                    await asyncio.to_thread(proc.wait, 5)


@pytest.mark.parametrize("fault_name", ["staged_unlink", "history_open"])
def test_postcommit_cleanup_or_history_failure_does_not_restart_health_child(manager, monkeypatch, fault_name):
    mgr, calls = manager
    stage(mgr)
    marker = "PRIVATE_CONFIG_PRESERVED=1\n"
    (mgr.root / ".env").write_text(marker, encoding="utf-8")
    unlink = Path.unlink
    open_path = Path.open
    injected = {"count": 0}
    def failing_unlink(path, *args, **kwargs):
        if path == mgr.store.home / "staged.json":
            injected["count"] += 1
            raise OSError("fixture staged cleanup unavailable")
        return unlink(path, *args, **kwargs)
    def failing_open(path, *args, **kwargs):
        if path == mgr.store.home / "history.jsonl":
            injected["count"] += 1
            raise OSError("fixture history append unavailable")
        return open_path(path, *args, **kwargs)
    if fault_name == "staged_unlink":
        monkeypatch.setattr(Path, "unlink", failing_unlink)
    else:
        monkeypatch.setattr(Path, "open", failing_open)
    mgr.execute({"action": "apply", "actor": "postcommit-regression"})
    assert injected["count"] >= 1
    assert calls == ["validate", "stop", "migrate", "start"]
    assert package_version(mgr.root) == "1.1.0"
    assert mgr.store.read()["state"] == "complete"
    assert not (mgr.store.home / "transaction.json").exists()
    assert (mgr.root / ".env").read_text(encoding="utf-8") == marker


def test_transaction_commit_failure_still_rolls_back_before_postcommit_boundary(manager, monkeypatch):
    mgr, calls = manager
    stage(mgr)
    unlink = Path.unlink
    injected = {"count": 0}
    def fail_once(path, *args, **kwargs):
        if path == mgr.store.home / "transaction.json" and not injected["count"]:
            injected["count"] += 1
            raise OSError("fixture commit journal unlink unavailable")
        return unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", fail_once)
    mgr.execute({"action": "apply", "actor": "precommit-regression"})
    assert injected["count"] == 1
    assert package_version(mgr.root) == "1.0.0"
    result = mgr.store.read()
    assert result["state"] == "error" and result["rolled_back"]
    assert calls.count("start") == 2
    assert not (mgr.store.home / "transaction.json").exists()


def test_postcommit_status_retry_keeps_target_python_runtime(manager, monkeypatch):
    mgr, calls = manager
    stage(mgr)
    original_python = mgr.python
    target_python = str(mgr.store.home / "runtimes/target-python/bin/python")
    monkeypatch.setattr(mgr, "prepare_python", lambda *_: target_python)
    write_status = mgr.store.status
    injected = {"count": 0}
    def fail_complete_once(state, message, progress=0, **extra):
        if state == "complete" and not injected["count"]:
            injected["count"] += 1
            raise OSError("fixture completion write unavailable")
        return write_status(state, message, progress, **extra)
    monkeypatch.setattr(mgr.store, "status", fail_complete_once)
    mgr.execute({"action": "apply", "actor": "target-runtime-regression"})
    assert injected["count"] == 1
    assert mgr.python == target_python and mgr.python != original_python
    assert package_version(mgr.root) == "1.1.0"
    assert mgr.store.read()["state"] == "complete"
    assert calls == ["validate", "stop", "migrate", "start"]
    assert not (mgr.store.home / "transaction.json").exists()


def test_start_child_rejects_live_child_without_overwriting_reference_or_token(manager):
    mgr, calls = manager
    child = SimpleNamespace(pid=987654, poll=lambda: None)
    mgr.child = child
    mgr.token = "existing-healthy-instance"
    # 调用真实方法，仅 child 是进程状态替身；存活保护须早于任何 Popen 或 token 替换。
    with pytest.raises(UpdateError):
        ServerManager.start_child(mgr)
    assert mgr.child is child
    assert mgr.token == "existing-healthy-instance"
    assert not calls
