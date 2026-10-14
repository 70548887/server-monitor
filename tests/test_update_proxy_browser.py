"""更新代理表单回归：真实管理员 API，临时配置文件，外网请求由 MockTransport 接管。"""

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from test_login_dialog import login_browser
from test_updates import service


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_BROWSER_TESTS") != "1", reason="显式启用浏览器验收"
)

PROXY_URL = "http://test-user:test-secret@127.0.0.1:8888"
DISPLAY_URL = "http://127.0.0.1:8888"


@pytest.fixture
async def proxy_browser(login_browser, tmp_path, monkeypatch):
    from app.routes import updates
    from app.services.auth import AuthService

    page, url, _, _, errors = login_browser
    svc, requests = service(tmp_path)
    monkeypatch.setattr(updates, "get_update_service", lambda: svc)
    monkeypatch.setattr(AuthService, "get_session", AsyncMock(return_value={
        "admin_id": 1, "username": "test-admin",
    }))
    monkeypatch.setattr(AuthService, "get_admin_by_id", AsyncMock(return_value=SimpleNamespace(
        username="test-admin", email=None, is_active=True,
        created_at=None, last_login=None,
    )))
    await page.context.add_cookies([{
        "name": "session_id", "value": "fixture-session", "url": url,
    }])
    await page.route("**/healthz", lambda route: route.fulfill(
        status=200, json={"status": "ok"},
    ))
    page.on("dialog", lambda dialog: dialog.accept())
    yield page, url, svc, requests, errors
    if svc.task and not svc.task.done():
        svc.task.cancel()
        try:
            await svc.task
        except asyncio.CancelledError:
            pass


async def open_proxy_settings(page, url):
    from playwright.async_api import expect

    await page.goto(url + "/settings?tab=updates")
    await expect(page.locator("#updateCurrent")).to_have_text("v1.0.0")
    await expect(page.locator("#updateProxyForm")).to_be_visible()
    await expect(page.locator("#updateProxyMessage")).to_have_attribute("role", "status")


async def submit_proxy(page, button="#updateProxySave"):
    async with page.expect_response(lambda response:
        response.url.endswith("/api/system/update/proxy") and
        response.request.method == "POST"
    ) as pending:
        await page.locator(button).click()
    response = await pending.value
    data = await response.json()
    serialized = json.dumps(data)
    assert "test-user" not in serialized
    assert "test-secret" not in serialized
    return response, data


async def save_proxy(page, value=PROXY_URL):
    from playwright.async_api import expect

    await page.locator("#updateProxyEnabled").check()
    await page.locator("#updateProxyUrl").fill(value)
    response, data = await submit_proxy(page)
    assert response.status == 200
    await expect(page.locator("#updateProxyUrl")).to_have_value("")
    await expect(page.locator("#updateProxySave")).to_be_enabled()
    return data


async def test_proxy_save_reload_disable_and_clear_preserve_credentials_privately(proxy_browser):
    from playwright.async_api import expect

    page, url, svc, _, errors = proxy_browser
    await open_proxy_settings(page, url)
    field = page.locator("#updateProxyUrl")
    await expect(field).to_have_attribute("type", "password")
    await expect(page.locator("#updateProxyEnabled")).not_to_be_checked()
    await save_proxy(page)
    proxy = svc.status()["proxy"]
    assert proxy == {
        "enabled": True, "configured": True,
        "display_url": DISPLAY_URL, "has_auth": True,
    }
    assert "test-user" not in await page.content()
    assert "test-secret" not in await page.content()
    await page.reload()
    await expect(page.locator("#updateProxyEnabled")).to_be_checked()
    await expect(field).to_have_value("")
    assert "已保存" in await field.get_attribute("placeholder")
    await expect(page.locator("#updateProxyForm")).to_contain_text(DISPLAY_URL)
    status_response = await page.request.get(url + "/api/system/update/status")
    assert status_response.status == 200
    status_text = await status_response.text()
    assert "test-user" not in status_text and "test-secret" not in status_text

    await page.locator("#updateProxyEnabled").uncheck()
    response, _ = await submit_proxy(page)
    assert response.status == 200
    assert "url" not in response.request.post_data_json
    assert svc.status()["proxy"] == {**proxy, "enabled": False}
    await page.reload()
    await expect(page.locator("#updateProxyEnabled")).not_to_be_checked()
    await expect(page.locator("#updateProxyClear")).to_be_enabled()
    response, _ = await submit_proxy(page, "#updateProxyClear")
    assert response.status == 200
    assert response.request.post_data_json["url"] == ""
    assert not svc.status()["proxy"]["configured"]
    assert not svc.status()["proxy"]["enabled"]
    await expect(field).to_have_value("")
    await page.reload()
    await expect(page.locator("#updateProxyEnabled")).not_to_be_checked()
    await expect(page.locator("#updateProxyForm")).not_to_contain_text(DISPLAY_URL)
    assert not errors


