"""节点编辑的真实 Chromium 回归；页面使用真实模板，节点 API 由测试截留。"""

import asyncio
import copy
import json
import os
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit
from unittest.mock import AsyncMock

import pytest

from test_login_dialog import login_browser


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_BROWSER_TESTS") != "1", reason="显式启用浏览器验收"
)

ORIGINAL_NAME = "演示节点 O'Reilly &?#%"
RENAMED_NAME = "更新节点 中文 &?#%"


@pytest.fixture
async def node_edit_browser(login_browser, monkeypatch):
    from app.routes import ui
    from app.services.auth import AuthService
    from app.services.config import NodeConfig

    page, url, _, _, errors = login_browser
    state = {
        "node": {
            "name": ORIGINAL_NAME,
            "url": "https://node.example.invalid/health",
            "region": "test-east",
            "tags": ["demo"],
            "timeout_ms": 2000,
            "enabled": True,
            "expect": {"status": 200, "body_contains": "ready"},
        },
        "submissions": [],
        "reads": [],
        "dialogs": [],
        "conflict_name": "已存在的演示节点",
        "hold_get": False,
        "hold_put": False,
        "get_arrived": asyncio.Event(),
        "put_arrived": asyncio.Event(),
        "release_get": asyncio.Event(),
        "release_put": asyncio.Event(),
    }

    async def nodes_snapshot():
        return [NodeConfig.from_database(copy.deepcopy(state["node"]))]

    monkeypatch.setattr(ui, "_get_nodes_from_db", nodes_snapshot)
    monkeypatch.setattr(AuthService, "get_session", AsyncMock(
        return_value={"admin_id": 1, "username": "browser-fixture"},
    ))
    await page.context.add_cookies([{
        "name": "session_id", "value": "node-edit-fixture", "url": url,
    }])

    async def dialog_handler(dialog):
        state["dialogs"].append(dialog.message)
        await dialog.accept()

    page.on("dialog", dialog_handler)

    async def json_response(route, payload, status=200):
        await route.fulfill(
            status=status, content_type="application/json",
            body=json.dumps(payload, ensure_ascii=False),
        )

    async def node_api(route):
        request = route.request
        path = urlsplit(request.url).path
        if path == "/api/nodes/stats/availability":
            return await json_response(route, {"nodes": []})
        if path.endswith("/availability/trend"):
            return await json_response(route, {"data": []})
        requested_name = unquote(path.removeprefix("/api/nodes/"))
        if request.method == "GET":
            state["reads"].append(request.url)
            state["get_arrived"].set()
            if state["hold_get"]:
                await state["release_get"].wait()
            if requested_name != state["node"]["name"]:
                return await json_response(route, {"detail": "节点不存在"}, 404)
            return await json_response(route, state["node"])
        if request.method == "PUT":
            submitted = request.post_data_json
            state["submissions"].append((request.url, submitted))
            state["put_arrived"].set()
            if state["hold_put"]:
                await state["release_put"].wait()
            if requested_name != state["node"]["name"]:
                return await json_response(route, {"detail": "节点不存在"}, 404)
            if submitted.get("name") == state["conflict_name"]:
                return await json_response(route, {
                    "detail": {"message": "该节点名称已存在，请使用其他名称"},
                }, 409)
            state["node"].update(submitted)
            return await json_response(route, state["node"])
        await json_response(route, {"detail": "测试未预期的请求"}, 405)

    await page.route("**/api/nodes/**", node_api)
    await page.route("**/healthz", lambda route: route.fulfill(
        status=200, content_type="application/json", body='{"uptime_seconds":10}',
    ))
    try:
        yield page, url, state, errors
    finally:
        state["release_get"].set()
        state["release_put"].set()
        page.remove_listener("dialog", dialog_handler)


async def open_node_editor(page, url):
    from playwright.async_api import expect

    await page.goto(url + "/ui/nodes")
    await page.locator("#nodes-table").get_by_role(
        "button", name="编辑", exact=True,
    ).click()
    await expect(page.locator("#editNodeModal")).to_be_visible()
    await expect(page.locator("#editName")).to_have_value(ORIGINAL_NAME)
    await expect(page.locator('#editNodeForm button[type="submit"]')).to_be_enabled()


async def test_rename_visible_name_uses_original_encoded_endpoint(node_edit_browser):
    from playwright.async_api import expect

    page, url, state, errors = node_edit_browser
    await open_node_editor(page, url)
    await expect(page.locator("#editName")).to_be_editable()
    await page.locator("#editName").fill(RENAMED_NAME)
    await page.locator("#editUrl").fill("https://updated.example.invalid/status")
    await page.locator("#editRegion").fill("test-west")
    await page.locator("#editTags").fill("web, renamed")
    await page.locator("#editTimeout").fill("4500")
    await page.locator("#editExpectStatus").fill("202")
    await page.locator("#editExpectBody").fill("accepted")
    await page.locator("#editEnabled").select_option("false")
    preview = Path("logs/node-rename-20261014/edit-preview.png")
    preview.parent.mkdir(parents=True, exist_ok=True)
    await page.screenshot(path=str(preview), animations="disabled")
    await page.locator('#editNodeForm button[type="submit"]').click()

    await expect(page.locator("#nodes-table .name-cell")).to_have_text(RENAMED_NAME)
    await expect(page.locator("#editNodeModal")).not_to_be_visible()
    assert len(state["submissions"]) == 1
    endpoint, payload = state["submissions"][0]
    expected_endpoint = url + "/api/nodes/" + quote(ORIGINAL_NAME, safe="'!()*")
    assert endpoint == expected_endpoint
    assert state["reads"] == [expected_endpoint]
    assert payload == {
        "name": RENAMED_NAME,
        "url": "https://updated.example.invalid/status",
        "region": "test-west",
        "tags": ["web", "renamed"],
        "timeout_ms": 4500,
        "enabled": False,
        "expect": {"status": 202, "body_contains": "accepted"},
    }
    assert not errors, errors


