"""数据库重载的状态发布、异常隔离和下一轮检测条件。"""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import OperationalError

from app.routes import nodes
from app.schemas.node import NodeStatus
from app.services.config import AppConfig, NodeConfig
from app.services.scheduler import SchedulerService
from app.repositories.mysql_repository import MySQLRepository


def row(name="existing", **changes):
    value = dict(name=name, url="http://node.invalid/health", region="new-region",
                 tags=["updated"], enabled=True, timeout_ms=3500,
                 expect={"status": 204, "body_contains": "ready"})
    value.update(changes)
    return value


@pytest.fixture
def reload_state(monkeypatch):
    cfg = AppConfig(nodes=[NodeConfig(name="yaml-only", url="http://example.invalid")])
    cached = NodeStatus(name="existing", url="http://node.invalid/health", region="old",
                        tags=["old"], enabled=True, last_status_code=503,
                        last_latency_ms=41, last_error="offline", consecutive_failures=4,
                        last_check_at=datetime(2026, 10, 13), availability=.95, error_rate=.05)
    removed = cached.model_copy(update={"name": "removed"}, deep=True)
    cache = {"existing": cached, "removed": removed}
    monkeypatch.setattr(nodes, "_node_status_cache", cache)
    monkeypatch.setattr(nodes, "get_config", lambda: cfg)
    monkeypatch.setattr("app.services.scheduler.get_config", lambda: cfg)
    repo = SimpleNamespace(get_all_nodes=AsyncMock(return_value=[row()]))
    monkeypatch.setattr(nodes, "_get_db_repository", lambda: repo)
    monkeypatch.setattr("app.repositories.mysql_repository.get_mysql_repository", lambda: repo)
    return cfg, cache, repo


async def test_reload_updates_metadata_and_keeps_same_target_metrics(reload_state):
    cfg, before, repo = reload_state
    repo.get_all_nodes.return_value = [row(enabled=False), row("added")]
    output = await nodes.reload_node_config()
    assert output["source"] == "database"
    assert (output["count"], output["enabled_count"]) == (2, 1)
    repo.get_all_nodes.assert_awaited_once_with(strict=True)
    refreshed = nodes._node_status_cache
    assert set(refreshed) == {"existing", "added"}
    status = refreshed["existing"]
    assert (status.region, status.tags, status.enabled) == ("new-region", ["updated"], False)
    for key in ("last_status_code", "last_latency_ms", "last_error", "last_check_at", "consecutive_failures", "availability", "error_rate"):
        assert getattr(status, key) == getattr(before["existing"], key)
    assert before["existing"].enabled is True  # 不部分修改旧快照
    assert cfg.nodes[0].timeout_ms == 3500
    assert cfg.nodes[0].expect.status == 204
    assert cfg.nodes[0].expect.body_contains == "ready"
    assert refreshed["added"].last_check_at is None
    assert "yaml-only" not in refreshed


async def test_url_change_resets_old_target_results(reload_state):
    _, _, repo = reload_state
    repo.get_all_nodes.return_value = [row(url="http://replacement.invalid")]
    await nodes.reload_node_config()
    status = nodes._node_status_cache["existing"]
    assert status.url == "http://replacement.invalid"
    assert status.last_check_at is None
    assert status.last_status_code is None
    assert status.consecutive_failures == 0


async def test_empty_database_is_success_and_clears_old_cache(reload_state):
    cfg, _, repo = reload_state
    repo.get_all_nodes.return_value = []
    output = await nodes.reload_node_config()
    assert output["count"] == output["enabled_count"] == 0
    assert cfg.nodes == [] and nodes._node_status_cache == {}


@pytest.mark.parametrize("failure", ["unavailable", "query", "invalid", "duplicate"])
async def test_failed_reload_preserves_entire_original_state(reload_state, monkeypatch, failure):
    cfg, cache, repo = reload_state
    old_config = cfg.nodes
    if failure == "unavailable":
        monkeypatch.setattr(nodes, "_get_db_repository", lambda: None)
    elif failure == "query":
        repo.get_all_nodes.side_effect = RuntimeError("internal database detail")
    elif failure == "invalid":
        repo.get_all_nodes.return_value = [row(), row("broken", timeout_ms=0)]
    else:
        repo.get_all_nodes.return_value = [row(), row()]
    with pytest.raises(HTTPException) as error:
        await nodes.reload_node_config()
    assert error.value.status_code == 503
    assert "internal database detail" not in str(error.value.detail)
    assert cfg.nodes is old_config
    assert nodes._node_status_cache is cache
    assert cache["existing"].region == "old"