async def test_proxy_invalid_url_is_explained_and_can_be_corrected(proxy_browser):
    from playwright.async_api import expect

    page, url, svc, _, errors = proxy_browser
    await open_proxy_settings(page, url)
    await page.locator("#updateProxyEnabled").check()
    await page.locator("#updateProxyUrl").fill("this is not a proxy URL")
    response, data = await submit_proxy(page)
    assert response.status == 400
    assert isinstance(data.get("detail"), str)
    await expect(page.locator("#updateProxyMessage")).to_contain_text(data["detail"])
    await expect(page.locator("#updateProxySave")).to_be_enabled()
    await expect(page.locator("#updateProxyUrl")).to_have_value("this is not a proxy URL")
    assert not svc.status()["proxy"]["configured"]
    await save_proxy(page)
    assert svc.status()["proxy"]["enabled"]
    assert not errors


async def test_saved_proxy_can_check_release_without_real_network(proxy_browser):
    from playwright.async_api import expect
    from app.services.updates import API_URL

    page, url, svc, requests, errors = proxy_browser
    await open_proxy_settings(page, url)
    await save_proxy(page)
    await page.locator("#updateCheck").click()
    await expect(page.locator("#updateLatest")).to_have_text("v1.1.0", timeout=8000)
    assert svc.status()["state"] == "available"
    assert any(str(request.url) == API_URL for request in requests)
    await expect(page.locator("#updateProxyEnabled")).to_be_checked()
    await expect(page.locator("#updateProxySave")).to_be_enabled()
    await expect(page.locator("#updateProxyUrl")).to_have_value("")
    await page.set_viewport_size({"width": 1200, "height": 1000})
    await page.locator("#systemUpdate").evaluate("element => element.scrollIntoView({block: 'start'})")
    preview = Path("logs/update-proxy-20261014/preview.png")
    preview.parent.mkdir(parents=True, exist_ok=True)
    await page.screenshot(path=str(preview), animations="disabled")
    assert not errors


async def test_status_poll_keeps_unsaved_proxy_edits_and_disables_busy_save(proxy_browser, monkeypatch):
    from playwright.async_api import expect

    page, url, svc, _, errors = proxy_browser
    await open_proxy_settings(page, url)
    await save_proxy(page)
    draft = "http://draft-user:draft-secret@127.0.0.1:9999"
    await page.locator("#updateProxyUrl").fill(draft)
    await page.locator("#updateProxyEnabled").uncheck()
    started, release = asyncio.Event(), asyncio.Event()
    original_check = svc.check

    async def delayed_check():
        started.set()
        await release.wait()
        await original_check()

    monkeypatch.setattr(svc, "check", delayed_check)
    try:
        async with page.expect_response(lambda response:
            response.url.endswith("/api/system/update/status"), timeout=8000
        ) as pending:
            await page.locator("#updateCheck").click()
            await asyncio.wait_for(started.wait(), 5)
            await expect(page.locator("#updateProxySave")).to_be_disabled()
        assert (await pending.value).status == 200
        await expect(page.locator("#updateProxyUrl")).to_have_value(draft)
        await expect(page.locator("#updateProxyEnabled")).not_to_be_checked()
        assert svc.status()["proxy"]["enabled"]
    finally:
        release.set()
        if svc.task:
            await svc.task
    await expect(page.locator("#updateLatest")).to_have_text("v1.1.0", timeout=8000)
    await expect(page.locator("#updateProxySave")).to_be_enabled()
    await expect(page.locator("#updateProxyUrl")).to_have_value(draft)
    await expect(page.locator("#updateProxyEnabled")).not_to_be_checked()
    assert not errors


