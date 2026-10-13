"""登录验证弹窗的真实浏览器回归；登录提交截留在测试浏览器，不使用真实账号。"""
import asyncio
import os
import socket
from pathlib import Path
from urllib.parse import parse_qs

import pytest

pytestmark = pytest.mark.skipif(os.getenv("RUN_BROWSER_TESTS") != "1", reason="显式启用浏览器验收")


@pytest.fixture
async def login_browser(monkeypatch):
    import uvicorn
    from playwright.async_api import async_playwright
    from app.main import app
    from app.utils import captcha
    from app.middleware.security import rate_limit_store

    store = captcha.CaptchaStore()
    store._redis_available = False
    monkeypatch.setattr(captcha, "store", store)
    monkeypatch.setattr(rate_limit_store, "_redis_available", False)
    monkeypatch.setattr(rate_limit_store, "_requests", __import__('collections').defaultdict(list))
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    url = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="error"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(.02)
        assert server.started
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width":1200,"height":850})
            requests, errors = [], []
            page.on("pageerror", lambda error: errors.append(str(error)))
            async def routes(route):
                request = route.request
                if not request.url.startswith(url):
                    await route.abort()
                elif request.method == "POST" and request.url == url + "/login":
                    requests.append(parse_qs(request.post_data))
                    await route.fulfill(status=200, content_type="text/html", body="<h1>Login submitted</h1>")
                else:
                    await route.continue_()
            await page.route("**/*", routes)
            try:
                yield page, url, store, requests, errors
            finally:
                await browser.close()
    finally:
        server.should_exit = True
        await asyncio.wait_for(task,10)
        sock.close()


async def open_dialog(page, url):
    from playwright.async_api import expect
    await page.goto(url + "/login")
    await page.locator("#username").fill("test-user")
    await page.locator("#password").fill("test-password")
    await page.locator("#loginBtn").click()
    await expect(page.locator("#sliderHandle")).to_be_visible()


async def drag_original_pixels(page, target):
    handle = await page.locator("#sliderHandle").bounding_box()
    track = await page.locator("#sliderTrack").bounding_box()
    ratio = (track["width"] - 2 - handle["width"]) / 260
    x, y = handle["x"] + handle["width"]/2, handle["y"] + handle["height"]/2
    await page.mouse.move(x,y)
    await page.mouse.down()
    position, i = 0, 0
    steps = [3, 5, 8, 11, 7, 4]
    while position < target:
        position = min(target, position + steps[i % len(steps)])
        await page.mouse.move(x + position * ratio, y)
        await asyncio.sleep(.025)
        i += 1
    await page.mouse.up()


async def test_initial_page_has_no_challenge_and_requires_credentials(login_browser):
    from playwright.async_api import expect
    page, url, store, submissions, errors = login_browser
    await page.goto(url + "/login")
    await expect(page.locator("#loginBtn")).to_be_enabled()
    await expect(page.locator("#captchaDialog")).not_to_be_visible()
    assert not store._memory._data
    # 新模板不能继续加载浏览器里缓存的常驻验证码脚本。
    assert await page.locator('script[src*="login.js"]').get_attribute("src") == "/static/js/login.js?v=login-dialog-1"
    Path('logs').mkdir(exist_ok=True)
    await page.screenshot(path="logs/login-page-optimized.png", animations="disabled")
    await page.locator("#loginBtn").click()
    assert not store._memory._data
    await page.locator("#username").fill("test-user")
    await page.locator("#password").fill("test-password")
    await page.locator("#password").press("Enter")
    await expect(page.locator("#sliderHandle")).to_be_visible()
    assert len(store._memory._data) == 1
    await page.keyboard.press("Escape")
    await expect(page.locator("#captchaDialog")).not_to_be_visible()
    await expect(page.locator("#loginBtn")).to_be_focused()
    assert await page.locator("#password").input_value() == "test-password"
    assert await page.locator("#captcha_id").input_value() == ""
    assert not submissions and not errors


@pytest.mark.parametrize("width", [1200, 320])
async def test_scaled_puzzle_success_submits_once(login_browser, width):
    from playwright.async_api import expect
    page, url, store, submissions, errors = login_browser
    await page.set_viewport_size({"width":width,"height":850})
    if width == 320:
        await page.add_init_script("localStorage.setItem('login-theme', 'dark')")
    await open_dialog(page,url)
    assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    record = next(iter(store._memory._data.values()))[1]
    Path('logs').mkdir(exist_ok=True)
    await page.screenshot(path=f"logs/login-dialog-{width}.png", animations="disabled")
    await drag_original_pixels(page, record["x"])
    await expect(page.get_by_role("heading",name="Login submitted")).to_be_visible()
    assert len(submissions) == 1
    assert abs(int(submissions[0]["captcha_x"][0]) - record["x"]) <= 1
    assert submissions[0]["captcha_trail"][0] != "[]"
    assert submissions[0]["username"] == ["test-user"]
    assert not errors


