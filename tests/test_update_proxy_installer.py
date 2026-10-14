"""升级依赖代理隔离、日志脱敏和失败路径；不执行真实 pip 或外网请求。"""
import io
import os
from pathlib import Path
import subprocess
import sys

import pytest

from app.services.update_runtime import UpdateError, atomic_json
from scripts import run_server
from scripts.run_server import ServerManager


PROXY = "http://upgrade-user:secret%40value@proxy.example.invalid:3128"
PROXY_KEYS = {"http_proxy", "https_proxy", "all_proxy", "no_proxy", "pip_proxy"}


@pytest.fixture
def manager(tmp_path):
    root = tmp_path / "installed"
    root.mkdir()
    (root / "requirements.lock").write_text("fastapi==0.115.0\n", encoding="utf-8")
    return ServerManager(root)


def capture_process(monkeypatch, output="installed fixture\n"):
    calls = []
    class Process:
        def __init__(self, args, **kwargs):
            calls.append((args, kwargs))
            self.stdout = io.StringIO(output)
        def wait(self, timeout=None):
            return 0
    monkeypatch.setattr(run_server.subprocess, "Popen", Process)
    return calls


def test_pip_uses_saved_proxy_without_inheriting_bypass_or_changing_global_env(manager, monkeypatch):
    atomic_json(manager.store.home / "proxy.json", {"enabled": True, "url": PROXY})
    inherited = {"http_proxy": "http://old.invalid:80", "NO_PROXY": "*", "PIP_PROXY": "http://old.invalid:80",
                 "PIP_INDEX_URL": "https://index.example.invalid/simple", "UNCHANGED": "yes"}
    monkeypatch.setattr(manager, "environment", lambda: inherited.copy())
    environment_before = dict(os.environ)
    calls = capture_process(monkeypatch, PROXY + "\nsecret@value\nupgrade-user\n")
    args = [sys.executable, "-m", "pip", "install", "-r", "requirements.lock"]
    manager.command(args, 10, "安装依赖", use_update_proxy=True)
    captured_args, kwargs = calls[0]
    env = kwargs["env"]
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy", "PIP_PROXY"):
        assert env[key] == PROXY
    assert not any(key.lower() == "no_proxy" for key in env)
    assert env["PIP_INDEX_URL"] == inherited["PIP_INDEX_URL"]
    assert env["UNCHANGED"] == "yes"
    assert env["PIP_NO_INPUT"] == "1"
    assert captured_args == args and PROXY not in repr(captured_args)
    assert inherited["http_proxy"] == "http://old.invalid:80"
    assert dict(os.environ) == environment_before
    log = (manager.store.home / "worker.log").read_text(encoding="utf-8")
    assert "secret" not in log and "upgrade-user" not in log


@pytest.mark.parametrize("saved", [None, {"enabled": False, "url": PROXY}])
def test_disabled_pip_proxy_removes_inherited_proxy_variables(manager, monkeypatch, saved):
    if saved is not None:
        atomic_json(manager.store.home / "proxy.json", saved)
    inherited = {key: "http://inherited.invalid:80" for key in
                 ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy",
                  "NO_PROXY", "no_proxy", "PIP_PROXY", "pip_proxy", "Http_Proxy")}
    inherited["PIP_INDEX_URL"] = "https://index.example.invalid/simple"
    monkeypatch.setattr(manager, "environment", lambda: inherited)
    calls = capture_process(monkeypatch)
    if saved and saved["url"]:
        with pytest.raises(UpdateError, match="未启用"):
            manager.command(["fixture-python", "-m", "pip"], 10, "安装依赖", use_update_proxy=True)
        assert not calls
        return
    manager.command(["fixture-python", "-m", "pip"], 10, "安装依赖", use_update_proxy=True)
    env = calls[0][1]["env"]
    assert not any(key.lower() in PROXY_KEYS for key in env)
    assert env["PIP_INDEX_URL"] == inherited["PIP_INDEX_URL"]
    assert "HTTP_PROXY" in inherited


