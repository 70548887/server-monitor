"""节点重命名：真实 SQLite 事务验证数据保留、名称冲突和中途失败回滚。"""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import Session

from app.models.base import Base
from app.models.node import NodeModel
from app.models.sample import AggregateModel, SampleModel
from app.models.notification_channel import AlertIncidentModel, AlertLogModel, NotificationLogModel
from app.models.schema import NODE_TRANSACTION_TABLES
from app.repositories.mysql_repository import MySQLRepository, NodeNameConflict
from test_mysql_integration import mysql_store  # noqa: F401 — 独立临时数据库夹具

HISTORIES = (SampleModel, AggregateModel, AlertIncidentModel, AlertLogModel, NotificationLogModel)
START = datetime(2026, 10, 14, 8)


def history(model, name):
    shared = dict(node_name=name)
    if model is SampleModel:
        return model(**shared, url="http://test.invalid", timestamp=START, latency_ms=12.5)
    if model is AggregateModel:
        return model(**shared, window_start=START, window_end=START + timedelta(minutes=5),
                     total_count=5, success_count=4, availability=0.8)
    if model is AlertIncidentModel:
        return model(**shared, alert_type="node_down", started_at=START, notify_count=3,
                     next_notify_at=START + timedelta(hours=5), total_failures=19)
    if model is AlertLogModel:
        return model(**shared, alert_type="node_down", message="old-node historical message",
                     incident_id=17, notify_seq=3)
    return model(**shared, channel_id=2, channel_name="email", alert_type="node_down",
                 subject="old-node historical subject", message="old-node historical message",
                 incident_id=17, notify_seq=3, success=True)


@pytest.fixture
async def rename_store(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    class AsyncSessionAdapter:
        def __init__(self, session):
            self.sync_session = session

        def get_bind(self):
            return self.sync_session.get_bind()

        def add(self, value):
            self.sync_session.add(value)

        def add_all(self, values):
            self.sync_session.add_all(values)

        async def execute(self, statement, params=None):
            return self.sync_session.execute(statement, params)

        async def flush(self):
            self.sync_session.flush()

        async def commit(self):
            self.sync_session.commit()

        async def rollback(self):
            self.sync_session.rollback()

    @asynccontextmanager
    async def sessions():
        with Session(engine, expire_on_commit=False) as raw_session:
            session = AsyncSessionAdapter(raw_session)
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    repo = MySQLRepository(use_retry=False)
    monkeypatch.setattr(repo, "_get_session_manager", sessions)
    async with sessions() as session:
        session.add(NodeModel(id=8, name="old-node", url="http://test.invalid", region="old"))
        session.add_all([history(model, "old-node") for model in HISTORIES])
        session.add(history(NotificationLogModel, None))
        await session.commit()
    try:
        yield engine, sessions, repo
    finally:
        engine.dispose()


async def rows_by_model(sessions):
    async with sessions() as session:
        snapshots = {}
        for model in (NodeModel,) + HISTORIES:
            snapshots[model] = [dict(row) for row in (await session.execute(
                select(model.__table__).order_by(model.id)
            )).mappings().all()]
        return snapshots


async def test_rename_preserves_id_history_content_and_incident_schedule(rename_store):
    _, sessions, repo = rename_store
    before = await rows_by_model(sessions)
    assert await repo.rename_node("old-node", "新名称", {
        "region": "new", "tags": ["renamed"], "timeout_ms": 5000,
        "expect": {"status": 204, "body_contains": "ok"},
    })
    after = await rows_by_model(sessions)
    node = after[NodeModel][0]
    assert node["id"] == 8 and node["name"] == "新名称"
    assert node["region"] == "new" and node["tags"] == ["renamed"]
    assert node["timeout_ms"] == 5000
    assert (node["expect_status"], node["expect_body_contains"]) == (204, "ok")
    for model in HISTORIES:
        expected = [{**row, "node_name": "新名称" if row["node_name"] else None} for row in before[model]]
        for actual, original in zip(after[model], expected):
            # ORM updated_at is expected to advance on the changed incident row.
            actual.pop("updated_at", None)
            original.pop("updated_at", None)
            assert actual == original


@pytest.mark.parametrize("deleted", [False, True])
async def test_rename_rejects_existing_and_soft_deleted_name_without_changes(rename_store, deleted):
    _, sessions, repo = rename_store
    async with sessions() as session:
        session.add(NodeModel(name="taken", url="http://other.invalid",
                              deleted_at=START if deleted else None))
        await session.commit()
    before = await rows_by_model(sessions)
    with pytest.raises(NodeNameConflict, match="名称已存在"):
        await repo.rename_node("old-node", "taken", {"region": "changed"})
    assert await rows_by_model(sessions) == before


@pytest.mark.parametrize("model", HISTORIES)
async def test_rename_rejects_orphan_history_without_merging(rename_store, model):
    _, sessions, repo = rename_store
    async with sessions() as session:
        session.add(history(model, "past-node"))
        await session.commit()
    before = await rows_by_model(sessions)
    with pytest.raises(NodeNameConflict, match="历史记录"):
        await repo.rename_node("old-node", "past-node", {})
    assert await rows_by_model(sessions) == before


async def test_case_only_rename_and_same_name_are_allowed(rename_store):
    _, sessions, repo = rename_store
    assert await repo.rename_node("old-node", "OLD-NODE", {})
    assert await repo.rename_node("OLD-NODE", "OLD-NODE", {"enabled": False})
    rows = await rows_by_model(sessions)
    assert rows[NodeModel][0]["id"] == 8
    assert rows[NodeModel][0]["enabled"] is False
    assert all(row["node_name"] in ("OLD-NODE", None) for model in HISTORIES for row in rows[model])


async def test_missing_node_does_not_change_history(rename_store):
    _, sessions, repo = rename_store
    before = await rows_by_model(sessions)
    assert not await repo.rename_node("missing", "new", {})
    assert await rows_by_model(sessions) == before


async def test_late_history_write_failure_rolls_back_every_table(rename_store):
    engine, sessions, repo = rename_store
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TRIGGER fail_last_history BEFORE UPDATE ON notification_logs "
            "BEGIN SELECT RAISE(ABORT, 'test history write failure'); END"
        ))
    before = await rows_by_model(sessions)
    assert not await repo.rename_node("old-node", "new", {"region": "changed"})
    assert await rows_by_model(sessions) == before


