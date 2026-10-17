"""一键更新与启动指导的浏览器回归；所有升级 API 均在浏览器中拦截。"""

import copy
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from test_login_dialog import login_browser


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_BROWSER_TESTS") != "1", reason="显式启用浏览器验收"
)


class PipelineAPI:
    """让浏览器只负责提交一次任务、持续读取由后台维护的状态。"""

    def __init__(self):
        self.posts = []
        self.status_reads = 0
        self.data = {
            "state": "available", "message": "发现可用版本 v1.4.2", "progress": 0,
            "current_version": "1.4.1", "repository": "70548887/server-monitor",
            "release": {
                "version": "1.4.2", "has_update": True, "notes": "连续更新测试",
                "url": "https://github.com/70548887/server-monitor/releases/tag/v1.4.2",
            },
            "capability": {
                "can_apply": True, "managed": True, "code": "managed", "setup": None,
                "reason": "升级前自动备份，健康检查失败自动恢复。",
            },
            "backups": [], "workflow": None,
            "proxy": {"enabled": False, "configured": False, "display_url": "", "has_auth": False},
        }

    def snapshot(self):
        return copy.deepcopy(self.data)

    def stage(self, state, message, progress=0):
        self.data.update(state=state, message=message, progress=progress)
        self.data["workflow"] = {"mode": "upgrade", "id": "browser-pipeline"}
        if state == "complete":
            self.data["current_version"] = "1.4.2"
            self.data["release"]["has_update"] = False

    async def route(self, route):
        action = route.request.url.rsplit("/", 1)[-1]
        if route.request.method == "GET" and action == "status":
            self.status_reads += 1
            await route.fulfill(status=200, json=self.snapshot())
            return
        if route.request.method == "POST":
            self.posts.append((action, route.request.post_data_json))
            if action == "proxy":
                self.data["proxy"]["enabled"] = bool(route.request.post_data_json["enabled"])
            elif action == "upgrade":
                self.stage("checking", "一键升级：正在检查 GitHub 版本", 0)
            else:
                await route.fulfill(status=400, json={"detail": "本测试只允许提交后台一键升级"})
                return
            await route.fulfill(status=200 if action == "proxy" else 202, json=self.snapshot())
            return
        await route.fulfill(status=404, json={"detail": "not found"})


@pytest.fixture
async def pipeline_browser(login_browser, monkeypatch):
    from app.services.auth import AuthService

    page, url, _, _, errors = login_browser
    monkeypatch.setattr(AuthService, "get_session", AsyncMock(return_value={
        "admin_id": 1, "username": "test-admin",
    }))
    monkeypatch.setattr(AuthService, "get_admin_by_id", AsyncMock(return_value=SimpleNamespace(
        username="test-admin", email=None, is_active=True, created_at=None, last_login=None,
    )))
    await page.context.add_cookies([{"name": "session_id", "value": "fixture-session", "url": url}])
    await page.route("**/healthz", lambda route: route.fulfill(status=200, json={"status": "ok"}))
    api = PipelineAPI()
    await page.route("**/api/system/update/*", api.route)
    await page.clock.install()
    dialogs = []

    async def accept(dialog):
        dialogs.append(dialog.message)
        await dialog.accept()

    page.on("dialog", accept)
    yield page, url, api, dialogs, errors


async def open_pipeline(page, url):
    from playwright.async_api import expect

    await page.goto(url + "/settings?tab=updates")
    await expect(page.locator("#updateCurrent")).to_have_text("v1.4.1")
    await expect(page.locator("#updateSteps")).to_have_attribute("aria-label", "升级步骤")
    await expect(page.locator("#updateSteps li")).to_have_count(5)


async def poll_stage(page, api, state, message, progress):
    from playwright.async_api import expect

    api.stage(state, message, progress)
    await page.clock.fast_forward(3000)
    await expect(page.locator("#updateMessage")).to_contain_text(message)
    if state == "complete":
        await expect(page.locator('#updateSteps li[data-state="done"]')).to_have_count(5)
    else:
        current = {
            "checking": "check", "downloading": "download", "queued": "apply",
            "restarting": "restart",
        }[state]
        await expect(page.locator(f'#updateSteps li[data-step="{current}"]')).to_have_attribute("data-state", "current")
        await expect(page.locator('#updateSteps li[data-state="current"]')).to_have_count(1)


