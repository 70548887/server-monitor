"""在线升级的磁盘事务与独立启动管理器共用原语。

只管理程序文件；.env、configs、数据库、日志、.git 和用户文件始终保留。
"""
import ast
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile

from packaging.requirements import Requirement
from packaging.version import Version

MANAGED_DIRS = {"app", "scripts"}
MANAGED_FILES = {"requirements.txt", "requirements.lock", "README.md", "CHANGELOG.md", "robots.txt", "anti-crawling.txt"}
MAX_PACKAGE = 100 * 1024 * 1024
MAX_EXPANDED = 300 * 1024 * 1024
BUSY = {"checking", "downloading", "backing_up", "queued", "preparing", "applying", "restarting", "rolling_back"}


class UpdateError(Exception):
    pass


class UpdateBusy(UpdateError):
    pass


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {} if default is None else default


class OperationLock:
    """进程退出后由操作系统释放；不靠长期残留的PID文件判断锁。"""
    def __init__(self, path):
        self.path, self.file = Path(path), None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        if self.file.tell() == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            self.file = None
            raise UpdateBusy("另一个升级操作正在进行，请稍后重试") from exc
        return self

    def __exit__(self, *args):
        if self.file:
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_UN)
            self.file.close()
            self.file = None


def managed(name):
    parts = PurePosixPath(name).parts
    return bool(parts) and (parts[0] in MANAGED_DIRS or name in MANAGED_FILES) and not any(
        part.startswith(".") or part == "__pycache__" for part in parts
    ) and not name.endswith((".pyc", ".pyo"))


def safe_path(root, name):
    """对 Windows/Linux 均采用严格路径规则，并拒绝已有链接目录。"""
    parts = PurePosixPath(name).parts
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    if not parts or "\\" in name or name.startswith("/") or any(
        p in (".", "..") or ":" in p or p.endswith((" ", ".")) or p.split(".")[0].upper() in reserved
        for p in name.split("/") if p
    ):
        raise UpdateError("升级包包含不安全路径")
    root = Path(root).resolve()
    path = root.joinpath(*parts)
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            raise UpdateError("程序目录含链接，无法在线替换")
    if not path.resolve().is_relative_to(root):
        raise UpdateError("文件路径超出程序目录")
    return path


def package_version(root):
    source = (Path(root) / "app/__init__.py").read_text(encoding="utf-8-sig")
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__version__" for t in node.targets):
            value = ast.literal_eval(node.value)
            Version(value)
            return value
    raise UpdateError("升级包缺少有效版本号")


def validate_lock(root):
    entries = []
    for line in (Path(root) / "requirements.lock").read_text(encoding="utf-8-sig").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        try:
            req = Requirement(line)
        except Exception as exc:
            raise UpdateError("依赖锁文件格式错误") from exc
        if req.url or len(req.specifier) != 1 or any(s.operator != "==" or "*" in s.version for s in req.specifier):
            raise UpdateError("升级依赖必须锁定精确版本，不能使用URL或额外安装参数")
        entries.append(line)
    if not entries:
        raise UpdateError("升级包的依赖锁文件为空")
    return entries


def extract_package(archive, destination, expected_version=None, prefixed=True):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    with zipfile.ZipFile(archive) as z:
        members = z.infolist()
        if len(members) > 6000 or sum(m.file_size for m in members) > MAX_EXPANDED:
            raise UpdateError("升级包解压大小超出限制")
        prefixes = {m.filename.split("/", 1)[0] for m in members}
        if prefixed and len(prefixes) != 1:
            raise UpdateError("升级包必须包含单一项目根目录")
        names = set()
        selected = []
        for member in members:
            # ZipInfo在Windows会把反斜线规范化；必须先检查原始成员名。
            safe_path(destination, member.orig_filename.rstrip("/"))
            safe_path(destination, member.filename.rstrip("/"))
            mode = member.external_attr >> 16
            if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)):
                raise UpdateError("升级包不得包含链接或设备文件")
            name = member.filename.split("/", 1)[1] if prefixed and "/" in member.filename else member.filename
            if member.is_dir() or not managed(name):
                continue
            key = name.casefold()
            if key in names:
                raise UpdateError("升级包包含重复文件名")
            names.add(key)
            selected.append((member, safe_path(destination, name)))
        for member, target in selected:
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(member) as src, target.open("xb") as out:
                shutil.copyfileobj(src, out)
    for required in ("app/main.py", "app/__init__.py", "requirements.lock"):
        if not (destination / required).is_file():
            raise UpdateError("升级包缺少必要程序文件")
    version = package_version(destination)
    if expected_version and Version(version) != Version(expected_version):
        raise UpdateError("升级包版本与Release标签不一致")
    validate_lock(destination)
    return file_hashes(destination)


