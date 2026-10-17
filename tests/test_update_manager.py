"""独立管理器升级/重启/回滚的文件事务和真实子进程验收。"""
import json
from pathlib import Path
import socket
import sys
import uuid

import pytest

from app.services.update_runtime import atomic_json, file_hashes, read_json, package_version, UpdateError
from scripts.run_server import ServerManager
from test_updates import project


@pytest.fixture
def manager(tmp_path,monkeypatch):
    root=project(tmp_path/"app-root")
    manager=ServerManager(root)
    calls=[]
    monkeypatch.setattr(manager,"validate_code",lambda *args:calls.append("validate"))
    monkeypatch.setattr(manager,"migrate",lambda *args:calls.append("migrate"))
    monkeypatch.setattr(manager,"stop_child",lambda:calls.append("stop"))
    monkeypatch.setattr(manager,"start_child",lambda:calls.append("start"))
    monkeypatch.setattr(manager,"healthy",lambda *args:True)
    return manager,calls


def stage(manager,version="1.1.0"):
    ident=uuid.uuid4().hex
    folder=project(manager.store.home/"staging"/ident,version)
    atomic_json(manager.store.home/"staged.json",{"id":ident,"files":file_hashes(folder),"version":version})
    return folder


def test_apply_stops_before_replace_and_starts_new_version(manager):
    mgr,calls=manager
    stage(mgr)
    (mgr.root/".env").write_text('private')
    mgr.execute({"action":"apply","actor":"test"})
    assert calls==["validate","stop","migrate","start"]
    assert package_version(mgr.root)=="1.1.0"
    assert mgr.store.read()["state"]=="complete"
    assert mgr.store.backups()[0]["version"]=="1.0.0"
    assert (mgr.root/".env").read_text()=='private'
    assert not (mgr.store.home/"transaction.json").exists()


def test_bad_new_health_restores_old_program_and_runtime(manager,monkeypatch):
    mgr,calls=manager
    stage(mgr)
    original=file_hashes(mgr.root)
    old_python=mgr.python
    results=iter([False,True])
    monkeypatch.setattr(mgr,"healthy",lambda *args:next(results))
    mgr.execute({"action":"apply","actor":"test"})
    assert file_hashes(mgr.root)==original
    assert mgr.python==old_python
    assert mgr.store.read()["rolled_back"] is True
    assert mgr.store.read()["state"]=="error"
    assert mgr.store.read()["failed_state"] == "restarting"
    assert calls.count("start")==2


def test_schema_failure_restores_and_restarts_old_version(manager,monkeypatch):
    mgr,calls=manager
    stage(mgr)
    def broken(*args):raise UpdateError("schema unavailable")
    monkeypatch.setattr(mgr,"migrate",broken)
    mgr.execute({"action":"apply"})
    assert package_version(mgr.root)=="1.0.0"
    assert mgr.store.read()["rolled_back"] is True
    assert calls[-1]=="start"


def test_preflight_failure_leaves_running_process_untouched(manager,monkeypatch):
    mgr,calls=manager
    stage(mgr)
    def broken(*args):raise UpdateError("dependencies unavailable")
    monkeypatch.setattr(mgr,"prepare_python",broken)
    mgr.execute({"action":"apply"})
    assert calls==[]
    assert package_version(mgr.root)=="1.0.0"
    assert mgr.store.read()["state"]=="error"
    assert mgr.store.read()["failed_state"] == "preparing"


def test_tampered_staging_rejected_before_service_stop(manager):
    mgr,calls=manager
    folder=stage(mgr)
    (folder/"app/main.py").write_text("tampered=1")
    mgr.execute({"action":"apply"})
    assert calls==[]
    assert "发生变化" in mgr.store.read()["message"]


def test_manual_rollback_creates_new_backup_and_skips_reverse_ddl(manager):
    mgr,calls=manager
    backup=mgr.store.backup()
    stage(mgr)
    mgr.execute({"action":"apply"})
    calls.clear()
    mgr.execute({"action":"rollback","backup":backup["name"]})
    assert package_version(mgr.root)=="1.0.0"
    assert calls==["validate","stop","start"]
    assert len(mgr.store.backups())==3