async def save_update_preview(page, filename):
    folder = Path("logs/update-pipeline-20261017")
    folder.mkdir(parents=True, exist_ok=True)
    await page.set_viewport_size({"width": 1200, "height": 1200})
    await page.locator("#systemUpdate").evaluate("element => element.scrollIntoView({block: 'start'})")
    await page.screenshot(path=str(folder / filename), animations="disabled")


@pytest.mark.parametrize("initial", ["available", "ready"])
async def test_one_confirmation_starts_durable_pipeline_without_client_side_apply(pipeline_browser, initial):
    from playwright.async_api import expect

    page, url, api, dialogs, errors = pipeline_browser
    api.data["state"] = initial
    if initial == "ready":
        api.data["message"] = "升级包已校验，可以应用更新"
        api.data["staged"] = {"id": "browser-package", "version": "1.4.2"}
    await open_pipeline(page, url)
    await expect(page.locator("#updateUpgrade")).to_have_text("继续更新" if initial == "ready" else "一键更新")
    await expect(page.locator(f'#updateSteps li[data-step="{"apply" if initial == "ready" else "download"}"]')).to_have_attribute("data-state", "current")
    await page.locator("#updateUpgrade").click()
    await expect(page.locator("#updateUpgrade")).to_be_disabled()
    await expect(page.locator('#updateSteps li[data-step="check"]')).to_have_attribute("data-state", "current")
    assert len(dialogs) == 1
    assert [action for action, _ in api.posts] == ["upgrade"]
    await poll_stage(page, api, "downloading", "正在下载并校验升级包", 40)
    await poll_stage(page, api, "queued", "已提交应用更新，等待管理器处理", 100)
    await poll_stage(page, api, "restarting", "正在重启并等待健康检查", 96)
    if initial == "available":
        await save_update_preview(page, "managed-restarting.png")
    await poll_stage(page, api, "complete", "更新完成，服务已恢复", 100)
    await expect(page.locator("#updateCurrent")).to_have_text("v1.4.2")
    assert [action for action, _ in api.posts] == ["upgrade"]
    assert len(dialogs) == 1
    assert not errors


async def test_reload_rejoins_running_pipeline_and_keeps_polling(pipeline_browser):
    from playwright.async_api import expect

    page, url, api, dialogs, errors = pipeline_browser
    api.stage("downloading", "正在下载并校验升级包", 35)
    await open_pipeline(page, url)
    await expect(page.locator("#updateUpgrade")).to_be_disabled()
    await page.reload()
    await expect(page.locator("#updateMessage")).to_contain_text("正在下载并校验升级包")
    reads = api.status_reads
    await poll_stage(page, api, "queued", "已提交应用更新，等待管理器处理", 100)
    assert api.status_reads > reads
    await poll_stage(page, api, "complete", "更新完成，服务已恢复", 100)
    await expect(page.locator("#updateCurrent")).to_have_text("v1.4.2")
    assert not api.posts and not dialogs and not errors


