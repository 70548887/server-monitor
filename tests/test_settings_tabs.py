"""设置页分栏、表单回跳与移动端交互；不连接真实数据库或更新服务器。"""

import copy
import os
from html.parser import HTMLParser
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from test_login_dialog import login_browser


browser_only = pytest.mark.skipif(
    os.getenv("RUN_BROWSER_TESTS") != "1", reason="显式启用浏览器验收"
)


class SettingsMarkup(HTMLParser):
    def __init__(self, markup):
        super().__init__()
        self.tabs = {}
        self.panels = {}
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if attributes.get("role") == "tab":
            self.tabs[attributes["data-settings-tab"]] = attributes
        panel_id = attributes.get("id", "")
        if panel_id.startswith("settings-panel-"):
            self.panels[panel_id.removeprefix("settings-panel-")] = attributes


@pytest.fixture
async def settings_client():
    from app.routes import auth

    service = SimpleNamespace(
        get_session=AsyncMock(return_value={"admin_id": 1, "username": "test-admin"}),
        get_admin_by_id=AsyncMock(return_value=SimpleNamespace(
            username="test-admin", email="admin@example.test", is_active=True,
            created_at=None, last_login=None,
        )),
        change_password=AsyncMock(return_value=(True, "成功")),
        update_admin_info=AsyncMock(return_value=(True, "成功")),
        destroy_session=AsyncMock(),
    )
    app = FastAPI()
    app.include_router(auth.router)
    app.dependency_overrides[auth.get_auth_service] = lambda: service
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        cookies={"session_id": "fixture-session"}, follow_redirects=False,
    ) as client:
        yield client, service


@pytest.mark.parametrize("query, selected", [
    ("", "account"), ("?tab=security", "security"),
    ("?tab=updates", "updates"), ("?tab=unknown", "account"),
])
async def test_settings_server_renders_only_requested_panel(settings_client, query, selected):
    client, _ = settings_client
    response = await client.get("/settings" + query)
    assert response.status_code == 200
    markup = SettingsMarkup(response.text)
    assert set(markup.tabs) == {"account", "security", "updates"}
    assert set(markup.panels) == set(markup.tabs)
    for key in markup.tabs:
        assert markup.tabs[key]["aria-selected"] == str(key == selected).lower()
        assert ("hidden" not in markup.panels[key]) == (key == selected)
        assert markup.tabs[key]["aria-controls"] == "settings-panel-" + key


@pytest.mark.parametrize("success", [True, False])
async def test_password_submission_returns_to_security_tab(settings_client, success):
    client, service = settings_client
    service.change_password.return_value = (success, "当前密码错误 & 请重试")
    response = await client.post("/settings/password", data={
        "old_password": "old-password", "new_password": "new-password",
        "confirm_password": "new-password",
    })
    assert response.status_code == 302
    target = urlparse(response.headers["location"])
    query = parse_qs(target.query)
    assert target.path == "/settings"
    assert query["tab"] == ["security"]
    assert ("success" if success else "error") in query
    if not success:
        assert query["error"] == ["当前密码错误 & 请重试"]
    service.change_password.assert_awaited_once_with(1, "old-password", "new-password")


async def test_password_mismatch_stays_in_security_without_changing_password(settings_client):
    client, service = settings_client
    response = await client.post("/settings/password", data={
        "old_password": "old-password", "new_password": "new-password",
        "confirm_password": "different-password",
    })
    assert response.status_code == 302
    query = parse_qs(urlparse(response.headers["location"]).query)
    assert query["tab"] == ["security"]
    assert query["error"] == ["两次输入的新密码不一致"]
    service.change_password.assert_not_awaited()


@pytest.mark.parametrize("success", [True, False])
async def test_profile_submission_returns_to_account_tab(settings_client, success):
    client, service = settings_client
    service.update_admin_info.return_value = (success, "保存失败 & 请重试")
    response = await client.post("/settings/profile", data={
        "username": "test-admin", "email": "updated@example.test",
    })
    assert response.status_code == 302
    query = parse_qs(urlparse(response.headers["location"]).query)
    assert query["tab"] == ["account"]
    assert ("success" if success else "error") in query
    if not success:
        assert query["error"] == ["保存失败 & 请重试"]
    service.update_admin_info.assert_awaited_once_with(1, "test-admin", "updated@example.test")
    service.destroy_session.assert_not_awaited()


async def test_renaming_account_still_requires_login(settings_client):
    client, service = settings_client
    response = await client.post("/settings/profile", data={"username": "renamed-admin"})
    assert response.status_code == 302
    assert urlparse(response.headers["location"]).path == "/login"
    service.destroy_session.assert_awaited_once_with("fixture-session")
    assert 'session_id=""' in response.headers["set-cookie"]