def test_crash_journal_recovers_on_manager_start(manager):
    mgr,_=manager
    backup=mgr.store.backup()
    original=file_hashes(mgr.root)
    (mgr.root/"app/main.py").write_text("broken")
    (mgr.root/"app/partial.py").write_text("new")
    atomic_json(mgr.store.home/"transaction.json",{"backup":backup["name"],"changed_files":list(file_hashes(mgr.root))})
    mgr.recover_transaction()
    assert file_hashes(mgr.root)==original
    assert not (mgr.store.home/"transaction.json").exists()
    assert mgr.store.read()["rolled_back"]


def test_missing_new_python_still_allows_pending_transaction_recovery(tmp_path):
    root = project(tmp_path / "interrupted")
    mgr = ServerManager(root)
    original = file_hashes(root)
    backup = mgr.store.backup(sys.executable)
    (root / "app/__init__.py").write_text('__version__ = "1.1.0"\n')
    atomic_json(mgr.store.home / "runtime.json", {"python": str(root / ".updates/runtimes/missing/python.exe")})
    atomic_json(mgr.store.home / "transaction.json", {"backup": backup["name"], "changed_files": list(original)})
    recovered = ServerManager(root)
    assert recovered.python == sys.executable
    assert package_version(root) == "1.1.0"  # 未取得启动锁前不得改文件。
    recovered.recover_transaction()
    assert file_hashes(root) == original
    assert read_json(mgr.store.home / "runtime.json")["python"] == sys.executable
    assert not (mgr.store.home / "transaction.json").exists()


def test_missing_python_without_recovery_backup_is_not_silently_replaced(tmp_path):
    root = project(tmp_path / "no-backup")
    mgr = ServerManager(root)
    atomic_json(mgr.store.home / "runtime.json", {"python": str(root / "missing-python.exe")})
    with pytest.raises(UpdateError, match="没有可用的事务恢复环境"):
        ServerManager(root)


def test_existing_listener_prevents_transaction_recovery(tmp_path):
    root=project(tmp_path/"fixture")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1",0));listener.listen()
        mgr=ServerManager(root,port=listener.getsockname()[1])
        original=file_hashes(root)
        with pytest.raises(UpdateError,match="端口已被占用"):
            mgr.run()
        assert file_hashes(root)==original


def test_corrupted_backup_does_not_modify_program(manager):
    mgr,calls=manager
    backup=mgr.store.backup()
    archive=mgr.store.home/"backups"/backup["name"]/"code.zip"
    archive.write_bytes(b"corrupt")
    original=file_hashes(mgr.root)
    mgr.execute({"action":"rollback","backup":backup["name"]})
    assert calls==[]
    assert file_hashes(mgr.root)==original
    assert "完整性" in mgr.store.read()["message"]


MINIMAL_APP = '''from fastapi import FastAPI
from app import __version__
import os
app=FastAPI()
@app.get("/healthz")
def health(): return {"status":"healthy","version":__version__,"instance_id":os.getenv("MONITOR_INSTANCE")}
@app.get("/healthz/ready")
def ready(): return {"ready":True}
'''


def test_real_process_apply_and_rollback_health(tmp_path):
    # 独立临时目录和临时端口，真实Python/uvicorn进程；没有外部节点或数据库。
    root=project(tmp_path/"live-fixture")
    (root/"app/main.py").write_text(MINIMAL_APP)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1",0));port=sock.getsockname()[1]
    mgr=ServerManager(root,port=port)
    mgr.start_child()
    try:
        assert mgr.healthy("1.0.0",timeout=12)
        initial_pid=mgr.child.pid
        folder=stage(mgr)
        (folder/"app/main.py").write_text(MINIMAL_APP)
        staged=read_json(mgr.store.home/"staged.json")
        staged["files"]=file_hashes(folder)
        atomic_json(mgr.store.home/"staged.json",staged)
        mgr.execute({"action":"apply","actor":"integration"})
        assert mgr.store.read()["state"]=="complete"
        assert mgr.healthy("1.1.0",timeout=5)
        assert mgr.child.pid != initial_pid
        backup=mgr.store.read()["last_backup"]
        mgr.execute({"action":"rollback","actor":"integration","backup":backup})
        assert mgr.store.read()["state"]=="complete"
        assert mgr.healthy("1.0.0",timeout=5)
    finally:
        mgr.stop_child()
