"""Rule-based notification skips remain distinct from actual delivery failures in both views."""

import os
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse
from unittest.mock import AsyncMock

import pytest

from test_login_dialog import login_browser


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_BROWSER_TESTS") != "1", reason="显式启用浏览器验收"
)


@pytest.fixture
async def notification_skip_browser(login_browser, monkeypatch):
    from app.services.auth import AuthService

    page, url, _, _, errors = login_browser
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
    unsafe_reason = '节点 <img src=x onerror="window.ruleSkipInjected=true"> 今日发送已达上限（20次）'
    base = {
        "channel_name": "默认邮箱", "node_name": "test-node", "alert_type": "node_down",
        "recipients": "fixture@example.test", "sent_at": "2026-10-16T10:00:00+08:00",
        "notify_seq": 5, "send_duration_ms": None,
    }
    logs = [
        {**base, "id": 1, "subject": "delivered", "success": True,
         "status": "success", "error_message": None, "send_duration_ms": 80},
        {**base, "id": 2, "subject": "smtp-failed", "success": False,
         "status": "failed", "error_message": "SMTP connection timed out"},
        {**base, "id": 3, "subject": "daily-limit", "success": False,
         "status": "skipped", "skip_reason": unsafe_reason, "error_message": None},
        {**base, "id": 4, "subject": "legacy-ascii-limit", "success": False,
         "error_message": "发送被限制: 最小发送间隔未到"},
        {**base, "id": 5, "subject": "legacy-chinese-limit", "success": False,
         "error_message": "发送被限制：当前不在发送时段"},
    ]
    requests = []

    async def notification_routes(route):
        request = route.request
        parsed = urlparse(request.url)
        requests.append(parsed)
        if parsed.path in {"/api/notifications/recipients", "/api/notifications/channels"}:
            data = []
        elif parsed.path == "/api/notifications/channels/recalculate-stats":
            data = {"message": "已重新计算1个渠道的统计", "channels": {"1": {
                "channel_name": "默认邮箱", "old_total": 5, "new_total": 2,
                "old_failed": 4, "new_failed": 1, "changed": True,
            }}}
        elif parsed.path == "/api/notifications/logs/stats":
            data = {"today": {"total": 2, "success": 1, "failed": 1, "skipped": 3}}
        elif parsed.path == "/api/notifications/logs":
            status = parse_qs(parsed.query).get("status", [""])[0]
            ids = {"success": [1], "failed": [2], "skipped": [3, 4, 5]}
            filtered = [item for item in logs if not status or item["id"] in ids[status]]
            data = {"total": len(filtered), "logs": filtered}
        elif parsed.path == "/api/notifications/incidents":
            data = {"total": 1, "incidents": [{
                "id": 7, "node_name": "test-node", "alert_type": "node_down",
                "status": "open", "started_at": "2026-10-15T10:00:00+08:00",
                "notify_count": 3, "total_failures": 20,
            }]}
        elif parsed.path == "/api/notifications/incidents/stats":
            data = {"open_incidents": 1, "resolved_incidents": 0, "open_notifications": 3}
        elif parsed.path == "/api/notifications/alerts/logs/stats":
            data = {"today_total": 1, "by_type": {}}
        elif parsed.path == "/api/notifications/incidents/7/notifications":
            data = {"incident_id": 7, "count": len(logs), "notifications": logs}
        else:
            await route.fulfill(status=404, json={"detail": "Unmocked test endpoint"})
            return
        await route.fulfill(status=200, json=data)

    await page.route("**/api/notifications/**", notification_routes)
    await page.route("**/healthz", lambda route: route.fulfill(status=200, json={"status": "ok", "uptime_seconds": 60}))
    yield page, url, requests, errors, unsafe_reason


async def open_notification_logs(page, url):
    from playwright.async_api import expect

    await page.goto(url + "/ui/notifications")
    await page.locator('[data-tab="logs"]').click()
    await expect(page.locator("#logs-tbody tr")).to_have_count(5)


