"""升级代理配置、认证隔离、出站路由及错误脱敏回归；不访问外网。"""
import asyncio
import json
import os
import stat
import time
import zipfile

import httpx
import pytest

from app.services.update_network import load_update_proxy, proxy_status, save_update_proxy
from app.services.update_runtime import UpdateBusy, UpdateError
from app.services.updates import API_URL, UpdateService
from test_updates import service, finish

PROXY = "http://fixture-user:fixture-password@proxy.example.test:3128"


def test_private_proxy_survives_restart_without_exposing_credentials(tmp_path):
    svc, _ = service(tmp_path)
    assert svc.status()["proxy"] == {"enabled": False, "configured": False, "display_url": "", "has_auth": False}
    svc._last_check = time.monotonic()
    status = svc.save_proxy(True, PROXY, "test-admin")
    assert svc._last_check == 0
    assert status["proxy"] == {"enabled": True, "configured": True,
                                "display_url": "http://proxy.example.test:3128", "has_auth": True}
    restarted = UpdateService(svc.store.root)
    assert restarted.status()["proxy"] == status["proxy"]
    assert load_update_proxy(svc.store.root) == {"enabled": True, "url": PROXY}
    private_path = svc.store.home / "proxy.json"
    assert json.loads(private_path.read_text())["url"] == PROXY
    assert "fixture-user" not in json.dumps(status)
    assert "fixture-password" not in json.dumps(status)
    for path in svc.store.home.glob("*.json*"):
        if path != private_path:
            assert "fixture-user" not in path.read_text(encoding="utf-8")
            assert "fixture-password" not in path.read_text(encoding="utf-8")
    backup = svc.store.backup()
    with zipfile.ZipFile(svc.store.home / "backups" / backup["name"] / "code.zip") as archive:
        assert ".updates/proxy.json" not in archive.namelist()
        assert all(b"fixture-password" not in archive.read(name) for name in archive.namelist())


def test_disable_reenable_and_clear_proxy(tmp_path):
    save_update_proxy(tmp_path, True, PROXY)
    save_update_proxy(tmp_path, False)
    assert load_update_proxy(tmp_path) == {"enabled": False, "url": PROXY}
    save_update_proxy(tmp_path, True)
    assert proxy_status(tmp_path)["enabled"]
    with pytest.raises(UpdateError):
        save_update_proxy(tmp_path, True, "")
    assert load_update_proxy(tmp_path)["url"] == PROXY
    save_update_proxy(tmp_path, False, "")
    assert load_update_proxy(tmp_path) == {"enabled": False, "url": ""}
    with pytest.raises(UpdateError):
        save_update_proxy(tmp_path, True)


@pytest.mark.parametrize("url", [
    "socks5://proxy.example.test:1080", "ftp://proxy.example.test", "http:///missing",
    "http://host:0", "http://host:65536", "http://host:not-a-port", "http://host:",
    "http://host/path", "http://host?", "http://host?token=fixture-password", "http://host#secret",
    "http://fixture-user:fixture-password@host\\evil", "http://bad host:8080",
    "http://host\n", "http://fixture-user:p%0Dass@host", "http://fixture-user:p%ZZ@host",
    "http://user:pass@other@host", "http://:password@host", "http://-invalid.test:80",
    "http://999.1.1.1", "http://[invalid]:123", "not a proxy",
])
def test_invalid_urls_never_replace_valid_configuration(tmp_path, url):
    save_update_proxy(tmp_path, True, PROXY)
    with pytest.raises(UpdateError) as error:
        save_update_proxy(tmp_path, True, url)
    assert "fixture-password" not in str(error.value)
    assert url not in str(error.value)
    assert load_update_proxy(tmp_path)["url"] == PROXY


