"""节点改名 API、缓存同步和心跳回调竞态。"""
import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from httpx import AsyncClient, ASGITransport

from app.routes import nodes
from app.repositories.mysql_repository import NodeNameConflict
from app.schemas.node import NodeUpdate, NodeStatus
from app.services.availability import AvailabilityService, HeartbeatRecord
from app.services.alerting import AlertingService
from app.services.config import AppConfig, NodeConfig, AlertingConfig
from app.services.heartbeat import HeartbeatResult

OLD = "old-node"
NEW = "新节点 &?#%"


@pytest.fixture
def rename_state(monkeypatch):
    runtime = NodeConfig(name=OLD, url="http://node.invalid/health")
    cfg = AppConfig(nodes=[runtime], alerting=AlertingConfig(suppress_persist=False))
    status = NodeStatus(name=OLD, url=runtime.url, region="default", tags=[], enabled=True,
                        last_status_code=200, last_latency_ms=12, last_check_at=datetime(2026,10,14), consecutive_failures=2)
    availability = AvailabilityService()
    availability.add_record(HeartbeatRecord(OLD, datetime(2026,10,14), True, 12))
    alerts = AlertingService(cfg.alerting)
    alerts._get_or_create_state(OLD).consecutive_failures = 2
    alerts.evaluate_batch = AsyncMock(return_value=[])
    row = {"id":1, "name":OLD, "url":runtime.url, "region":"default", "tags":[],
           "enabled":True, "timeout_ms":2000, "expect":{"status":200, "body_contains":None}}
    saved = []
    async def rename(old, new, data):
        row.update(data, name=new)
        for sample in saved:
            if sample["node_name"] == old:
                sample["node_name"] = new
        return True
    repo = SimpleNamespace(
        get_node_by_name=AsyncMock(side_effect=lambda name: dict(row) if name == row["name"] else None),
        get_all_nodes=AsyncMock(side_effect=lambda: [dict(row)]),
        rename_node=AsyncMock(side_effect=rename), update_node=AsyncMock(return_value=True),
        save_samples_batch=AsyncMock(side_effect=lambda samples: saved.extend(samples) or len(samples)),
    )
    monkeypatch.setattr(nodes, "get_config", lambda: cfg)
    monkeypatch.setattr(nodes, "_node_status_cache", {OLD:status})
    monkeypatch.setattr(nodes, "_get_db_repository", lambda: repo)
    monkeypatch.setattr(nodes, "get_availability_service", lambda: availability)
    monkeypatch.setattr("app.services.alerting.get_alerting_service", lambda: alerts)
    monkeypatch.setattr("app.repositories.mysql_repository.get_mysql_repository", lambda: repo)
    return cfg, runtime, status, availability, alerts, repo, saved


async def test_rename_preserves_live_status_history_and_alert_state(rename_state):
    cfg, original, status, availability, alerts, repo, _ = rename_state
    result = await nodes.update_node(OLD, NodeUpdate(name=NEW, region="new-region"))
    assert result.name == NEW and result.region == "new-region"
    assert result.last_latency_ms == status.last_latency_ms
    assert [node.name for node in cfg.nodes] == [NEW]
    assert cfg.nodes[0].probe_revision != original.probe_revision
    assert set(nodes._node_status_cache) == {NEW}
    assert availability.get_memory_stats()["total_records"] == 1
    assert set(availability.get_all_stats()) == {NEW}
    assert alerts._states[NEW].consecutive_failures == 2 and OLD not in alerts._states
    repo.rename_node.assert_awaited_once_with(OLD, NEW, {"region":"new-region"})
    stale = HeartbeatResult(node_name=OLD, url=original.url, timestamp=datetime(2026,10,14),
                            success=True, status_code=200, latency_ms=1, probe_revision=original.probe_revision)
    assert not nodes.update_node_status_from_result(OLD, stale)


@pytest.mark.parametrize("conflict", [True, False])
async def test_failed_rename_preserves_every_runtime_cache(rename_state, conflict):
    cfg, original, status, availability, alerts, repo, _ = rename_state
    if conflict:
        repo.rename_node.side_effect = NodeNameConflict("节点名称已存在")
    else:
        repo.rename_node.side_effect = None
        repo.rename_node.return_value = False
    with pytest.raises(HTTPException) as caught:
        await nodes.update_node(OLD, NodeUpdate(name=NEW))
    assert caught.value.status_code == (409 if conflict else 503)
    assert cfg.nodes == [original]
    assert nodes._node_status_cache == {OLD:status}
    assert set(availability.get_all_stats()) == {OLD}
    assert set(alerts._states) == {OLD}


