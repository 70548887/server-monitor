"""启动历史查询：执行真实 SQL 验证结果，并检查避免慢半连接的查询形状。"""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.models.base import Base
from app.models.node import NodeModel
from app.models.sample import SampleModel
from app.repositories.mysql_repository import MySQLRepository


NOW = datetime(2026, 10, 14, 6, 0, 0)
FIELDS = {"node_name", "timestamp", "success", "latency_ms", "status_code", "error_type"}


@pytest.fixture
def startup_store(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[NodeModel.__table__, SampleModel.__table__])
    statements = []

    @event.listens_for(engine, "before_cursor_execute")
    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    class AsyncSessionAdapter:
        def __init__(self, session):
            self.session = session

        async def execute(self, statement):
            return self.session.execute(statement)

    @asynccontextmanager
    async def context():
        with Session(engine) as session:
            yield AsyncSessionAdapter(session)

    repo = MySQLRepository(use_retry=False)
    monkeypatch.setattr(repo, "_get_session_manager", context)
    monkeypatch.setattr("app.repositories.mysql_repository.utcnow", lambda: NOW)
    try:
        yield engine, repo, statements
    finally:
        engine.dispose()


def sample(name, timestamp, latency=1.0):
    return SampleModel(
        node_name=name, url="https://test.invalid/health", timestamp=timestamp,
        latency_ms=latency, status_code=200, success=True,
        error_type=None, error_message="unused error details", response_size=100,
    )


async def test_startup_samples_full_window_projection_and_node_filter(startup_store):
    engine, repo, statements = startup_store
    cutoff = NOW - timedelta(hours=24)
    with Session(engine) as session:
        session.add_all([
            NodeModel(name="active", url="https://test.invalid"),
            NodeModel(name="disabled", url="https://test.invalid", enabled=False),
            NodeModel(name="deleted", url="https://test.invalid", deleted_at=NOW),
        ])
        session.add_all([
            sample("active", NOW - timedelta(minutes=1), 3.0),
            sample("active", cutoff, 1.0),
            sample("active", cutoff - timedelta(microseconds=1), 99.0),
            # 相同秒的两条有效记录都必须保留。
            sample("active", cutoff, 2.0),
            sample("disabled", NOW - timedelta(hours=1), 4.0),
            sample("deleted", NOW - timedelta(hours=1), 99.0),
            sample("orphan", NOW - timedelta(hours=1), 99.0),
        ])
        session.commit()

    rows = await repo.get_recent_samples()

    assert len(rows) == 4
    assert all(set(row) == FIELDS for row in rows)
    assert {row["latency_ms"] for row in rows} == {1.0, 2.0, 3.0, 4.0}
    assert [row["timestamp"] for row in rows] == sorted(row["timestamp"] for row in rows)
    assert sum(row["timestamp"] == cutoff for row in rows) == 2
    assert {row["node_name"] for row in rows} == {"active", "disabled"}
    assert len(statements) == 2
    query = statements[1].lower()
    assert query.count("select") == 1
    assert "from samples" in query and "from nodes" not in query
    assert "samples.node_name in (" in query
    assert "samples.timestamp >=" in query
    projection = query.split("from samples")[0]
    assert len(projection.removeprefix("select ").split(",")) == 6
    assert "samples.url" not in projection and "samples.error_message" not in projection


@pytest.mark.parametrize("deleted_only", [False, True])
async def test_no_active_nodes_skips_sample_query(startup_store, deleted_only):
    engine, repo, statements = startup_store
    with Session(engine) as session:
        if deleted_only:
            session.add(NodeModel(name="gone", url="https://test.invalid", deleted_at=NOW))
        session.add(sample("gone", NOW))
        session.commit()

    assert await repo.get_recent_samples() == []
    assert len(statements) == 1
    assert "from samples" not in statements[0].lower()


async def test_custom_history_window_and_no_truncation(startup_store):
    engine, repo, _ = startup_store
    cutoff = NOW - timedelta(hours=2)
    with Session(engine) as session:
        session.add(NodeModel(name="node", url="https://test.invalid"))
        session.add(sample("node", cutoff - timedelta(seconds=1), 99.0))
        session.add_all(sample("node", cutoff + timedelta(seconds=i)) for i in range(3001))
        session.commit()

    rows = await repo.get_recent_samples(hours=2)
    assert len(rows) == 3001
    assert rows[0]["timestamp"] == cutoff
    assert all(row["latency_ms"] == 1.0 for row in rows)


@pytest.mark.parametrize("error", [
    OperationalError("SELECT", {}, RuntimeError("database unavailable")),
    RuntimeError("unexpected adapter error"),
])
async def test_query_failure_keeps_existing_empty_fallback(monkeypatch, error, caplog):
    session = AsyncMock()
    session.execute.side_effect = error

    @asynccontextmanager
    async def context():
        yield session

    repo = MySQLRepository(use_retry=False)
    monkeypatch.setattr(repo, "_get_session_manager", context)
    assert await repo.get_recent_samples() == []
    assert "recent samples" in caplog.text