@pytest.mark.parametrize("url,display", [
    ("https://user:p%40ss@proxy.example.test:443/", "https://proxy.example.test:443"),
    ("http://[2001:db8::1]:3128", "http://[2001:db8::1]:3128"),
    ("http://127.0.0.1:8080", "http://127.0.0.1:8080"),
    ("http://localhost", "http://localhost"),
])
def test_valid_proxy_endpoints(tmp_path, url, display):
    save_update_proxy(tmp_path, True, url)
    assert proxy_status(tmp_path)["display_url"] == display


@pytest.mark.parametrize("broken", ["not-json fixture-password", "[]", '{"enabled":true,"url":""}', '{"enabled":"yes","url":"http://host"}'])
def test_corrupt_proxy_fails_closed_and_can_be_replaced(tmp_path, broken):
    svc, _ = service(tmp_path)
    (svc.store.home / "proxy.json").write_text(broken, encoding="utf-8")
    for operation in (lambda: load_update_proxy(svc.store.root), svc.status):
        with pytest.raises(UpdateError) as error:
            operation()
        assert "fixture-password" not in str(error.value)
    svc.save_proxy(True, PROXY)
    assert svc.status()["proxy"]["enabled"]
    (svc.store.home / "proxy.json").write_text(broken, encoding="utf-8")
    svc.save_proxy(False, "")
    assert not svc.status()["proxy"]["configured"]


@pytest.mark.skipif(os.name == "nt", reason="Unix 文件权限检查")
def test_proxy_file_is_owner_read_write_only(tmp_path):
    save_update_proxy(tmp_path, True, PROXY)
    path = tmp_path / ".updates/proxy.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path.chmod(0o644)
    save_update_proxy(tmp_path, False)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_proxy_write_is_atomic_on_replace_failure(tmp_path, monkeypatch):
    save_update_proxy(tmp_path, True, PROXY)
    import app.services.update_network as network
    def fail_replace(*args):
        raise OSError("fixture-password")
    monkeypatch.setattr(network.os, "replace", fail_replace)
    with pytest.raises(UpdateError) as error:
        save_update_proxy(tmp_path, False, "")
    assert "fixture-password" not in str(error.value)
    assert load_update_proxy(tmp_path)["enabled"]
    assert not list((tmp_path / ".updates").glob(".proxy-*"))


async def test_proxy_cannot_change_while_update_is_running(tmp_path):
    svc, _ = service(tmp_path)
    started, release = asyncio.Event(), asyncio.Event()
    async def check():
        started.set()
        await release.wait()
        svc.store.status("up_to_date", "done", 100)
    svc.check = check
    await svc.start("check")
    await started.wait()
    try:
        with pytest.raises(UpdateBusy):
            svc.save_proxy(True, PROXY)
        assert not (svc.store.home / "proxy.json").exists()
    finally:
        release.set()
        await svc.task
    svc.store.status("queued", "queued")
    with pytest.raises(UpdateBusy):
        svc.save_proxy(True, PROXY)