@pytest.mark.parametrize("legacy", [False, True])
async def test_unmanaged_verified_package_explains_disabled_apply_and_copyable_launcher(pipeline_browser, legacy):
    from playwright.async_api import expect

    page, url, api, dialogs, errors = pipeline_browser
    api.data.update(state="ready", message="升级包已校验，可以应用更新")
    directory = "/www/wwwroot/server monitor"
    command = "/www/server/python3 scripts/run_server.py --host 0.0.0.0 --port 8080"
    api.data["capability"] = {
        "can_apply": False, "managed": False,
        "reason": "在线应用和重启需要用 python scripts/run_server.py 启动服务。",
    }
    if not legacy:
        api.data["capability"].update(code="manager_required", setup={
            "working_directory": directory, "command": command,
        })
    await open_pipeline(page, url)
    await expect(page.locator("#updateApply")).to_be_disabled()
    await expect(page.locator("#updateUpgrade")).to_be_disabled()
    await expect(page.locator("#updateSetup")).to_be_visible()
    await expect(page.locator("#updateSetup")).to_contain_text("校验")
    await expect(page.locator("#updateSetup")).to_contain_text("在线")
    await expect(page.locator("#updateSetupCommand")).to_contain_text("scripts/run_server.py")
    if not legacy:
        await expect(page.locator("#updateSetupDirectory")).to_have_text(directory)
        await expect(page.locator("#updateSetupCommand")).to_have_text(command)
    await page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    await page.locator("#updateSetupCopy").click()
    copied = await page.evaluate("navigator.clipboard.readText()")
    assert "scripts/run_server.py" in copied
    if not legacy:
        await save_update_preview(page, "unmanaged-ready.png")
    assert not api.posts and not dialogs and not errors


@pytest.mark.parametrize("failed_state,failed_step,done", [
    (None, "check", 0),
    ("downloading", "download", 1),
    ("restarting", "restart", 3),
])
async def test_pipeline_error_stops_polling_and_allows_a_new_explicit_attempt(pipeline_browser, failed_state, failed_step, done):
    from playwright.async_api import expect

    page, url, api, dialogs, errors = pipeline_browser
    api.stage("error", "更新包校验失败，请重试", 0)
    if failed_state is not None:
        api.data["failed_state"] = failed_state
    await open_pipeline(page, url)
    await expect(page.locator("#updateMessage")).to_contain_text("校验失败")
    await expect(page.locator(f'#updateSteps li[data-step="{failed_step}"]')).to_have_attribute("data-state", "error")
    await expect(page.locator('#updateSteps li[data-state="error"]')).to_have_count(1)
    await expect(page.locator('#updateSteps li[data-state="done"]')).to_have_count(done)
    await expect(page.locator('#updateSteps li[data-state="pending"]')).to_have_count(4 - done)
    await expect(page.locator("#updateUpgrade")).to_be_enabled()
    reads = api.status_reads
    await page.clock.fast_forward(10000)
    assert api.status_reads == reads
    await page.locator("#updateUpgrade").click()
    await expect(page.locator("#updateUpgrade")).to_be_disabled()
    assert len(dialogs) == 1
    assert [action for action, _ in api.posts] == ["upgrade"]
    assert not errors


async def test_one_click_enables_saved_proxy_before_starting_upgrade(pipeline_browser):
    from playwright.async_api import expect

    page, url, api, dialogs, errors = pipeline_browser
    api.data["proxy"] = {
        "enabled": False, "configured": True,
        "display_url": "http://127.0.0.1:8888", "has_auth": True,
    }
    await open_pipeline(page, url)
    await expect(page.locator("#updateUpgrade")).to_have_text("启用代理并更新")
    await page.locator("#updateUpgrade").click()
    await expect(page.locator("#updateUpgrade")).to_be_disabled()
    assert [action for action, _ in api.posts] == ["proxy", "upgrade"]
    assert api.posts[0][1] == {"enabled": True}
    assert api.data["proxy"]["enabled"]
    assert len(dialogs) == 1 and not errors


async def test_unsaved_proxy_prevents_one_click_upgrade(pipeline_browser):
    from playwright.async_api import expect

    page, url, api, _, errors = pipeline_browser
    await open_pipeline(page, url)
    await page.locator("#updateProxyUrl").fill("http://draft:private@127.0.0.1:9999")
    await page.locator("#updateUpgrade").click()
    await expect(page.locator("#updateProxyMessage")).to_contain_text("尚未保存")
    assert not api.posts and not errors