async def test_reload_keeps_heartbeat_result_arriving_during_query(reload_state):
    _, old_cache, repo = reload_state
    query_started, query_continue = asyncio.Event(), asyncio.Event()
    async def read(**kwargs):
        query_started.set()
        await query_continue.wait()
        return [row()]
    repo.get_all_nodes.side_effect = read
    task = asyncio.create_task(nodes.reload_node_config())
    await query_started.wait()
    old_cache["existing"].last_latency_ms = 77
    query_continue.set()
    await task
    assert nodes._node_status_cache["existing"].last_latency_ms == 77


async def test_scheduler_uses_reloaded_nested_expect_and_timeout(reload_state):
    cfg, _, repo = reload_state
    repo.get_all_nodes.return_value = [row(), row("disabled", enabled=False)]
    await nodes.reload_node_config()
    heartbeat = SimpleNamespace(check_nodes=AsyncMock(return_value=[]))
    scheduler = SchedulerService(heartbeat_service=heartbeat)
    await scheduler.run_once()
    arguments = heartbeat.check_nodes.call_args.kwargs["nodes"]
    assert len(arguments) == 1
    assert arguments[0].name == "existing"
    assert arguments[0].timeout_ms == 3500
    assert arguments[0].expect.status == 204
    assert arguments[0].expect.body_contains == "ready"
    repo.get_all_nodes.side_effect = RuntimeError("offline")
    fallback = await scheduler._get_nodes_from_db()
    assert fallback == [cfg.nodes[0]]


@pytest.mark.parametrize("failure", [RuntimeError("offline"), OperationalError("SELECT", {}, Exception("offline"))])
async def test_repository_strict_read_exposes_failure(monkeypatch, failure):
    repo = MySQLRepository(use_retry=False)
    @asynccontextmanager
    async def broken_session():
        raise failure
        yield
    monkeypatch.setattr(repo, "_get_session_manager", broken_session)
    assert await repo.get_all_nodes() == []
    with pytest.raises(type(failure)):
        await repo.get_all_nodes(strict=True)


def test_legacy_flat_expect_record_is_supported():
    values = row()
    del values["expect"]
    values.update(expect_status=201, expect_body_contains="created")
    node = NodeConfig.from_database(values)
    assert (node.expect.status, node.expect.body_contains) == (201, "created")


async def test_reload_http_requires_auth_and_returns_database_source(reload_state, monkeypatch):
    from test_routes import make_client, make_authed_client
    async with make_client() as client:
        response = await client.post("/api/nodes/reload")
        assert response.status_code == 401
    reload_state[2].get_all_nodes.assert_not_awaited()
    async with make_authed_client(monkeypatch) as client:
        response = await client.post("/api/nodes/reload")
        assert response.status_code == 200
        assert response.json()["source"] == "database"
        response = await client.get("/api/nodes")
        assert response.json()[0]["region"] == "new-region"


async def test_reload_http_database_failure_returns_503(reload_state, monkeypatch):
    from test_routes import make_authed_client
    reload_state[2].get_all_nodes.side_effect = RuntimeError("offline")
    async with make_authed_client(monkeypatch) as client:
        response = await client.post("/api/nodes/reload")
    assert response.status_code == 503


async def test_manual_probe_and_detail_use_database_expect(reload_state, monkeypatch):
    from app.routes import ui
    from app.services.heartbeat import HeartbeatService, HeartbeatResult

    _, _, repo = reload_state
    repo.get_node_by_name = AsyncMock(return_value=row())
    monkeypatch.setattr(ui, "_get_db_repository", lambda: repo)
    displayed = await ui._get_nodes_from_db()
    assert displayed[0].expect.status == 204
    assert displayed[0].expect.body_contains == "ready"

    captured = []
    async def probe(self, node):
        captured.append(node)
        return HeartbeatResult(node_name=node.name, url=node.url, timestamp=datetime(2026, 10, 13), status_code=204, latency_ms=5, success=True)
    monkeypatch.setattr(HeartbeatService, "check_node", probe)
    output = await nodes.ping_node("existing")
    assert output["success"]
    assert captured[0] == displayed[0]


async def test_scheduler_fallback_uses_latest_database_snapshot(reload_state):
    cfg, _, repo = reload_state
    repo.get_all_nodes.return_value = [row(), row("disabled", enabled=False)]
    scheduler = SchedulerService()
    loaded = await scheduler._get_nodes_from_db()
    assert [node.name for node in loaded] == ["existing"]
    assert [node.name for node in cfg.nodes] == ["existing", "disabled"]
    repo.get_all_nodes.side_effect = RuntimeError("offline")
    assert await scheduler._get_nodes_from_db() == loaded


async def test_scheduler_empty_database_does_not_restore_yaml_on_failure(reload_state):
    cfg, _, repo = reload_state
    repo.get_all_nodes.return_value = []
    scheduler = SchedulerService()
    assert await scheduler._get_nodes_from_db() == []
    repo.get_all_nodes.side_effect = RuntimeError("offline")
    assert await scheduler._get_nodes_from_db() == []
    assert cfg.nodes == []