def file_hashes(root):
    root = Path(root)
    paths = []
    for name in sorted(MANAGED_DIRS | MANAGED_FILES):
        path = safe_path(root, name)
        if path.is_dir():
            paths.extend(path.rglob("*"))
        elif path.exists():
            paths.append(path)
    hashes = {}
    for path in paths:
        name = path.relative_to(root).as_posix()
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raise UpdateError("程序目录含链接，无法备份或替换")
        if path.is_file() and managed(name):
            hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return dict(sorted(hashes.items()))


def apply_files(root, staged, previous_files):
    root, staged = Path(root), Path(staged)
    new = file_hashes(staged)
    # 先验证所有目标，包括待删除的文件；不触碰配置、日志或数据库。
    for name in set(new) | set(previous_files):
        if not managed(name):
            raise UpdateError("文件清单包含非程序文件")
        safe_path(root, name)
    for name in new:
        target = safe_path(root, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=target.parent, prefix=".upgrade-")
        os.close(fd)
        try:
            shutil.copy2(staged / name, temp)
            os.replace(temp, target)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
    for name in set(previous_files) - set(new):
        safe_path(root, name).unlink(missing_ok=True)
    # 删除程序目录内的字节码，避免新旧源码大小/mtime相同导致读到旧代码。
    for directory in MANAGED_DIRS:
        for path in (root / directory).rglob("*.pyc"):
            safe_path(root, path.relative_to(root).as_posix()).unlink()
    if file_hashes(root) != new:
        raise UpdateError("应用后的文件校验不一致")


class RuntimeStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.home = self.root / ".updates"
        self.home.mkdir(exist_ok=True)
        if self.home.is_symlink() or self.home.resolve().parent != self.root:
            raise UpdateError("升级状态目录必须位于当前项目内")
        for name in ("backups", "downloads", "staging", "runtimes"):
            safe_path(self.home, name).mkdir(exist_ok=True)

    def read(self):
        return read_json(self.home / "status.json", {"state":"idle", "message":"尚未检查更新", "progress":0})

    def status(self, state, message, progress=0, **extra):
        data = {**self.read(), "state":state, "message":message, "progress":progress,
                "updated_at":time.time(), **extra}
        atomic_json(self.home / "status.json", data)
        return data

    def lock(self):
        return OperationLock(self.home / "operation.lock")

    def backup(self, python=None):
        name = "backup-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
        folder = self.home / "backups" / name
        folder.mkdir()
        files = file_hashes(self.root)
        with zipfile.ZipFile(folder / "code.zip", "w", zipfile.ZIP_DEFLATED) as z:
            for path in files:
                z.write(self.root / path, path)
        manifest = {"name":name, "version":package_version(self.root), "created_at":time.time(),
                    "files":files, "sha256":hashlib.sha256((folder / "code.zip").read_bytes()).hexdigest(),
                    "python":str(python or sys.executable)}
        atomic_json(folder / "manifest.json", manifest)
        return manifest

    def backups(self):
        result = []
        for path in sorted((self.home / "backups").glob("backup-*/manifest.json"), reverse=True):
            try:
                item = read_json(path)
                result.append({k:item[k] for k in ("name", "version", "created_at")})
            except (OSError, ValueError, KeyError):
                continue
        return result

    def backup_manifest(self, name):
        if not re.fullmatch(r"backup-\d{8}-\d{6}-[0-9a-f]{8}", name):
            raise UpdateError("备份名称无效")
        folder = safe_path(self.home, "backups/" + name)
        manifest = read_json(folder / "manifest.json")
        if not manifest or hashlib.sha256((folder / "code.zip").read_bytes()).hexdigest() != manifest["sha256"]:
            raise UpdateError("备份不存在或完整性校验失败")
        return manifest

    def restore(self, name, changed_files):
        manifest = self.backup_manifest(name)
        stage = self.home / "staging" / ("restore-" + uuid.uuid4().hex)
        files = extract_package(self.home / "backups" / name / "code.zip", stage, manifest["version"], prefixed=False)
        if files != manifest["files"]:
            raise UpdateError("备份内容与清单不一致")
        apply_files(self.root, stage, changed_files)
        return manifest

    def ensure_idle(self):
        if (self.home / "transaction.json").exists():
            raise UpdateBusy("存在未恢复的升级事务，请重新启动管理器完成恢复")
        if (self.home / "command.json").exists() or self.read().get("state") in BUSY:
            raise UpdateBusy("已有升级操作正在进行")


def quiet_process_options():
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
