"""显式启用的真实 MySQL 集成测试；只写本次创建的临时测试库。

RUN_MYSQL_INTEGRATION=1 python -m pytest tests/test_mysql_integration.py
"""
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import pytest
from sqlalchemy import URL, text, select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.models.base import Base
from app.models.admin import Admin
from app.models.sample import SampleModel, AggregateModel
from app.services.config import get_config
from app.repositories.mysql_repository import MySQLRepository

pytestmark = pytest.mark.skipif(os.getenv("RUN_MYSQL_INTEGRATION") != "1", reason="显式启用真实MySQL测试")


@pytest.fixture
async def mysql_store(monkeypatch):
    c = get_config().database
    name = "server_monitor_test_" + uuid.uuid4().hex[:12]
    url = URL.create("mysql+aiomysql", username=c.mysql_user, password=c.mysql_password,
                     host=c.mysql_host, port=c.mysql_port, query={"charset": "utf8mb4"})
    admin = create_async_engine(url, hide_parameters=True)
    async with admin.begin() as conn:
        await conn.execute(text(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4"))
    engine = create_async_engine(url.set(database=name), hide_parameters=True, connect_args={"init_command": "SET default_storage_engine=InnoDB"})
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        @asynccontextmanager
        async def context():
            async with sessions() as session:
                try:
                    yield session
                    await session.commit()
                except Exception:
                    await session.rollback()
                    raise
        repo = MySQLRepository(use_retry=False)
        monkeypatch.setattr(repo, "_get_session_manager", context)
        for module in ("incident", "alerting", "notification"):
            monkeypatch.setattr(f"app.services.{module}.get_session_context", context)
        yield engine, sessions, repo
    finally:
        await engine.dispose()
        # 名称由本 fixture 生成，且只有 CREATE 成功才进入清理路径。
        assert name.startswith("server_monitor_test_") and len(name) == 32
        async with admin.begin() as conn:
            await conn.execute(text(f"DROP DATABASE `{name}`"))
        await admin.dispose()


async def test_real_mysql_utc_buckets_exact_replacement_and_idempotency(mysql_store, monkeypatch):
    engine, sessions, repo = mysql_store
    base = datetime(2026, 10, 12, 10)
    monkeypatch.setattr("app.repositories.mysql_repository.utcnow", lambda: base + timedelta(minutes=12))
    async with sessions() as s:
        for name, minutes in [("a", 0), ("b", 5)]:
            for i, latency in enumerate([1., 5., 9.]):
                s.add(SampleModel(node_name=name, url="http://test.invalid", timestamp=base + timedelta(minutes=minutes, seconds=i), latency_ms=latency, success=True))
        # 当前未结束窗口不应被汇总。
        s.add(SampleModel(node_name="a", url="http://test.invalid", timestamp=base + timedelta(minutes=11), latency_ms=999, success=False))
        for name, minutes, width in [("a",0,5),("b",5,5),("a",5,5),("a",0,60)]:
            s.add(AggregateModel(node_name=name, window_start=base+timedelta(minutes=minutes), window_end=base+timedelta(minutes=minutes+width), window_minutes=width, total_count=99))
        await s.commit()
    for tz in ("+08:00", "+00:00"):
        async with engine.connect() as conn:
            await conn.execute(text("SET SESSION time_zone=:tz"), {"tz":tz})
        result = await repo.rollup_samples_to_aggregates(hours=3)
        assert result == {"windows":2, "written":2, "samples":6}
        async with sessions() as s:
            rows = (await s.execute(select(AggregateModel))).scalars().all()
            assert len(rows) == 4
            changed = [r for r in rows if r.total_count == 3]
            assert {r.window_start for r in changed} == {base,base+timedelta(minutes=5)}
            assert all((r.latency_p50_ms,round(r.latency_p95_ms,2),round(r.latency_p99_ms,2)) == (5.,8.6,8.92) for r in changed)


async def test_real_mysql_event_delivery_and_recovery(mysql_store):
    from app.services.incident import IncidentService
    from app.services.notification import NotificationLogService
    from app.services.alerting import AlertingService
    from app.services.config import AlertingConfig
    from app.services.heartbeat import HeartbeatResult
    start = datetime(2026,10,12)
    incident = await IncidentService.open_incident("node", "node_down", "http://test.invalid", start)
    updated = await IncidentService.record_notification(incident["id"], (0,60,240),720,now=start)
    assert (await IncidentService.should_notify(updated,start+timedelta(minutes=5)))[0] is False
    await NotificationLogService.add_log(1,"test","node_down",True,incident_id=incident["id"],notify_seq=1)
    svc=AlertingService(AlertingConfig(suppress_persist=False))
    events=await svc.evaluate(HeartbeatResult(node_name="node",url="http://test.invalid",timestamp=start+timedelta(minutes=5),success=True,latency_ms=1,status_code=200))
    assert len(events)==1 and events[0].incident_duration_s==300
    assert await IncidentService.get_open_incident("node","node_down") is None


async def test_bootstrap_upgrades_legacy_schema_idempotently(mysql_store, monkeypatch):
    from scripts import bootstrap_database
    engine, _, _ = mysql_store
    async with engine.begin() as conn:
        await conn.execute(text("ALTER TABLE alert_incidents ENGINE=MyISAM"))
        await conn.execute(text("ALTER TABLE notification_logs DROP COLUMN incident_id, DROP COLUMN notify_seq"))
        await conn.execute(text("ALTER TABLE alert_logs DROP COLUMN incident_id, DROP COLUMN notify_seq"))
    monkeypatch.setattr(bootstrap_database, "get_engine", lambda **kwargs: engine)
    assert await bootstrap_database.run_phase2_migration(get_config()) >= 5
    assert await bootstrap_database.run_phase2_migration(get_config()) == 0
    async with engine.connect() as conn:
        assert (await conn.execute(text("SELECT ENGINE FROM information_schema.tables WHERE table_schema=DATABASE() AND table_name='alert_incidents'"))).scalar_one() == "InnoDB"


async def test_database_node_reload_and_next_probe(mysql_store, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from app.models.node import NodeModel
    from app.routes import nodes, ui
    from app.schemas.node import NodeStatus
    from app.services.config import AppConfig, NodeConfig
    from app.services.scheduler import SchedulerService
    from test_routes import make_authed_client

    _, sessions, repo = mysql_store
    async with sessions() as s:
        s.add_all([
            NodeModel(name="db-active", url="http://test.invalid/health", timeout_ms=4500,
                      expect_status=202, expect_body_contains="ready", region="new", tags=["db"], enabled=True),
            NodeModel(name="db-disabled", url="http://test.invalid/disabled", enabled=False),
            NodeModel(name="db-deleted", url="http://test.invalid/deleted", deleted_at=datetime(2026, 10, 13)),
        ])
        await s.commit()
    cfg = AppConfig(nodes=[NodeConfig("yaml-only", "http://example.invalid")])
    old = NodeStatus(name="db-active", url="http://test.invalid/health", region="old", tags=[], enabled=True, last_latency_ms=19)
    monkeypatch.setattr(nodes, "_node_status_cache", {"db-active": old, "removed": old.model_copy(update={"name":"removed"})})
    monkeypatch.setattr(nodes, "get_config", lambda: cfg)
    monkeypatch.setattr(nodes, "_get_db_repository", lambda: repo)
    monkeypatch.setattr(ui, "_get_db_repository", lambda: repo)
    monkeypatch.setattr("app.repositories.mysql_repository.get_mysql_repository", lambda: repo)
    monkeypatch.setattr("app.services.scheduler.get_config", lambda: cfg)
    async with make_authed_client(monkeypatch) as client:
        response = await client.post("/api/nodes/reload")
        assert response.status_code == 200
        assert response.json()["count"] == 2
        assert response.json()["enabled_count"] == 1
        listed = (await client.get("/api/nodes")).json()
        assert {n["name"] for n in listed} == {"db-active", "db-disabled"}
        status = nodes._node_status_cache["db-active"]
        assert status.last_latency_ms == 19 and status.region == "new"
        assert "removed" not in nodes._node_status_cache
        page = await client.get("/ui/nodes/db-active")
        assert page.status_code == 200 and "202" in page.text and "ready" in page.text
    heartbeat = SimpleNamespace(check_nodes=AsyncMock(return_value=[]))
    await SchedulerService(heartbeat_service=heartbeat).run_once()
    targets = heartbeat.check_nodes.call_args.kwargs["nodes"]
    assert len(targets) == 1
    assert (targets[0].name, targets[0].timeout_ms, targets[0].expect.status, targets[0].expect.body_contains) == ("db-active",4500,202,"ready")
