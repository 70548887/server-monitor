"""设置页升级流程浏览器测试：真实API、备份文件，GitHub下载使用独立测试包。"""
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from test_login_dialog import login_browser
from test_updates import service

pytestmark=pytest.mark.skipif(os.getenv("RUN_BROWSER_TESTS") != "1",reason="显式启用浏览器验收")


async def test_update_settings_check_download_backup_and_restart_gating(login_browser,tmp_path,monkeypatch):
    from playwright.async_api import expect
    from app.routes import updates
    from app.services.auth import AuthService
    page,url,_,_,errors=login_browser
    svc,_=service(tmp_path)
    monkeypatch.setattr(updates,"get_update_service",lambda:svc)
    monkeypatch.setattr(AuthService,"get_session",AsyncMock(return_value={"admin_id":1,"username":"test-admin"}))
    monkeypatch.setattr(AuthService,"get_admin_by_id",AsyncMock(return_value=SimpleNamespace(username="test-admin",email=None,is_active=True,created_at=None,last_login=None)))
    await page.context.add_cookies([{"name":"session_id","value":"fixture-session","url":url}])
    await page.goto(url+"/settings")
    await expect(page.locator('#updateCurrent')).to_have_text('v1.0.0')
    await expect(page.locator('#updateApply')).to_be_disabled()
    await page.locator('#updateCheck').click()
    await expect(page.locator('#updateLatest')).to_have_text('v1.1.0',timeout=8000)
    await expect(page.locator('#updateNotes')).to_contain_text('<img')
    assert await page.locator('#updateNotes img').count()==0
    await page.locator('#updateDownload').click()
    await expect(page.locator('#updateMessage')).to_contain_text('升级包已校验',timeout=8000)
    await expect(page.locator('#updateApply')).to_be_disabled()  # 直接uvicorn不冒充自动升级支持
    await page.locator('#updateBackup').click()
    await expect(page.locator('#updateBackups')).to_contain_text('v1.0.0',timeout=8000)
    assert len(svc.store.backups())==1
    await page.locator('#systemUpdate').scroll_into_view_if_needed()
    Path('logs').mkdir(exist_ok=True)
    await page.screenshot(path='logs/online-update-settings.png',animations='disabled')
    assert not errors


async def test_update_page_handles_api_failure_without_stuck_buttons(login_browser,monkeypatch):
    from playwright.async_api import expect
    from app.services.auth import AuthService
    page,url,_,_,errors=login_browser
    monkeypatch.setattr(AuthService,"get_session",AsyncMock(return_value={"admin_id":1,"username":"test-admin"}))
    monkeypatch.setattr(AuthService,"get_admin_by_id",AsyncMock(return_value=None))
    await page.context.add_cookies([{"name":"session_id","value":"fixture-session","url":url}])
    await page.route('**/api/system/update/status',lambda route:route.fulfill(status=503,content_type='application/json',body='{"detail":"offline"}'))
    await page.goto(url+'/settings')
    await expect(page.locator('#updateMessage')).to_contain_text('暂时无法连接')
    await expect(page.locator('#updateApply')).to_be_disabled()
    assert not errors