async def test_unique_constraint_race_becomes_conflict_and_rolls_back(rename_store):
    _, sessions, repo = rename_store
    original_context = repo._get_session_manager

    @asynccontextmanager
    async def racing_context():
        async with original_context() as session:
            def concurrent_insert(sync_session, flush_context, instances):
                sync_session.connection().execute(text(
                    "INSERT INTO nodes (name, url) VALUES ('raced-name', 'http://other.invalid')"
                ))
            event.listen(session.sync_session, "before_flush", concurrent_insert, once=True)
            yield session

    repo._get_session_manager = racing_context
    before = await rows_by_model(sessions)
    with pytest.raises(NodeNameConflict):
        await repo.rename_node("old-node", "raced-name", {})
    assert await rows_by_model(sessions) == before


@pytest.mark.parametrize("legacy_table", NODE_TRANSACTION_TABLES)
async def test_mysql_nontransactional_table_is_rejected_before_mutation(legacy_table):
    session = SimpleNamespace(
        get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="mysql")),
        execute=AsyncMock(return_value=SimpleNamespace(all=lambda: [
            (table, "MyISAM" if table == legacy_table else "InnoDB") for table in NODE_TRANSACTION_TABLES
        ])),
    )

    @asynccontextmanager
    async def context():
        yield session

    repo = MySQLRepository(use_retry=False)
    repo._get_session_manager = context
    assert not await repo.rename_node("old-node", "new", {})
    assert session.execute.await_count == 1


def test_new_node_and_history_tables_use_innodb():
    for model in (NodeModel,) + HISTORIES:
        assert model.__table__.dialect_options["mysql"]["engine"] == "InnoDB"


@pytest.mark.skipif(os.getenv("RUN_MYSQL_INTEGRATION") != "1", reason="显式启用独立 MySQL 测试库")
async def test_real_mysql_collation_transaction_and_engine_upgrade(mysql_store):
    from app.models.schema import ensure_schema

    engine, sessions, repo = mysql_store
    async with engine.begin() as connection:
        for table in NODE_TRANSACTION_TABLES:
            await connection.execute(text(
                f"ALTER TABLE {table} CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            ))
    async with sessions() as session:
        session.add(NodeModel(id=8, name="old-node", url="http://test.invalid"))
        session.add_all([history(model, "old-node") for model in HISTORIES])
        session.add(NodeModel(name="TaKeN", url="http://other.invalid", deleted_at=START))
        await session.commit()
    assert await repo.rename_node("old-node", "OLD-NODE", {})
    with pytest.raises(NodeNameConflict):
        await repo.rename_node("OLD-NODE", "taken", {})
    before = await rows_by_model(sessions)
    async with engine.begin() as connection:
        await connection.execute(text(
            "CREATE TRIGGER fail_history BEFORE UPDATE ON notification_logs "
            "FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'isolated rollback test'"
        ))
    assert not await repo.rename_node("OLD-NODE", "new-node", {"region": "changed"})
    assert await rows_by_model(sessions) == before
    async with engine.begin() as connection:
        await connection.execute(text("DROP TRIGGER fail_history"))
        await connection.execute(text("ALTER TABLE notification_logs ENGINE=MyISAM"))
    assert not await repo.rename_node("OLD-NODE", "new-node", {})
    assert await rows_by_model(sessions) == before
    assert await ensure_schema(engine) == 1
    assert await repo.rename_node("OLD-NODE", "new-node", {})
    after = await rows_by_model(sessions)
    assert after[NodeModel][0]["id"] == 8 and after[NodeModel][0]["name"] == "new-node"
    assert all(row["node_name"] == "new-node" for model in HISTORIES for row in after[model])
    assert after[AlertIncidentModel][0]["notify_count"] == 3
    assert after[NotificationLogModel][0]["incident_id"] == 17
    assert after[NotificationLogModel][0]["message"] == "old-node historical message"