async def test_proxy_applies_to_metadata_checksum_archive_and_redirects(tmp_path, monkeypatch):
    svc, seen = service(tmp_path)
    svc.save_proxy(True, PROXY)
    captured = []
    original = httpx.AsyncClient
    def client_factory(**kwargs):
        captured.append(kwargs)
        return original(**kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    result = await finish(svc, "download")
    assert result["state"] == "ready"
    assert len(captured) == 3
    assert all(options["proxy"] == PROXY and options["trust_env"] is False for options in captured)
    assert all(options["follow_redirects"] is False for options in captured)
    assert len(seen) == 3
    assert all("proxy-authorization" not in request.headers for request in seen)
    redirected = []
    monkeypatch.setenv("GITHUB_TOKEN", "fixture-token")
    def redirect(request):
        redirected.append(request)
        if request.url.host == "api.github.com":
            return httpx.Response(302, headers={"location": "https://release-assets.githubusercontent.com/asset"})
        return httpx.Response(200, content=b"ok")
    svc.transport = httpx.MockTransport(redirect)
    assert await svc._fetch(API_URL, 100) == b"ok"
    assert redirected[0].headers["authorization"] == "Bearer fixture-token"
    assert "authorization" not in redirected[1].headers
    assert "proxy-authorization" not in redirected[1].headers


async def test_disabled_proxy_uses_explicit_direct_connection(tmp_path, monkeypatch):
    svc, _ = service(tmp_path)
    svc.save_proxy(False, PROXY)
    monkeypatch.setenv("HTTPS_PROXY", "http://environment.invalid:1111")
    captured = []
    original = httpx.AsyncClient
    def client_factory(**kwargs):
        captured.append(kwargs)
        return original(**kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    assert (await finish(svc, "check"))["state"] == "available"
    assert captured[0]["proxy"] is None and captured[0]["trust_env"] is False


@pytest.mark.parametrize("error_type", [httpx.ProxyError, httpx.ConnectError, httpx.ReadTimeout])
async def test_proxy_errors_are_redacted_in_status_and_audit(tmp_path, error_type):
    svc, _ = service(tmp_path)
    svc.save_proxy(True, PROXY)
    def fail(request):
        raise error_type("failed with " + PROXY)
    svc.transport = httpx.MockTransport(fail)
    result = await finish(svc, "check")
    assert result["state"] == "error" and "代理连接失败" in result["error"]
    for path in (svc.store.home / "status.json", svc.store.home / "history.jsonl"):
        assert "fixture-password" not in path.read_text(encoding="utf-8")
        assert "fixture-user" not in path.read_text(encoding="utf-8")


async def test_proxy_authentication_error_is_readable(tmp_path):
    svc, _ = service(tmp_path)
    svc.save_proxy(True, PROXY)
    svc.transport = httpx.MockTransport(lambda request: httpx.Response(407))
    with pytest.raises(UpdateError, match="代理认证失败"):
        await svc._fetch(API_URL, 100)


async def test_proxy_route_requires_auth_origin_and_header(tmp_path, monkeypatch):
    from test_routes import make_client, make_authed_client
    from app.routes import updates
    svc, _ = service(tmp_path)
    monkeypatch.setattr(updates, "get_update_service", lambda: svc)
    url = "/api/system/update/proxy"
    payload = {"enabled": True, "url": PROXY}
    async with make_client() as client:
        assert (await client.post(url, json=payload)).status_code == 401
    async with make_authed_client(monkeypatch) as client:
        assert (await client.post(url, json=payload)).status_code == 403
        assert (await client.post(url, json=payload, headers={"X-Update-Request": "1", "Origin": "https://elsewhere.invalid"})).status_code == 403
        response = await client.post(url, json=payload, headers={"X-Update-Request": "1"})
        assert response.status_code == 200
        assert response.json()["proxy"]["enabled"]
        assert "fixture-user" not in response.text and "fixture-password" not in response.text
        with svc.store.lock():
            response = await client.post(url, json=payload, headers={"X-Update-Request": "1"})
            assert response.status_code == 409


@pytest.mark.parametrize("payload", [
    {"enabled": "yes", "url": PROXY}, {"enabled": True, "url": [PROXY]},
    {"enabled": True, "url": PROXY, "unexpected": PROXY}, {"enabled": True, "url": "ftp://fixture-user:fixture-password@host"},
])
async def test_invalid_api_payload_does_not_echo_credentials(tmp_path, monkeypatch, payload):
    from test_routes import make_authed_client
    from app.routes import updates
    svc, _ = service(tmp_path)
    monkeypatch.setattr(updates, "get_update_service", lambda: svc)
    async with make_authed_client(monkeypatch) as client:
        response = await client.post("/api/system/update/proxy", json=payload, headers={"X-Update-Request": "1"})
        assert response.status_code == 400
        assert "fixture-user" not in response.text and "fixture-password" not in response.text
        response = await client.post("/api/system/update/proxy", content='{"url":"fixture-password"', headers={"X-Update-Request": "1"})
        assert response.status_code == 400 and "fixture-password" not in response.text
