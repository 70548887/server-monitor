"""固定GitHub仓库的版本检查、校验下载、备份和升级任务入口。"""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlsplit, unquote
import uuid

import httpx
from packaging.version import Version, InvalidVersion

from app import __version__
from .update_runtime import (
    RuntimeStore, UpdateError, UpdateBusy, MAX_PACKAGE, BUSY,
    atomic_json, read_json, extract_package, file_hashes, package_version,
)

REPOSITORY = "70548887/server-monitor"
API_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
HOSTS = {"api.github.com", "github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}


def trusted_url(url):
    try:
        parsed = urlsplit(url)
        return parsed.scheme == "https" and parsed.hostname in HOSTS and parsed.port in (None, 443) and not parsed.username and not parsed.password
    except ValueError:
        return False


def release_info(data, current):
    tag = data.get("tag_name", "")
    if not re.fullmatch(r"v\d+\.\d+\.\d+", tag) or data.get("draft") or data.get("prerelease"):
        raise UpdateError("最新Release不是有效的稳定版本")
    version = str(Version(tag[1:]))
    asset_name = f"server-monitor-{tag}.zip"
    assets = data.get("assets", [])
    def pick(name):
        matches = [a for a in assets if a.get("name") == name]
        if len(matches) != 1:
            raise UpdateError(f"Release缺少唯一的 {name}")
        a = matches[0]
        url = a.get("browser_download_url", "")
        expected = f"/{REPOSITORY}/releases/download/{tag}/{name}"
        if not trusted_url(url) or urlsplit(url).hostname != "github.com" or unquote(urlsplit(url).path) != expected:
            raise UpdateError("Release资产地址不属于本项目")
        return {"name":name, "url":url, "size":int(a.get("size", 0))}
    archive, checksum = pick(asset_name), pick("SHA256SUMS.txt")
    if not 0 < archive["size"] <= MAX_PACKAGE or not 0 < checksum["size"] <= 65536:
        raise UpdateError("升级资产大小无效")
    return {"version":version, "tag":tag, "has_update":Version(version) > Version(current),
            "name":str(data.get("name") or tag)[:200], "notes":str(data.get("body") or "")[:30000],
            "published_at":data.get("published_at"), "url":f"https://github.com/{REPOSITORY}/releases/tag/{tag}",
            "archive":archive, "checksum":checksum}


def checksum_for(text, filename):
    matches = []
    for line in text.splitlines():
        match = re.fullmatch(r"([a-fA-F0-9]{64})\s+\*?(.+)", line.strip())
        if match and match[2] == filename:
            matches.append(match[1].lower())
    if len(matches) != 1:
        raise UpdateError("校验清单缺少与升级包精确匹配的SHA256")
    return matches[0]


class UpdateService:
    def __init__(self, root=None, version=None, transport=None):
        self.store = RuntimeStore(root or Path(__file__).resolve().parents[2])
        self.version = version or __version__
        self.transport = transport
        self.task = None
        self._last_check = 0

    def capability(self):
        manager = read_json(self.store.home / "manager.json")
        managed = bool(os.getenv("MONITOR_UPDATE_SESSION")) and manager.get("session") == os.getenv("MONITOR_UPDATE_SESSION") and time.time()-manager.get("heartbeat", 0) < 10
        container = os.getenv("MONITOR_DEPLOYMENT") == "docker" or Path("/.dockerenv").exists()
        writable = os.access(self.store.root / "app", os.W_OK) and os.access(self.store.root / "scripts", os.W_OK)
        supported = managed and writable and not container
        if container:
            reason = "容器部署请更新镜像并重建容器；这里仍可检查版本和查看更新说明。"
        elif not managed:
            reason = "在线应用和重启需要用 python scripts/run_server.py 启动服务。"
        elif not writable:
            reason = "当前服务用户没有程序目录写入权限。"
        else:
            reason = "升级前自动备份程序，保留配置与数据；健康检查失败自动恢复程序和运行环境。"
        return {"can_apply":supported, "managed":managed, "reason":reason}

    def status(self):
        value = self.store.read()
        # 磁盘事务状态可跨重启读取。进程内任务异常退出留下的状态允许重新操作。
        if value.get("state") in {"checking", "downloading", "backing_up"}:
            try:
                with self.store.lock():
                    value = self.store.status("error", "上一次操作被中断，可以重新检查或下载")
            except UpdateBusy:
                pass
        if value.get("release"):
            value["release"]["has_update"] = Version(value["release"]["version"]) > Version(self.version)
        return {**value, "current_version":self.version, "repository":REPOSITORY,
                "capability":self.capability(), "backups":self.store.backups()}

    def audit(self, action, actor, result):
        with (self.store.home / "history.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"time":time.time(), "action":action, "actor":str(actor)[:100], "result":result}, ensure_ascii=False) + "\n")

    async def _fetch(self, url, maximum, destination=None, progress=False):
        """逐跳验证URL；令牌只发到GitHub API，不转发到资产CDN。"""
        async with httpx.AsyncClient(timeout=httpx.Timeout(40, connect=10), transport=self.transport, follow_redirects=False) as client:
            for _ in range(6):
                if not trusted_url(url):
                    raise UpdateError("下载被重定向到非GitHub地址")
                headers = {"Accept":"application/vnd.github+json" if urlsplit(url).hostname == "api.github.com" else "application/octet-stream",
                           "User-Agent":"server-monitor-updater"}
                token = os.getenv("GITHUB_TOKEN")
                if token and urlsplit(url).hostname == "api.github.com":
                    headers["Authorization"] = "Bearer " + token
                async with client.stream("GET", url, headers=headers) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        from urllib.parse import urljoin
                        url = urljoin(url, response.headers.get("location", ""))
                        continue
                    if response.status_code in (403, 429):
                        raise UpdateError("GitHub访问受限，请稍后重试或配置GITHUB_TOKEN")
                    if response.status_code == 404:
                        raise UpdateError("未找到正式Release或对应升级包")
                    if response.status_code != 200:
                        raise UpdateError(f"GitHub返回HTTP {response.status_code}")
                    expected = int(response.headers.get("content-length", 0))
                    if expected > maximum:
                        raise UpdateError("下载文件大小超出限制")
                    size, chunks, last_write = 0, [], 0
                    output = Path(destination).open("wb") if destination else None
                    try:
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > maximum:
                                raise UpdateError("下载文件大小超出限制")
                            if output:
                                output.write(chunk)
                            else:
                                chunks.append(chunk)
                            if progress and time.monotonic()-last_write > .4:
                                self.store.status("downloading", "正在下载升级包", min(85, int(size / max(expected, maximum) * 85)), downloaded_bytes=size, total_bytes=expected)
                                last_write = time.monotonic()
                    finally:
                        if output:
                            output.close()
                    return b"".join(chunks) if not destination else size
            raise UpdateError("下载重定向次数过多")

    async def check(self):
        raw = await self._fetch(API_URL, 1024 * 1024)
        try:
            release = release_info(json.loads(raw), self.version)
        except (ValueError, TypeError, KeyError) as exc:
            raise UpdateError("GitHub版本信息格式无效") from exc
        self._last_check = time.monotonic()
        self.store.status("available" if release["has_update"] else "up_to_date",
                          "发现可用更新" if release["has_update"] else "当前已是最新版本", 100,
                          release=release, staged=None, error=None)

    async def download(self):
        (self.store.home / "staged.json").unlink(missing_ok=True)
        # 重新从固定Release源解析，客户端不得传入下载URL或校验值。
        release = release_info(json.loads(await self._fetch(API_URL, 1024 * 1024)), self.version)
        if not release["has_update"]:
            raise UpdateError("没有高于当前版本的正式更新")
        self.store.status("downloading", "正在获取校验清单", release=release, staged=None, error=None)
        expected = checksum_for((await self._fetch(release["checksum"]["url"], 65536)).decode("utf-8-sig"), release["archive"]["name"])
        ident = uuid.uuid4().hex
        archive = self.store.home / "downloads" / (ident + ".zip")
        stage = self.store.home / "staging" / ident
        try:
            size = await self._fetch(release["archive"]["url"], MAX_PACKAGE, archive, True)
            if size != release["archive"]["size"] or hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
                raise UpdateError("升级包SHA256或大小校验失败")
            self.store.status("downloading", "正在校验版本和程序文件", 90)
            files = await asyncio.to_thread(extract_package, archive, stage, release["version"])
            staged = {"id":ident, "version":release["version"], "sha256":expected, "files":files}
            atomic_json(self.store.home / "staged.json", staged)
            self.store.status("ready", "升级包已校验，可以应用更新", 100, staged={"id":ident, "version":release["version"]})
        except Exception:
            archive.unlink(missing_ok=True)
            raise

    async def start(self, action, actor="admin"):
        if action not in ("check", "download", "backup"):
            raise UpdateError("未知操作")
        self.status()  # 恢复没有活跃锁的中断状态。
        lock = self.store.lock()
        lock.__enter__()
        try:
            self.store.ensure_idle()
            if action == "check" and time.monotonic()-self._last_check < 30:
                return self.status()
            self.store.status({"check":"checking", "download":"downloading", "backup":"backing_up"}[action],
                              {"check":"正在检查GitHub版本", "download":"正在准备下载", "backup":"正在备份程序文件"}[action], 0, error=None)
            self.audit(action, actor, "started")
            active_lock = lock
            async def work():
                try:
                    if action == "check":
                        await self.check()
                    elif action == "download":
                        await self.download()
                    else:
                        backup = await asyncio.to_thread(self.store.backup)
                        staged = read_json(self.store.home / "staged.json")
                        next_state = "ready" if staged and Version(staged["version"]) > Version(self.version) else "idle"
                        self.store.status(next_state, "程序备份已创建", 100, last_backup=backup["name"])
                    self.audit(action, actor, "completed")
                except Exception as exc:
                    message = str(exc) if isinstance(exc, UpdateError) else f"操作失败（{type(exc).__name__}），请检查网络和运行环境后重试"
                    self.store.status("error", message, error=message)
                    self.audit(action, actor, "failed")
                finally:
                    active_lock.__exit__(None, None, None)
            self.task = asyncio.create_task(work())
            lock = None
            return self.status()
        finally:
            if lock:
                lock.__exit__(None, None, None)

    def request_operation(self, action, actor, backup=None):
        if action not in ("apply", "rollback", "restart"):
            raise UpdateError("未知操作")
        with self.store.lock():
            self.store.ensure_idle()
            capability = self.capability()
            if not capability["can_apply"]:
                raise UpdateError(capability["reason"])
            if package_version(self.store.root) != self.version:
                raise UpdateError("磁盘代码与运行版本不一致，请先重启")
            if action == "apply":
                staged = read_json(self.store.home / "staged.json")
                if self.store.read().get("state") != "ready" or not staged or Version(staged["version"]) <= Version(self.version):
                    raise UpdateError("请先下载并校验新版本")
            if action == "rollback":
                self.store.backup_manifest(backup or "")
            command = {"id":uuid.uuid4().hex, "action":action, "backup":backup,
                       "actor":str(actor)[:100], "session":os.environ["MONITOR_UPDATE_SESSION"]}
            self.audit(action, actor, "queued")
            self.store.status("queued", "任务已提交，正在准备" if action != "restart" else "正在准备重启", operation_id=command["id"], error=None)
            atomic_json(self.store.home / "command.json", command)
        return self.status()


_service = None


def get_update_service():
    global _service
    if _service is None:
        _service = UpdateService()
    return _service