async def test_unchanged_name_uses_existing_update_path(rename_state):
    _, _, _, _, _, repo, _ = rename_state
    await nodes.update_node(OLD, NodeUpdate(name=OLD, region="other"))
    repo.update_node.assert_awaited_once_with(OLD, {"region":"other"})
    repo.rename_node.assert_not_awaited()


@pytest.mark.parametrize("name", ["", "   ", "x"*101, "a/b", "a\n"])
async def test_invalid_rename_rejected_before_writes(rename_state, name):
    *_, repo, _ = rename_state
    app = FastAPI()
    app.include_router(nodes.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.put("/api/nodes/old-node", json={"name":name})
    assert response.status_code == 422
    repo.rename_node.assert_not_awaited()
    repo.update_node.assert_not_awaited()


async def test_heartbeat_write_finishes_before_rename_and_late_old_result_is_dropped(rename_state, monkeypatch):
    from app import main
    cfg, original, _, availability, _, repo, saved = rename_state
    callbacks = []
    scheduler = SimpleNamespace(set_on_results_callback=callbacks.append, start=lambda:None, stop=lambda:None)
    monkeypatch.setattr(main, "load_config", lambda **kwargs:cfg)
    monkeypatch.setattr(main, "init_async_logging", lambda **kwargs:None)
    monkeypatch.setattr(main, "shutdown_logging", lambda:None)
    monkeypatch.setattr(main, "_init_database", AsyncMock())
    monkeypatch.setattr(main, "_init_storage_health_check", AsyncMock())
    monkeypatch.setattr(main, "_init_default_notification_channels", AsyncMock())
    monkeypatch.setattr(main, "create_default_admin", AsyncMock())
    monkeypatch.setattr(main, "get_scheduler", lambda:scheduler)
    monkeypatch.setattr(main, "get_availability_service", lambda:availability)
    monkeypatch.setattr(main, "_db_available", True)
    writing, release = asyncio.Event(), asyncio.Event()
    async def persist(samples):
        writing.set()
        await release.wait()
        saved.extend(samples)
        return len(samples)
    repo.save_samples_batch.side_effect = persist
    result = HeartbeatResult(node_name=OLD, url=original.url, timestamp=datetime(2026,10,14),
                             success=True, status_code=200, latency_ms=1, probe_revision=original.probe_revision)
    async with main.lifespan(main.app):
        callback = callbacks[0]
        heartbeat = asyncio.create_task(callback([result]))
        await asyncio.wait_for(writing.wait(), 2)
        rename = asyncio.create_task(nodes.update_node(OLD, NodeUpdate(name=NEW)))
        await asyncio.sleep(0)
        repo.rename_node.assert_not_awaited()
        release.set()
        await asyncio.wait_for(heartbeat, 2)
        await asyncio.wait_for(rename, 2)
        assert saved and all(row["node_name"] == NEW for row in saved)
        await callback([result])
        assert all(row["node_name"] == NEW for row in saved)
        assert OLD not in availability.get_all_stats()


async def test_committed_name_and_status_publish_before_optional_redis_wait(rename_state):
    cfg, _, status, availability, alerts, _, _ = rename_state
    waiting, release = asyncio.Event(), asyncio.Event()
    original = alerts.rename_node
    async def slow_cache(old, new):
        await original(old, new)
        waiting.set()
        await release.wait()
    alerts.rename_node = slow_cache
    task = asyncio.create_task(nodes.update_node(OLD, NodeUpdate(name=NEW)))
    try:
        await asyncio.wait_for(waiting.wait(), 2)
        assert set(nodes._node_status_cache) == {NEW}
        assert nodes._node_status_cache[NEW].last_latency_ms == status.last_latency_ms
        assert [node.name for node in cfg.nodes] == [NEW]
        assert set(availability.get_all_stats()) == {NEW}
    finally:
        release.set()
        await asyncio.wait_for(task, 2)
