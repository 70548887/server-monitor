"""单实例启动管理器：应用重启后仍可完成升级验证和失败回滚。

python scripts/run_server.py --host 127.0.0.1 --port 8080
"""
import argparse
import hashlib
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import time
import uuid
import zipfile
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def recover_before_import(root, host, port):
    """使用备份中的独立恢复原语；此函数仅依赖标准库，不导入待恢复的 app。"""
    home = root / ".updates"
    journal = home / "transaction.json"
    if not journal.exists():
        return None
    if home.is_symlink() or home.resolve().parent != root:
        raise RuntimeError("升级状态目录必须位于项目内")
    transaction = json.loads(journal.read_text(encoding="utf-8"))
    name = transaction.get("backup", "")
    if not re.fullmatch(r"backup-\d{8}-\d{6}-[0-9a-f]{8}", name):
        raise RuntimeError("事务备份名称无效")
    folder = home / "backups" / name
    if folder.resolve() != folder or not folder.is_dir():
        raise RuntimeError("事务备份目录无效")
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    archive = folder / "code.zip"
    if hashlib.sha256(archive.read_bytes()).hexdigest() != manifest["sha256"]:
        raise RuntimeError("事务备份完整性校验失败")
    python = manifest["python"]
    if not Path(python).is_file():
        raise RuntimeError("事务备份对应的Python环境不存在")
    # 备份的恢复模块不依赖 app 包。用它自己的解释器运行，避免新依赖污染恢复。
    # 恢复、锁和路径校验复用旧版本原语，重新加锁后校验事务未发生变化。
    worker = r'''
import hashlib, json, os, socket, sys, zipfile
from pathlib import Path
root, name, expected, host, port = sys.argv[1:]
root = Path(root)
home = root / ".updates"
folder = home / "backups" / name
manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
archive = folder / "code.zip"
if hashlib.sha256(archive.read_bytes()).hexdigest() != manifest["sha256"]:
    raise RuntimeError("Recovery archive checksum mismatch")
module = "app/services/update_runtime.py"
with zipfile.ZipFile(archive) as z:
    source = z.read(module)
if hashlib.sha256(source).hexdigest() != manifest["files"][module]:
    raise RuntimeError("Recovery code checksum mismatch")
ns = {"__name__":"monitor_recovery", "__file__":str(folder / module)}
exec(compile(source, module, "exec"), ns)
store = ns["RuntimeStore"](root)
with ns["OperationLock"](home / "manager.lock"), store.lock():
    transaction = ns["read_json"](home / "transaction.json")
    if transaction != json.loads(expected):
        raise RuntimeError("Recovery transaction changed; retry startup")
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        if os.name == "nt":
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        probe.bind((host, int(port)))
    restored = store.restore(name, transaction["changed_files"])
    ns["atomic_json"](home / "runtime.json", {"python":restored["python"]})
    store.status("error", "检测到中断的升级，已恢复升级前程序；数据库结构不作逆向回滚", 100, rolled_back=True)
    (home / "transaction.json").unlink()
'''
    subprocess.run([python, "-c", worker, str(root), name, json.dumps(transaction), host, str(port)],
                   cwd=root, check=True,
                   **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
    return python


if __name__ == "__main__":
    # 连 --help 都不能先导入 app：中断时包初始化文件可能已换，依赖文件尚未换。
    bootstrap_args = argparse.ArgumentParser(add_help=False)
    bootstrap_args.add_argument("--host", default="127.0.0.1")
    bootstrap_args.add_argument("--port", type=int, default=8080)
    bootstrap, _ = bootstrap_args.parse_known_args()
    if not 1 <= bootstrap.port <= 65535:
        bootstrap_args.error("port must be 1..65535")
    restored_python = recover_before_import(ROOT, bootstrap.host, bootstrap.port)
    if restored_python:
        os.execv(restored_python, [restored_python, str(ROOT / "scripts/run_server.py"), *sys.argv[1:]])

import httpx
from app.services.update_network import load_update_proxy, ensure_proxy_enabled
from app.services.update_runtime import (
    RuntimeStore, UpdateError, OperationLock, atomic_json, read_json, package_version,
    file_hashes, apply_files, validate_lock, quiet_process_options, UpdateBusy,
)


def redact_proxy_output(value, proxy_url):
    """代理凭据只在子进程环境中使用，原文不得落入安装日志。"""
    if not proxy_url:
        return value
    parsed = urlsplit(proxy_url)
    secrets = {proxy_url, unquote(proxy_url)}
    for credential in (parsed.username, parsed.password):
        if credential:
            secrets.update((credential, unquote(credential)))
    for secret in sorted(secrets, key=len, reverse=True):
        value = value.replace(secret, "***")
    # pip/requests 可能重新格式化 URL；其他含认证信息的 URL 也不写入日志。
    return re.sub(r"(?i)(https?://)[^\s/]*@", r"\1***@", value)


class ServerManager:
    def __init__(self, root, host="127.0.0.1", port=8080):
        self.root = Path(root).resolve()
        self.store = RuntimeStore(self.root)
        self.host, self.port = host, port
        self.session = uuid.uuid4().hex
        self.child = None
        self.stopping = False
        self.token = None
        self.child_env = None
        self.python = read_json(self.store.home / "runtime.json", {"python":sys.executable})["python"]
        if not Path(self.python).is_file():
            # 文件事务尚未提交时，新依赖环境可能因中断而缺失。
            # 先从校验通过的备份选择旧解释器；实际文件恢复仍在 run() 的端口检查和锁内执行。
            transaction = read_json(self.store.home / "transaction.json")
            if transaction:
                previous = self.store.backup_manifest(transaction.get("backup", ""))
                self.python = previous["python"]
            if not transaction or not Path(self.python).is_file():
                raise UpdateError("已配置的Python运行环境不存在，且没有可用的事务恢复环境")

    def heartbeat(self):
        atomic_json(self.store.home / "manager.json", {"pid":os.getpid(), "session":self.session,
                                                      "heartbeat":time.time(), "port":self.port})

    def environment(self):
        # 不把 .env 装入管理器自身环境；每个子进程重新读取，外部显式环境仍优先。
        from dotenv.main import DotEnv
        values = DotEnv(self.root / ".env", override=False).dict()
        return {**{k:v for k,v in values.items() if v is not None}, **os.environ}

    def start_child(self):
        self.token = uuid.uuid4().hex
        env = {**self.environment(), "MONITOR_UPDATE_SESSION":self.session, "MONITOR_INSTANCE":self.token,
               "PYTHONUNBUFFERED":"1"}
        self.child_env = env
        self.child = subprocess.Popen([self.python, "-m", "uvicorn", "app.main:app", "--host", self.host,
                                       "--port", str(self.port)], cwd=self.root, env=env, **quiet_process_options())

    def stop_child(self):
        if self.child is None or self.child.poll() is not None:
            return
        # 只操作本管理器直接创建并持有的子进程，不根据宽泛进程名结束服务。
        self.child.terminate()
        try:
            self.child.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.child.kill()
            self.child.wait(timeout=10)

    def _waiting_timeout(self, variable, default, minimum):
        value = self.environment().get(variable, str(default))
        try:
            seconds = int(value)
            if not minimum <= seconds <= 86400:
                raise ValueError
        except (TypeError, ValueError):
            raise UpdateError(f"{variable} 必须为 {minimum} 到 86400 之间的整数秒数") from None
        return seconds

    def healthy(self, expected, timeout=None):
        if timeout is None:
            timeout = self._waiting_timeout("MONITOR_UPDATE_HEALTH_TIMEOUT_SECONDS", 1800, 30)
        host = "::1" if ":" in self.host else "127.0.0.1"
        if self.host not in ("0.0.0.0", "::", "127.0.0.1", "localhost"):
            host = self.host
        authority = f"[{host}]" if ":" in host else host
        end = time.monotonic() + timeout
        allowed = [v.strip() for v in (self.child_env or self.environment()).get("ALLOWED_HOSTS", "").split(",") if v.strip()]
        headers = {}
        if allowed and "*" not in allowed:
            # 直连本机仍遵循部署配置的Host校验，不扩大公开允许域名。
            name = allowed[0]
            headers["Host"] = "monitor" + name[1:] if name.startswith("*.") else name
        with httpx.Client(base_url=f"http://{authority}:{self.port}", timeout=3, trust_env=False, headers=headers) as client:
            while time.monotonic() < end and not self.stopping:
                if self.child.poll() is not None:
                    return False
                try:
                    info = client.get("/healthz").json()
                    ready = client.get("/healthz/ready")
                    if (info.get("version") == expected and info.get("instance_id") == self.token
                            and ready.status_code == 200 and info.get("status") == "healthy"):
                        return True
                except (httpx.HTTPError, ValueError):
                    pass
                time.sleep(.5)
        return False

    def command(self, args, timeout, label, cwd=None, *, use_update_proxy=False):
        """输出脱敏后落本地日志；升级代理仅进入 pip 的子进程环境。"""
        env = {**self.environment(), "PIP_NO_INPUT":"1"}
        proxy_url = ""
        if use_update_proxy:
            network = load_update_proxy(self.root)
            ensure_proxy_enabled(network)
            proxy_url = network["url"]
            # 清除大小写混用的继承值，避免 NO_PROXY 绕过本页选择的代理。
            # 关闭时仅停用本页代理，用户自己的 pip 配置文件仍保持原有语义。
            proxy_keys = {"http_proxy", "https_proxy", "all_proxy", "no_proxy", "pip_proxy"}
            env = {key:value for key,value in env.items() if key.lower() not in proxy_keys}
            if network["enabled"]:
                for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
                    env[key] = env[key.lower()] = proxy_url
                env["PIP_PROXY"] = proxy_url
        # 先从管道读取并脱敏，再写日志；不能先把含密码的原始输出写到临时文件。
        with (self.store.home / "worker.log").open("a", encoding="utf-8") as log:
            process = subprocess.Popen(args, cwd=cwd or self.root, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                       env=env, **quiet_process_options())
            def copy_output():
                with process.stdout:
                    for line in process.stdout:
                        log.write(redact_proxy_output(line, proxy_url))
                        log.flush()
            with ThreadPoolExecutor(max_workers=1) as executor:
                output = executor.submit(copy_output)
                try:
                    code = process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                    output.result()
                    raise UpdateError(label + "超时") from None
                output.result()
        if code:
            raise UpdateError(label + "失败，详见本机 .updates/worker.log")

    def prepare_python(self, staged):
        validate_lock(staged)
        if (staged / "requirements.lock").read_bytes() == (self.root / "requirements.lock").read_bytes():
            return self.python
        runtime = self.store.home / "runtimes" / uuid.uuid4().hex
        # 每个版本使用独立依赖环境；安装失败不污染当前Python，回滚直接切回旧解释器。
        self.command([self.python, "-m", "venv", str(runtime)], 120, "创建升级运行环境")
        python = runtime / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        self.command([str(python), "-m", "pip", "install", "--disable-pip-version-check", "--only-binary=:all:",
                      "-r", str(staged / "requirements.lock")], 900, "安装升级依赖", use_update_proxy=True)
        return str(python)

    def validate_code(self, staged, python):
        self.command([python, "-m", "compileall", "-q", str(staged / "app"), str(staged / "scripts")], 60, "检查代码语法")
        self.command([python, "-c", "import app.main"], 60, "检查应用导入", cwd=staged)

    def migrate(self, staged, python):
        script = staged / "scripts/bootstrap_database.py"
        if not script.is_file():
            raise UpdateError("升级包缺少数据库引导脚本")
        # 首次旧库大表转换与升级锁等待均可能超过原来的十分钟。
        timeout = self._waiting_timeout("MONITOR_UPDATE_MIGRATION_TIMEOUT_SECONDS", 3600, 60)
        self.command([python, str(script)], timeout, "检查数据库结构")

    def recover_transaction(self):
        transaction = read_json(self.store.home / "transaction.json")
        if not transaction:
            return
        # 启动管理器崩溃/系统断电后，先恢复未提交的文件事务再启动应用。
        manifest = self.store.restore(transaction["backup"], transaction["changed_files"])
        self.python = manifest["python"]
        atomic_json(self.store.home / "runtime.json", {"python":self.python})
        (self.store.home / "transaction.json").unlink()
        self.store.status("error", "检测到中断的升级，已恢复升级前程序；数据库结构不作逆向回滚", 100, rolled_back=True)

    def execute(self, request):
        # API写入命令后尚需退出其短暂临界区，管理器在此等锁，避免交接竞态。
        lock = self.store.lock()
        deadline = time.monotonic() + 10
        while True:
            try:
                lock.__enter__()
                break
            except UpdateBusy:
                if time.monotonic() >= deadline:
                    self.store.status("error", "升级操作锁等待超时，请重试")
                    return
                time.sleep(.1)
        try:
            self._execute_locked(request)
        finally:
            lock.__exit__(None, None, None)

    def _execute_locked(self, request):
        old_python = self.python
        backup, transaction, stopped = None, None, False
        # 锁由 execute 持有，贯穿备份、替换、重启和失败恢复。
        try:
            if (self.store.home / "transaction.json").exists():
                raise UpdateError("存在未恢复的升级事务，请重新启动管理器完成恢复")
            action = request["action"]
            if action not in ("apply", "rollback", "restart"):
                raise UpdateError("未知管理器操作")
            if action == "restart":
                self.store.status("restarting", "正在重启服务", 50)
                self.stop_child()
                self.start_child()
                if not self.healthy(package_version(self.root)):
                    raise UpdateError("重启后的健康检查未通过")
                self.store.status("complete", "服务已重启并通过健康检查", 100)
                return

            self.store.status("preparing", "正在校验文件与准备运行环境", 10)
            if action == "apply":
                staged_info = read_json(self.store.home / "staged.json")
                ident = staged_info.get("id", "")
                if not re.fullmatch(r"[0-9a-f]{32}", ident):
                    raise UpdateError("缺少已校验的升级包")
                staged = self.store.home / "staging" / ident
                if file_hashes(staged) != staged_info["files"]:
                    raise UpdateError("已下载的升级文件发生变化，请重新下载")
                from packaging.version import Version
                if Version(package_version(staged)) <= Version(package_version(self.root)):
                    raise UpdateError("目标版本不高于当前版本")
                if not (staged / "scripts/run_server.py").is_file():
                    raise UpdateError("目标版本不支持当前升级管理器")
                target_python = self.prepare_python(staged)
            else:
                selected = self.store.backup_manifest(request.get("backup") or "")
                from app.services.update_runtime import extract_package
                staged = self.store.home / "staging" / ("rollback-" + uuid.uuid4().hex)
                files = extract_package(self.store.home / "backups" / selected["name"] / "code.zip", staged, selected["version"], prefixed=False)
                if files != selected["files"]:
                    raise UpdateError("备份文件清单不一致")
                target_python = selected["python"]
                if not Path(target_python).is_file():
                    raise UpdateError("备份对应的运行环境不存在")
            self.validate_code(staged, target_python)
            self.store.status("backing_up", "正在保存升级前程序与环境信息", 35)
            backup = self.store.backup(self.python)
            target_files = file_hashes(staged)
            self.store.status("applying", "正在停止检测并应用程序更新", 50, last_backup=backup["name"], target_version=package_version(staged))
            self.stop_child()
            stopped = True
            transaction = {"backup":backup["name"], "changed_files":list(set(backup["files"]) | set(target_files))}
            atomic_json(self.store.home / "transaction.json", transaction)
            if action == "apply":
                # 仅前向/幂等引导；数据库不随代码回滚，发布需保持向后兼容。
                self.migrate(staged, target_python)
            apply_files(self.root, staged, backup["files"])
            self.python = target_python
            atomic_json(self.store.home / "runtime.json", {"python":self.python})
            self.store.status("restarting", "正在启动新版本并检查服务健康", 80)
            self.start_child()
            if not self.healthy(package_version(staged)):
                raise UpdateError("新版本健康检查未通过")
            (self.store.home / "transaction.json").unlink()
            transaction = None
            (self.store.home / "staged.json").unlink(missing_ok=True)
            self.store.status("complete", "升级完成，服务健康" if action == "apply" else "程序已回滚，服务健康", 100, staged=None, rolled_back=False)
        except Exception as exc:
            message = str(exc) if isinstance(exc, UpdateError) else f"升级处理失败（{type(exc).__name__}）"
            restored = False
            if transaction:
                try:
                    self.store.status("rolling_back", "更新失败，正在恢复备份", 85)
                    self.stop_child()
                    self.store.restore(backup["name"], transaction["changed_files"])
                    self.python = old_python
                    atomic_json(self.store.home / "runtime.json", {"python":self.python})
                    self.start_child()
                    restored = self.healthy(backup["version"])
                    if restored:
                        (self.store.home / "transaction.json").unlink()
                    else:
                        message += "；恢复后健康检查仍失败，请检查本机日志"
                except Exception:
                    message += "；自动恢复未完成，备份与事务记录已保留"
            elif stopped:
                self.python = old_python
                self.start_child()
            self.store.status("error", message + ("；已自动恢复升级前程序与依赖" if restored else ""), 100,
                              error=message, rolled_back=restored)
        finally:
            with (self.store.home / "history.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps({"time":time.time(), "action":request.get("action"), "actor":request.get("actor"),
                                    "result":self.store.read().get("state")}, ensure_ascii=False) + "\n")

    def run(self):
        with OperationLock(self.store.home / "manager.lock"):
            # 管理器异常退出后可能留下旧子进程；确认端口空闲才允许恢复磁盘事务。
            family = socket.AF_INET6 if ":" in self.host else socket.AF_INET
            with socket.socket(family, socket.SOCK_STREAM) as probe:
                if os.name == "nt":
                    probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                try:
                    probe.bind((self.host, self.port))
                except OSError as exc:
                    raise UpdateError("服务端口已被占用，请先停止旧监控实例；未修改程序文件") from exc
            with self.store.lock():
                self.recover_transaction()
                (self.store.home / "command.json").unlink(missing_ok=True)
                if self.store.read().get("state") in {"queued", "preparing", "applying", "restarting", "rolling_back", "backing_up", "downloading", "checking"}:
                    self.store.status("error", "上次操作已中断，请重新检查更新")
            self.heartbeat()
            self.start_child()
            future = None
            try:
                with ThreadPoolExecutor(max_workers=1) as executor:
                    while not self.stopping:
                        self.heartbeat()
                        if future and future.done():
                            future.result()
                            future = None
                        command = read_json(self.store.home / "command.json")
                        if command and not future:
                            (self.store.home / "command.json").unlink()
                            if command.get("session") != self.session:
                                self.store.status("error", "升级请求属于旧会话，请重新提交")
                            else:
                                future = executor.submit(self.execute, command)
                        if not future and self.child.poll() is not None:
                            # 常规进程异常交由外部服务管理器处理，避免无限重启掩盖错误。
                            return self.child.returncode or 1
                        time.sleep(.5)
            finally:
                self.stop_child()
                (self.store.home / "manager.json").unlink(missing_ok=True)
        return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be 1..65535")
    manager = ServerManager(ROOT, args.host, args.port)
    def stop(*_):
        manager.stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    return manager.run()


if __name__ == "__main__":
    sys.exit(main())