async def test_name_conflict_retains_input_and_allows_retry(node_edit_browser):
    from playwright.async_api import expect

    page, url, state, errors = node_edit_browser
    await open_node_editor(page, url)
    await page.locator("#editName").fill(state["conflict_name"])
    await page.locator("#editRegion").fill("pending-region")
    await page.locator('#editNodeForm button[type="submit"]').click()
    await expect(page.locator('#editNodeForm button[type="submit"]')).to_be_enabled()
    await expect(page.locator("#editNodeModal")).to_be_visible()
    await expect(page.locator("#editName")).to_have_value(state["conflict_name"])
    await expect(page.locator("#editRegion")).to_have_value("pending-region")
    await expect(page.locator("#nodes-table .name-cell")).to_have_text(ORIGINAL_NAME)
    assert any("名称已存在" in message for message in state["dialogs"])

    await page.locator("#editName").fill(RENAMED_NAME)
    await page.locator('#editNodeForm button[type="submit"]').click()
    await expect(page.locator("#nodes-table .name-cell")).to_have_text(RENAMED_NAME)
    assert len(state["submissions"]) == 2
    assert state["submissions"][0][0] == state["submissions"][1][0]
    assert state["node"]["region"] == "pending-region"
    assert not errors, errors


async def test_existing_fields_still_edit_without_renaming(node_edit_browser):
    from playwright.async_api import expect

    page, url, state, errors = node_edit_browser
    await open_node_editor(page, url)
    await page.locator("#editTags").fill("changed, retained-name")
    await page.locator("#editTimeout").fill("3200")
    await page.locator('#editNodeForm button[type="submit"]').click()
    await expect(page.locator("#editNodeModal")).not_to_be_visible()
    await expect(page.locator("#nodes-table .name-cell")).to_have_text(ORIGINAL_NAME)
    assert len(state["submissions"]) == 1
    assert state["node"]["name"] == ORIGINAL_NAME
    assert state["node"]["tags"] == ["changed", "retained-name"]
    assert state["node"]["timeout_ms"] == 3200
    assert not errors, errors


async def test_save_disabled_while_loading_and_saving(node_edit_browser):
    from playwright.async_api import expect

    page, url, state, errors = node_edit_browser
    state["hold_get"] = True
    await page.goto(url + "/ui/nodes")
    await page.locator("#nodes-table").get_by_role("button", name="编辑", exact=True).click()
    await asyncio.wait_for(state["get_arrived"].wait(), 5)
    save = page.locator('#editNodeForm button[type="submit"]')
    await expect(save).to_be_disabled()
    assert not state["submissions"]
    state["release_get"].set()
    await expect(save).to_be_enabled()
    await expect(page.locator("#editName")).to_have_value(ORIGINAL_NAME)

    # HTML 必填约束阻止空名称提交。
    await page.locator("#editName").fill("")
    await save.click()
    assert not state["submissions"]
    assert await page.locator("#editName").evaluate("input => input.validity.valueMissing")

    state["hold_put"] = True
    await page.locator("#editName").fill(RENAMED_NAME)
    await save.click()
    await asyncio.wait_for(state["put_arrived"].wait(), 5)
    await expect(save).to_be_disabled()
    assert len(state["submissions"]) == 1
    state["release_put"].set()
    await expect(page.locator("#nodes-table .name-cell")).to_have_text(RENAMED_NAME)
    assert len(state["submissions"]) == 1
    assert not errors, errors


@pytest.mark.parametrize("viewport", [
    {"width": 375, "height": 667},
    {"width": 1200, "height": 700},
])
async def test_editor_fits_short_viewport_and_scrolls_to_save(node_edit_browser, viewport):
    from playwright.async_api import expect

    page, url, state, errors = node_edit_browser
    await page.set_viewport_size(viewport)
    await open_node_editor(page, url)
    content = page.locator("#editNodeModal .modal-content")
    box = await content.bounding_box()
    assert box is not None
    assert box["x"] >= 0 and box["y"] >= 0
    assert box["x"] + box["width"] <= viewport["width"] + 1
    assert box["y"] + box["height"] <= viewport["height"] + 1
    await expect(page.locator("#editName")).to_be_in_viewport()
    await page.locator("#editName").fill(RENAMED_NAME)
    assert await content.evaluate("el => el.scrollHeight > el.clientHeight")
    if viewport["width"] == 1200:
        preview = Path("logs/node-rename-20261014/edit-preview.png")
        preview.parent.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(preview), animations="disabled")
    save = page.locator('#editNodeForm button[type="submit"]')
    await save.scroll_into_view_if_needed()
    await expect(save).to_be_in_viewport()
    await expect(save).to_be_enabled()
    await save.click()
    await expect(page.locator("#nodes-table .name-cell")).to_have_text(RENAMED_NAME)
    await expect(page.locator("#editNodeModal")).not_to_be_visible()
    assert len(state["submissions"]) == 1
    assert state["submissions"][0][1]["name"] == RENAMED_NAME
    assert not errors, errors