def test_non_pip_command_does_not_load_or_apply_saved_update_proxy(manager, monkeypatch):
    def forbidden_load(*args):
        pytest.fail("非 pip 子进程不应读取升级代理")
    monkeypatch.setattr(run_server, "load_update_proxy", forbidden_load)
    monkeypatch.setattr(manager, "environment", lambda: {"APP_SETTING": "kept"})
    calls = capture_process(monkeypatch)
    manager.command(["fixture-python", "-m", "compileall"], 10, "检查代码语法")
    env = calls[0][1]["env"]
    assert env["APP_SETTING"] == "kept"
    assert not any(key.lower() in PROXY_KEYS for key in env)


def test_prepare_python_only_enables_proxy_for_dependency_install(manager, tmp_path, monkeypatch):
    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "requirements.lock").write_text("fastapi==0.115.1\n", encoding="utf-8")
    calls = []
    monkeypatch.setattr(manager, "command", lambda *args, **kwargs: calls.append((args, kwargs)))
    python = manager.prepare_python(staged)
    assert len(calls) == 2
    assert calls[0][0][0][1:3] == ["-m", "venv"]
    assert not calls[0][1].get("use_update_proxy", False)
    assert calls[1][0][0][1:4] == ["-m", "pip", "install"]
    assert calls[1][1]["use_update_proxy"] is True
    assert calls[1][0][0][0] == python
    assert PROXY not in repr(calls)


def test_unchanged_lock_preserves_runtime_and_skips_installer(manager, tmp_path, monkeypatch):
    staged = tmp_path / "same-lock"
    staged.mkdir()
    (staged / "requirements.lock").write_bytes((manager.root / "requirements.lock").read_bytes())
    monkeypatch.setattr(manager, "command", lambda *args, **kwargs: pytest.fail("没有依赖变更不应安装"))
    assert manager.prepare_python(staged) == manager.python


def test_real_process_failure_redacts_stdout_stderr_before_logging(manager):
    atomic_json(manager.store.home / "proxy.json", {"enabled": True, "url": PROXY})
    script = """import os, sys
from urllib.parse import unquote, urlsplit
url = os.environ['HTTPS_PROXY']
assert os.environ['PIP_PROXY'] == url
print('stdout ' + url, flush=True)
print('stderr ' + unquote(url), file=sys.stderr, flush=True)
parts = urlsplit(url)
print(parts.username + ' ' + parts.password + ' ' + unquote(parts.password), flush=True)
sys.exit(7)
"""
    with pytest.raises(UpdateError, match="安装依赖失败") as error:
        manager.command([sys.executable, "-c", script], 10, "安装依赖", use_update_proxy=True)
    log = (manager.store.home / "worker.log").read_text(encoding="utf-8")
    assert "stdout" in log and "stderr" in log and "***" in log
    for secret in (PROXY, "upgrade-user", "secret%40value", "secret@value"):
        assert secret not in log and secret not in str(error.value)


def test_real_process_timeout_drains_sanitized_output(manager):
    atomic_json(manager.store.home / "proxy.json", {"enabled": True, "url": PROXY})
    script = "import os,time; print(os.environ['PIP_PROXY'],flush=True); time.sleep(30)"
    with pytest.raises(UpdateError, match="安装依赖超时"):
        manager.command([sys.executable, "-c", script], .5, "安装依赖", use_update_proxy=True)
    log = (manager.store.home / "worker.log").read_text(encoding="utf-8")
    assert "***" in log
    assert "secret" not in log and "upgrade-user" not in log


def test_invalid_saved_proxy_fails_before_launching_installer(manager, monkeypatch):
    atomic_json(manager.store.home / "proxy.json", {"enabled": True, "url": "file:///not-a-proxy"})
    calls = capture_process(monkeypatch)
    with pytest.raises(UpdateError):
        manager.command(["fixture-python", "-m", "pip"], 10, "安装依赖", use_update_proxy=True)
    assert calls == []