async def test_notification_skips_are_neutral_and_not_counted_as_failed(notification_skip_browser):
    from playwright.async_api import expect

    page, url, _, errors, unsafe_reason = notification_skip_browser
    await open_notification_logs(page, url)
    await expect(page.locator("#today-total")).to_have_text("2")
    await expect(page.locator("#today-success")).to_have_text("1")
    await expect(page.locator("#today-failed")).to_have_text("1")
    await expect(page.locator("#today-skipped")).to_have_text("3")
    for subject in ["daily-limit", "legacy-ascii-limit", "legacy-chinese-limit"]:
        row = page.locator("#logs-tbody tr").filter(has_text=subject)
        cell = row.locator("td").nth(7)
        await expect(cell.locator(".badge-secondary")).to_have_text("规则跳过")
        await expect(cell.locator(".badge-danger")).to_have_count(0)
        assert "🛑" not in await cell.inner_text()
    new_skip = page.locator("#logs-tbody tr").filter(has_text="daily-limit")
    await expect(new_skip.locator("td").nth(7).locator("small")).to_have_attribute("title", unsafe_reason)
    assert await page.evaluate("window.ruleSkipInjected === undefined")
    failure = page.locator("#logs-tbody tr").filter(has_text="smtp-failed")
    await expect(failure.locator("td").nth(7).locator(".badge-danger")).to_have_text("失败")
    assert await page.locator('script[src*="notifications.js"]').get_attribute("src") == "/static/js/notifications.js?v=rule-skip-1"
    preview = Path("logs/notification-rule-skip-20261016/notifications.png")
    preview.parent.mkdir(parents=True, exist_ok=True)
    await page.screenshot(path=str(preview), animations="disabled")
    assert not errors


@pytest.mark.parametrize("status, count, subject", [
    ("failed", 1, "smtp-failed"),
    ("skipped", 3, "daily-limit"),
    ("success", 1, "delivered"),
])
async def test_notification_status_filters_separate_skips_from_failures(
    notification_skip_browser, status, count, subject,
):
    from playwright.async_api import expect

    page, url, requests, errors, _ = notification_skip_browser
    await open_notification_logs(page, url)
    await page.locator("#log-filter-status").select_option(status)
    async with page.expect_response(lambda response:
        urlparse(response.url).path == "/api/notifications/logs" and
        parse_qs(urlparse(response.url).query).get("status") == [status]
    ):
        await page.locator("#tab-logs").get_by_role("button", name="查询", exact=True).click()
    await expect(page.locator("#logs-tbody tr")).to_have_count(count)
    await expect(page.locator("#logs-tbody")).to_contain_text(subject)
    last = next(item for item in reversed(requests) if item.path == "/api/notifications/logs")
    assert parse_qs(last.query)["status"] == [status]
    assert "success" not in parse_qs(last.query)
    await page.locator("#tab-logs").get_by_role("button", name="重置", exact=True).click()
    await expect(page.locator("#logs-tbody tr")).to_have_count(5)
    await expect(page.locator("#log-filter-status")).to_have_value("")
    assert not errors


async def test_incident_detail_distinguishes_deliveries_failures_and_skips(notification_skip_browser):
    from playwright.async_api import expect

    page, url, _, errors, unsafe_reason = notification_skip_browser
    await page.goto(url + "/ui/alerts")
    await page.locator("#inc-toggle-7").click()
    detail = page.locator("#inc-detail-body-7")
    await expect(detail.locator("tbody tr")).to_have_count(5)
    summary = detail.locator("div").first
    await expect(summary).to_contain_text("已送达 1 条，发送失败 1 条，规则跳过 3 条")
    assert "该故障共提醒" not in await summary.inner_text()
    row = detail.locator("tbody tr").filter(has_text="daily-limit")
    await expect(row.locator(".badge-secondary")).to_have_text("规则跳过")
    await expect(row.locator("td").last).to_have_attribute("title", unsafe_reason)
    assert "#6c757d" in await row.locator("td").last.get_attribute("style")
    assert await page.evaluate("window.ruleSkipInjected === undefined")
    await expect(detail.locator("tbody tr").filter(has_text="smtp-failed").locator(".badge-danger")).to_have_text("发送失败")
    assert not errors


async def test_legacy_counter_recalculation_reports_corrected_counts(notification_skip_browser):
    page, url, _, errors, _ = notification_skip_browser
    await open_notification_logs(page, url)
    messages = []
    async def accept(dialog):
        messages.append(dialog.message)
        await dialog.accept()
    page.on("dialog", accept)
    await page.evaluate("recalculateStats()")
    assert len(messages) == 2
    assert "默认邮箱: 5→2 次, 失败 4→1" in messages[-1]
    assert "undefined" not in messages[-1]
    assert not errors