@pytest.fixture
async def settings_browser(login_browser, monkeypatch):
    from app.services.auth import AuthService

    page, url, _, _, errors = login_browser
    monkeypatch.setattr(AuthService, "get_session", AsyncMock(
        return_value={"admin_id": 1, "username": "test-admin"},
    ))
    monkeypatch.setattr(AuthService, "get_admin_by_id", AsyncMock(
        return_value=SimpleNamespace(
            username="test-admin", email="admin@example.test", is_active=True,
            created_at=None, last_login=None,
        ),
    ))
    await page.context.add_cookies([{
        "name": "session_id", "value": "fixture-session", "url": url,
    }])
    state = {
        "state": "idle", "current_version": "1.1.0", "message": "已准备就绪",
        "progress": 0, "capability": {"reason": "测试更新状态", "can_apply": False},
        "release": None, "backups": [],
    }
    checks = []

    async def update_api(route):
        if route.request.url.endswith("/check"):
            checks.append(route.request.method)
            state["release"] = {
                "version": "1.2.0", "has_update": True, "notes": "测试更新说明",
                "url": "https://example.test/releases/v1.2.0",
            }
            state["message"] = "发现新版本"
        await route.fulfill(status=200, json=copy.deepcopy(state))

    await page.route("**/api/system/update/*", update_api)
    await page.route("**/healthz", lambda route: route.fulfill(status=200, json={"status": "ok"}))
    yield page, url, errors, checks


async def assert_selected(page, name):
    from playwright.async_api import expect

    for key in ("account", "security", "updates"):
        tab = page.locator(f'[role="tab"][data-settings-tab="{key}"]')
        panel = page.locator("#settings-panel-" + key)
        await expect(tab).to_have_attribute("aria-selected", str(key == name).lower())
        if key == name:
            await expect(panel).to_be_visible()
        else:
            await expect(panel).not_to_be_visible()


@browser_only
async def test_default_account_tab_and_update_actions_after_switch(settings_browser):
    from playwright.async_api import expect

    page, url, errors, checks = settings_browser
    await page.goto(url + "/settings")
    await assert_selected(page, "account")
    await expect(page.locator('form[action="/settings/profile"]')).to_be_visible()
    await expect(page.locator("#old_password")).not_to_be_visible()
    await page.locator('[data-settings-tab="security"]').click()
    await assert_selected(page, "security")
    await expect(page.locator("#old_password")).to_be_visible()
    await page.locator('[data-settings-tab="updates"]').click()
    await assert_selected(page, "updates")
    await expect(page.locator("#updateCurrent")).to_have_text("v1.1.0")
    await page.locator("#updateCheck").click()
    await expect(page.locator("#updateLatest")).to_have_text("v1.2.0")
    await expect(page.locator("#updateDownload")).to_be_enabled()
    assert checks == ["POST"]
    assert not errors


@browser_only
async def test_tabs_keyboard_navigation_and_refresh_preserve_selection(settings_browser):
    from playwright.async_api import expect

    page, url, errors, _ = settings_browser
    await page.goto(url + "/settings?tab=security")
    await assert_selected(page, "security")
    await page.locator('[data-settings-tab="security"]').focus()
    await page.keyboard.press("ArrowRight")
    await assert_selected(page, "updates")
    await expect(page.locator('[data-settings-tab="updates"]')).to_be_focused()
    assert parse_qs(urlparse(page.url).query)["tab"] == ["updates"]
    await page.reload()
    await assert_selected(page, "updates")
    await page.locator('[data-settings-tab="updates"]').focus()
    await page.keyboard.press("Home")
    await assert_selected(page, "account")
    await page.keyboard.press("ArrowLeft")
    await assert_selected(page, "updates")
    await page.keyboard.press("Home")
    await page.keyboard.press("End")
    await assert_selected(page, "updates")
    await page.keyboard.press("ArrowLeft")
    await assert_selected(page, "security")
    assert not errors


@browser_only
@pytest.mark.parametrize("width", [320, 375])
async def test_settings_panels_fit_mobile_width(settings_browser, width):
    page, url, errors, _ = settings_browser
    await page.set_viewport_size({"width": width, "height": 812})
    await page.goto(url + "/settings")
    for name in ("account", "security", "updates"):
        await page.locator(f'[data-settings-tab="{name}"]').click()
        await assert_selected(page, name)
        size = await page.locator(".settings-page").evaluate("""element => ({
            left: element.getBoundingClientRect().left,
            right: element.getBoundingClientRect().right,
            scroll: element.scrollWidth,
            client: element.clientWidth,
            viewport: innerWidth
        })""")
        assert size["left"] >= -1
        assert size["right"] <= size["viewport"] + 1
        assert size["scroll"] <= size["client"] + 1, (name, size)
    assert not errors


@browser_only
async def test_emergency_account_is_not_reported_as_disabled(settings_browser, monkeypatch):
    from playwright.async_api import expect
    from app.services.auth import AuthService

    page, url, errors, _ = settings_browser
    monkeypatch.setattr(AuthService, "get_admin_by_id", AsyncMock(return_value=None))
    await page.goto(url + "/settings")
    await expect(page.locator("#settings-panel-account")).to_contain_text("应急账户")
    await expect(page.locator("#settings-panel-account")).not_to_contain_text("已禁用")
    assert not errors