async def test_failed_drag_refresh_and_close_do_not_submit(login_browser):
    from playwright.async_api import expect
    page, url, store, submissions, errors = login_browser
    await open_dialog(page,url)
    old_id = next(iter(store._memory._data))
    await page.locator("#sliderHandle").click()
    await expect(page.locator("#captchaDialog")).to_have_attribute("data-state", "error")
    assert old_id not in store._memory._data
    await page.locator("#captchaRefresh").click()
    await expect(page.locator("#sliderHandle")).to_be_enabled()
    assert old_id not in store._memory._data and len(store._memory._data) == 1
    await page.locator("#captchaClose").click()
    await expect(page.locator("#captchaDialog")).not_to_be_visible()
    assert not submissions and not errors


async def test_loading_error_can_be_retried(login_browser):
    from playwright.async_api import expect
    page, url, _, submissions, errors = login_browser
    await page.route("**/api/auth/captcha", lambda route: route.fulfill(status=503, body="offline"), times=1)
    await page.goto(url + "/login")
    await page.locator("#username").fill("test-user")
    await page.locator("#password").fill("test-password")
    await page.locator("#loginBtn").click()
    await expect(page.locator("#captchaStatus")).to_contain_text("加载失败")
    assert await page.locator(".captcha-spinner").count() == 0
    await page.locator("#captchaRefresh").click()
    await expect(page.locator("#sliderHandle")).to_be_visible()
    assert not submissions and not errors


async def test_expired_dialog_requires_new_challenge(login_browser):
    from playwright.async_api import expect
    page, url, _, submissions, errors = login_browser
    await page.clock.install()
    await open_dialog(page,url)
    await page.clock.fast_forward(116000)
    await expect(page.locator("#captchaStatus")).to_contain_text("已过期")
    await expect(page.locator("#sliderHandle")).to_be_disabled()
    await page.locator("#captchaRefresh").click()
    await expect(page.locator("#sliderHandle")).to_be_enabled()
    assert not submissions and not errors


async def test_touch_drag_submits_scaled_image_coordinates(login_browser):
    from playwright.async_api import expect
    page, url, store, submissions, errors = login_browser
    await page.set_viewport_size({"width":375,"height":812})
    client = await page.context.new_cdp_session(page)
    await client.send("Emulation.setTouchEmulationEnabled", {"enabled":True})
    await open_dialog(page,url)
    target = next(iter(store._memory._data.values()))[1]["x"]
    handle = await page.locator("#sliderHandle").bounding_box()
    track = await page.locator("#sliderTrack").bounding_box()
    x, y = handle["x"]+handle["width"]/2, handle["y"]+handle["height"]/2
    ratio = (track["width"]-2-handle["width"])/260
    await client.send("Input.dispatchTouchEvent", {"type":"touchStart","touchPoints":[{"x":x,"y":y}]})
    position, i = 0, 0
    while position < target:
        position = min(target,position+[3,5,8,11,7,4][i%6])
        await client.send("Input.dispatchTouchEvent", {"type":"touchMove","touchPoints":[{"x":x+position*ratio,"y":y}]})
        await asyncio.sleep(.025)
        i += 1
    await client.send("Input.dispatchTouchEvent", {"type":"touchEnd","touchPoints":[]})
    await expect(page.get_by_role("heading",name="Login submitted")).to_be_visible()
    assert len(submissions) == 1
    assert abs(int(submissions[0]["captcha_x"][0])-target) <= 1
    assert not errors


async def test_closing_during_verification_ignores_late_success(login_browser):
    from playwright.async_api import expect
    page, url, store, submissions, errors = login_browser
    arrived, release = asyncio.Event(), asyncio.Event()
    async def delayed(route):
        arrived.set()
        await release.wait()
        await route.fulfill(status=200,content_type="application/json",body='{"success":true}')
    await page.route("**/api/auth/captcha/verify",delayed)
    await open_dialog(page,url)
    record = next(iter(store._memory._data.values()))[1]
    await drag_original_pixels(page,record["x"])
    await asyncio.wait_for(arrived.wait(),5)
    await page.locator("#captchaClose").click()
    release.set()
    await expect(page.locator("#captchaDialog")).not_to_be_visible()
    await page.locator("#loginBtn").click()
    await expect(page.locator("#sliderHandle")).to_be_visible()
    assert await page.locator("#captcha_id").input_value() == ""
    assert not submissions and not errors