@pytest.mark.parametrize("width", [320, 375])
async def test_update_pipeline_guidance_and_steps_fit_mobile(pipeline_browser, width):
    from playwright.async_api import expect

    page, url, api, _, errors = pipeline_browser
    api.data["capability"] = {
        "can_apply": False, "managed": False, "code": "manager_required",
        "reason": "尚未使用更新管理器启动。",
        "setup": {
            "working_directory": "/www/wwwroot/server_monitor/very-long-project-directory",
            "command": "/www/server/pyporject_evn/versions/3.12.11/bin/python3 scripts/run_server.py --host 0.0.0.0 --port 8080",
        },
    }
    await page.set_viewport_size({"width": width, "height": 812})
    await open_pipeline(page, url)
    await expect(page.locator("#updateSetup")).to_be_visible()
    await expect(page.locator("#updateUpgrade")).to_be_disabled()
    for selector in ("#systemUpdate", "#updateSteps", "#updateSetup", "#updateSetupCommand", "#updateUpgrade"):
        size = await page.locator(selector).evaluate("""element => ({
            left: element.getBoundingClientRect().left,
            right: element.getBoundingClientRect().right,
            scroll: element.scrollWidth, client: element.clientWidth, viewport: innerWidth
        })""")
        assert size["left"] >= -1, (selector, size)
        assert size["right"] <= size["viewport"] + 1, (selector, size)
        assert size["scroll"] <= size["client"] + 1, (selector, size)
    assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert not errors


async def test_pipeline_available_to_download_keeps_progress_and_polling(pipeline_browser):
    from playwright.async_api import expect

    page, url, api, dialogs, errors = pipeline_browser
    # 后台完成检查时 progress=100 仍只代表整条流程的第一阶段完成。
    api.stage("available", "发现可用版本，正在进入下载阶段", 100)
    await open_pipeline(page, url)
    await expect(page.locator("#updateUpgrade")).to_be_disabled()
    await expect(page.locator("#updateProgress")).to_be_visible()
    await expect(page.locator("#updateProgress")).to_have_js_property("value", 10)
    before = await page.locator("#updateProgress").evaluate("element => element.value")
    reads = api.status_reads
    await poll_stage(page, api, "downloading", "下载已开始", 0)
    after = await page.locator("#updateProgress").evaluate("element => element.value")
    assert after >= before
    assert api.status_reads > reads
    await expect(page.locator("#updateUpgrade")).to_be_disabled()
    assert not api.posts and not dialogs and not errors


async def test_pipeline_transient_ready_stays_busy_until_backend_queues_apply(pipeline_browser):
    from playwright.async_api import expect

    page, url, api, dialogs, errors = pipeline_browser
    api.stage("ready", "更新包已校验，后台正在提交应用任务", 100)
    await open_pipeline(page, url)
    await expect(page.locator('#updateSteps li[data-step="apply"]')).to_have_attribute("data-state", "current")
    for selector in ("#updateUpgrade", "#updateApply", "#updateDownload", "#updateCheck", "#updateRestart"):
        await expect(page.locator(selector)).to_be_disabled()
    reads = api.status_reads
    await poll_stage(page, api, "queued", "已提交应用更新，等待管理器处理", 100)
    assert api.status_reads > reads
    await expect(page.locator("#updateUpgrade")).to_be_disabled()
    assert not api.posts and not dialogs and not errors


async def test_pipeline_up_to_date_finishes_only_check_step_and_stops_polling(pipeline_browser):
    from playwright.async_api import expect

    page, url, api, dialogs, errors = pipeline_browser
    api.stage("checking", "正在检查是否有新版本", 0)
    await open_pipeline(page, url)
    api.stage("up_to_date", "当前已是最新版本", 100)
    api.data["release"]["has_update"] = False
    api.data["release"]["version"] = "1.4.1"
    await page.clock.fast_forward(3000)
    await expect(page.locator("#updateMessage")).to_have_text("当前已是最新版本")
    await expect(page.locator('#updateSteps li[data-step="check"]')).to_have_attribute("data-state", "done")
    await expect(page.locator('#updateSteps li[data-state="done"]')).to_have_count(1)
    await expect(page.locator('#updateSteps li[data-state="pending"]')).to_have_count(4)
    await expect(page.locator('#updateSteps li[aria-current]')).to_have_count(0)
    await expect(page.locator("#updateUpgrade")).to_be_enabled()
    await expect(page.locator("#updateProgress")).not_to_be_visible()
    reads = api.status_reads
    await page.clock.fast_forward(10000)
    assert api.status_reads == reads
    assert not api.posts and not dialogs and not errors


