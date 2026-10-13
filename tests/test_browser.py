"""真实 Chromium 验收；使用临时 MySQL 库，不启动探测/通知调度器。"""
import asyncio
import os
import socket
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from test_mysql_integration import mysql_store

pytestmark = pytest.mark.skipif(os.getenv("RUN_BROWSER_TESTS") != "1", reason="显式启用浏览器验收")


async def test_login_slider_node_crud_and_percentile_page(mysql_store, monkeypatch):
    import uvicorn
    from playwright.async_api import async_playwright, expect
    from app.main import app
    from app.models import base
    from app.models.admin import Admin
    from app.models.sample import AggregateModel
    from app.services.auth import AuthService
    from app.utils import captcha
    from app.middleware.security import rate_limit_store
    from app.utils.timeutil import utcnow

    engine, sessions, repo = mysql_store
    monkeypatch.setattr(base, "_engine", engine)
    monkeypatch.setattr(base, "_AsyncSessionLocal", sessions)
    monkeypatch.setattr(AuthService, "_check_redis", AsyncMock(return_value=False))
    monkeypatch.setattr(rate_limit_store, "_redis_available", False)
    isolated_captcha = captcha.CaptchaStore()
    isolated_captcha._redis_available = False
    monkeypatch.setattr(captcha, "store", isolated_captcha)
    async with sessions() as s:
        s.add(Admin(username="browser-test", password_hash=AuthService.hash_password("BrowserFixture-123!"), is_active=True))
        await s.commit()

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
            try:
                page = await browser.new_page(viewport={"width":1200,"height":850})
                errors = []
                page.on("pageerror", lambda exc: errors.append(str(exc)))
                await page.route("**/*", lambda route: route.continue_() if route.request.url.startswith(url) else route.abort())
                await page.goto(url + "/login")
                await page.locator("#username").fill("browser-test")
                await page.locator("#password").fill("BrowserFixture-123!")
                await page.locator("#loginBtn").click()
                await page.locator("#sliderHandle").wait_for()
                # 测试进程读取自身生成的挑战答案；实际通过鼠标操作验证前后端协议。
                record = next(iter(isolated_captcha._memory._data.values()))[1]
                target = record["x"]
                box = await page.locator("#sliderHandle").bounding_box()
                track = await page.locator("#sliderTrack").bounding_box()
                # 原始图片310px，拼图50px；按显示轨道宽度换算实际拖动距离。
                travel_scale = (track["width"] - 2 - box["width"]) / 260
                x,y = box["x"] + box["width"]/2,box["y"] + box["height"]/2
                await page.mouse.move(x,y)
                await page.mouse.down()
                position = 0
                steps = [3,5,8,11,7,4]
                i = 0
                while position < target:
                    position = min(target, position + steps[i % len(steps)])
                    await page.mouse.move(x+position*travel_scale,y)
                    await asyncio.sleep(.025)
                    i += 1
                await page.mouse.up()
                await page.wait_for_url("**/ui/**")

                name = "O'Reilly 审计节点"
                unsafe_name = "<img src=x onerror=window.auditXss=1>"
                from app.models.node import NodeModel
                async with sessions() as s:
                    s.add(NodeModel(name=unsafe_name,url="http://test.invalid",enabled=False))
                    await s.commit()
                response = await page.request.post(url+"/api/nodes", data={"name":name,"url":"http://test.invalid","enabled":False})
                assert response.status == 201
                await page.goto(url+"/ui/nodes")
                await expect(page.get_by_text(name,exact=True)).to_be_visible()
                await expect(page.get_by_text(unsafe_name,exact=True)).to_be_visible()
                await page.locator("tr").filter(has=page.get_by_text(name,exact=True)).get_by_role("button",name="编辑",exact=True).click()
                await expect(page.locator("#editNodeModal")).to_be_visible()
                assert await page.evaluate("window.auditXss || 0") == 0
                # 持久化窗口统计用真实 SQL，详情页必须显示真正的分位数。
                async with sessions() as s:
                    end = utcnow()-timedelta(minutes=5)
                    s.add(AggregateModel(node_name=name,window_start=end-timedelta(minutes=5),window_end=end,window_minutes=5,total_count=5,latency_p50_ms=10,latency_p95_ms=20,latency_p99_ms=30))
                    await s.commit()
                from urllib.parse import quote
                await page.goto(url+"/ui/nodes/"+quote(name,safe=""))
                await expect(page.locator("#latencyPercentiles")).to_contain_text("P95 20.00 ms")
                assert await page.evaluate("window.auditXss || 0") == 0
                Path("logs").mkdir(exist_ok=True)
                await page.screenshot(path="logs/browser-node-detail.png")
                assert not errors, errors
                response=await page.request.delete(url+"/api/nodes/"+quote(name,safe="")+"?hard=true")
                assert response.status == 200
            finally:
                await browser.close()
    finally:
        server.should_exit = True
        await asyncio.wait_for(task,10)
        sock.close()
