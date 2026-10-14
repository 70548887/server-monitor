"""系统升级专用网络配置；带认证的代理地址只保存在私有状态文件中。"""
import ipaddress
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import unquote, urlsplit

from .update_runtime import UpdateError

_INVALID_URL = "代理地址无效，请填写 HTTP/HTTPS 代理地址（可包含用户名和密码）"
_INVALID_CONFIG = "升级代理配置损坏或不可读取，请重新保存代理设置"


def validate_proxy_url(url):
    """校验连接端点，不在异常里回显任何用户输入。"""
    try:
        if not isinstance(url, str) or not url or len(url) > 4096:
            raise ValueError
        if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in url):
            raise ValueError
        if "\\" in url or "?" in url or "#" in url:
            raise ValueError
        parsed = urlsplit(url)
        host = parsed.hostname
        if parsed.scheme not in ("http", "https") or not host or parsed.path not in ("", "/"):
            raise ValueError
        if parsed.netloc.count("@") > 1 or parsed.port == 0 or parsed.netloc.endswith(":"):
            raise ValueError
        # 拒绝不可打印/无效转义；认证中的保留字符可用百分号编码。
        if re.search(r"%(?![0-9a-fA-F]{2})", url):
            raise ValueError
        for value in (parsed.username, parsed.password):
            if value is not None and any(ord(c) < 32 or ord(c) == 127 for c in unquote(value)):
                raise ValueError
        if "@" in parsed.netloc and not parsed.username:
            raise ValueError
        if ":" in host:
            ipaddress.IPv6Address(host)
        else:
            ascii_host = host.encode("idna").decode("ascii").rstrip(".")
            if len(ascii_host) > 253 or not all(
                re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", part)
                for part in ascii_host.split(".")
            ):
                raise ValueError
            if re.fullmatch(r"[0-9.]+", ascii_host):
                ipaddress.IPv4Address(ascii_host)
        return url
    except (ValueError, UnicodeError, TypeError):
        raise UpdateError(_INVALID_URL) from None


def _config_path(root):
    root = Path(root).resolve()
    home = root / ".updates"
    path = home / "proxy.json"
    if home.is_symlink() or home.resolve().parent != root or path.is_symlink():
        raise UpdateError(_INVALID_CONFIG)
    return path


def load_update_proxy(root):
    """缺省直连；存在但无效的配置必须显式失败，避免意外绕开代理。"""
    try:
        path = _config_path(root)
        if not path.exists():
            return {"enabled": False, "url": ""}
        if path.stat().st_size > 16384:
            raise ValueError
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or type(data.get("enabled")) is not bool or not isinstance(data.get("url"), str):
            raise ValueError
        url = data["url"]
        if url:
            validate_proxy_url(url)
        elif data["enabled"]:
            raise ValueError
        return {"enabled": data["enabled"], "url": url}
    except (OSError, ValueError, TypeError, UpdateError):
        raise UpdateError(_INVALID_CONFIG) from None


def proxy_status(root):
    """页面只能取得代理端点；用户名和密码均不回传。"""
    config = load_update_proxy(root)
    parsed = urlsplit(config["url"])
    host = parsed.hostname or ""
    if ":" in host:
        host = "[" + host + "]"
    endpoint = host + (":" + str(parsed.port) if parsed.port is not None else "")
    return {"enabled": config["enabled"], "configured": bool(config["url"]),
            "display_url": f"{parsed.scheme}://{endpoint}" if endpoint else "",
            "has_auth": parsed.username is not None or parsed.password is not None}


def save_update_proxy(root, enabled, url=None):
    """None 保留已有地址；关闭时可保存空字符串来清除地址。"""
    if type(enabled) is not bool or (url is not None and not isinstance(url, str)):
        raise UpdateError("代理配置格式无效")
    if url is None:
        url = load_update_proxy(root)["url"]
    if url:
        validate_proxy_url(url)
    elif enabled:
        raise UpdateError("启用代理前请填写代理地址")
    temporary = None
    try:
        path = _config_path(root)
        path.parent.mkdir(exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".proxy-", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            if os.name != "nt":
                os.fchmod(stream.fileno(), 0o600)
            json.dump({"enabled": enabled, "url": url}, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError:
        raise UpdateError("代理设置保存失败，请检查升级目录权限") from None
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)
    return proxy_status(root)