@pytest.mark.parametrize("width", [320, 375])
async def test_proxy_form_fits_mobile_width(proxy_browser, width):
    from playwright.async_api import expect

    page, url, _, _, errors = proxy_browser
    await page.set_viewport_size({"width": width, "height": 812})
    await open_proxy_settings(page, url)
    await save_proxy(page)
    await expect(page.locator("#updateProxyForm")).to_contain_text(DISPLAY_URL)
    for selector in (".settings-page", "#updateProxyForm", "#updateProxyUrl", "#updateProxySave", "#updateProxyClear", "#updateProxyVisibility"):
        size = await page.locator(selector).evaluate("""element => ({
            left: element.getBoundingClientRect().left,
            right: element.getBoundingClientRect().right,
            scroll: element.scrollWidth,
            client: element.clientWidth,
            viewport: innerWidth
        })""")
        assert size["left"] >= -1, (selector, size)
        assert size["right"] <= size["viewport"] + 1, (selector, size)
        if selector != "#updateProxyUrl":
            assert size["scroll"] <= size["client"] + 1, (selector, size)
    assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert not errors


async def test_proxy_visibility_toggle_and_save_clear_reset(proxy_browser):
    from playwright.async_api import expect

    page, url, svc, _, errors = proxy_browser
    await open_proxy_settings(page, url)
    field = page.locator("#updateProxyUrl")
    toggle = page.locator("#updateProxyVisibility")
    posts = []
    page.on("request", lambda request: posts.append(request.url)
            if request.method == "POST" and request.url.endswith("/proxy") else None)
    await expect(field).to_have_attribute("type", "password")
    await expect(toggle).to_have_attribute("aria-pressed", "false")
    await expect(toggle).to_have_attribute("type", "button")
    await toggle.click()
    await expect(field).to_have_attribute("type", "text")
    await expect(page.locator("#updateProxyMessage")).not_to_contain_text("修改尚未保存")
    await field.fill(PROXY_URL)
    await toggle.focus()
    await page.keyboard.press("Enter")
    await expect(field).to_have_attribute("type", "password")
    await expect(field).to_have_value(PROXY_URL)
    await page.keyboard.press("Space")
    await expect(field).to_have_attribute("type", "text")
    await expect(toggle).to_have_attribute("aria-label", "隐藏代理地址")
    assert posts == []
    assert not svc.status()["proxy"]["configured"]

    folder = Path("logs/release-v1.3.1")
    folder.mkdir(parents=True, exist_ok=True)
    await page.locator("#updateProxyForm").screenshot(path=str(folder / "proxy-visibility.png"))

    await page.locator("#updateProxyEnabled").check()
    response, _ = await submit_proxy(page)
    assert response.status == 200
    await expect(field).to_have_value("")
    await expect(field).to_have_attribute("type", "password")
    await expect(toggle).to_have_text("显示")
    await expect(toggle).to_have_attribute("aria-pressed", "false")
    await toggle.click()
    await expect(field).to_have_value("")
    await field.fill("http://replacement.example.test:3128")
    response, _ = await submit_proxy(page, "#updateProxyClear")
    assert response.status == 200
    await expect(field).to_have_value("")
    await expect(field).to_have_attribute("type", "password")
    assert not svc.status()["proxy"]["configured"]
    assert not errors