@pytest.mark.parametrize("terminal", ["up_to_date", "complete"])
async def test_terminal_update_can_start_another_check_for_future_versions(pipeline_browser, terminal):
    from playwright.async_api import expect

    page, url, api, dialogs, errors = pipeline_browser
    api.stage(terminal, "当前已是最新版本", 100)
    # 已检查或已升级完成，页面长时间打开后仍应允许再次发现将来的版本。
    api.data["current_version"] = "1.4.1"
    api.data["release"].update(version="1.4.1", has_update=False)
    await open_pipeline(page, url)
    await expect(page.locator("#updateUpgrade")).to_be_enabled()
    await expect(page.locator("#updateUpgrade")).to_have_text("一键更新")
    await page.locator("#updateUpgrade").click()
    await expect(page.locator("#updateMessage")).to_contain_text("正在检查 GitHub 版本")
    await expect(page.locator("#updateUpgrade")).to_be_disabled()
    assert [action for action, _ in api.posts] == ["upgrade"]
    assert len(dialogs) == 1 and not errors


async def test_accepted_upgrade_with_lost_response_rejoins_without_duplicate_post(pipeline_browser):
    from playwright.async_api import expect

    page, url, api, dialogs, errors = pipeline_browser

    async def accepted_then_disconnect(route):
        api.posts.append(("upgrade", route.request.post_data_json))
        api.stage("downloading", "后台已接受任务并开始下载", 20)
        # 模拟后台已启动工作，HTTP 回包在传输途中断开。
        await route.abort("failed")

    await page.route("**/api/system/update/upgrade", accepted_then_disconnect, times=1)
    await open_pipeline(page, url)
    reads = api.status_reads
    await page.locator("#updateUpgrade").click()
    await expect(page.locator("#updateMessage")).to_contain_text("请求结果尚未确认")
    await expect(page.locator("#updateUpgrade")).to_be_disabled()
    await page.clock.fast_forward(1500)
    await expect(page.locator("#updateMessage")).to_contain_text("后台已接受任务并开始下载")
    assert api.status_reads > reads
    await expect(page.locator("#updateUpgrade")).to_be_disabled()
    await poll_stage(page, api, "complete", "更新完成，服务已恢复", 100)
    await expect(page.locator("#updateUpgrade")).to_be_enabled()
    assert [action for action, _ in api.posts] == ["upgrade"]
    assert len(dialogs) == 1 and not errors


@pytest.mark.parametrize("status,detail", [
    (400, "升级条件未满足，请重新检查版本"),
    (409, "已有其他升级操作，请稍后重试"),
])
async def test_explicit_upgrade_rejection_keeps_readable_error_and_allows_retry(pipeline_browser, status, detail):
    from playwright.async_api import expect

    page, url, api, dialogs, errors = pipeline_browser

    async def reject_once(route):
        api.posts.append(("upgrade", route.request.post_data_json))
        await route.fulfill(status=status, json={"detail": detail})

    await page.route("**/api/system/update/upgrade", reject_once, times=1)
    await open_pipeline(page, url)
    reads = api.status_reads
    await page.locator("#updateUpgrade").click()
    await expect(page.locator("#updateMessage")).to_have_text(detail)
    await expect(page.locator("#updateUpgrade")).to_be_enabled()
    await page.clock.fast_forward(5000)
    await expect(page.locator("#updateMessage")).to_have_text(detail)
    assert api.status_reads == reads
    await page.locator("#updateUpgrade").click()
    await expect(page.locator("#updateMessage")).to_contain_text("正在检查 GitHub 版本")
    await expect(page.locator("#updateUpgrade")).to_be_disabled()
    assert [action for action, _ in api.posts] == ["upgrade", "upgrade"]
    assert len(dialogs) == 2 and not errors
