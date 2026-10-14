"""自动升级回归：真实 SQLite 表验证保留数据，MySQL 元数据/锁用适配器隔离。"""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, inspect, text

from app.models import base
from app.models.schema import SchemaMigrationError, ensure_schema


class ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar(self):
        return self.value


class SchemaConnection:
    """把元数据查询映射至真实 SQLite；MySQL 特有引擎/命名锁只记录行为。"""
    def __init__(self):
        self.engine = create_engine("sqlite:///:memory:")
        self.raw = self.engine.connect()
        self.table_engine = "InnoDB"
        self.statements = []
        self.lock = asyncio.Lock()
        self.lock_result = 1
        self.fail_on = None
        self.invalidated = False
        self.created_under_lock = []

    async def execute(self, statement, params=None):
        sql = str(statement)
        params = params or {}
        self.statements.append((sql, params))
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("ALTER denied")
        if sql == "SELECT DATABASE()":
            return ScalarResult("isolated_test")
        if "GET_LOCK" in sql:
            if self.lock_result == 1:
                await self.lock.acquire()
            return ScalarResult(self.lock_result)
        if "RELEASE_LOCK" in sql:
            self.lock.release()
            return ScalarResult(1)
        if "information_schema.columns" in sql:
            columns = inspect(self.raw).get_columns(params["t"])
            return ScalarResult(sum(c["name"] == params["c"] for c in columns))
        if "information_schema.statistics" in sql:
            indexes = inspect(self.raw).get_indexes(params["t"])
            return ScalarResult(sum(i["name"] == params["i"] for i in indexes))
        if "SELECT ENGINE" in sql:
            return ScalarResult(self.table_engine)
        if "information_schema.tables" in sql:
            return ScalarResult(int(inspect(self.raw).has_table("alert_incidents")))
        if sql == "ALTER TABLE alert_incidents ENGINE=InnoDB":
            self.table_engine = "InnoDB"
            return ScalarResult(None)
        return self.raw.execute(text(sql.split(" COMMENT ", 1)[0]), params)

    async def run_sync(self, callback):
        self.created_under_lock.append(self.lock.locked())
        # 让另一个启动任务确实有机会进入 GET_LOCK 并等待。
        await asyncio.sleep(0)
        return callback(self.raw)

    async def commit(self):
        self.raw.commit()

    async def invalidate(self):
        self.invalidated = True
        if self.lock.locked():
            self.lock.release()

    @asynccontextmanager
    async def connect(self):
        yield self

    def seed_legacy(self, *, partial=False):
        self.raw.execute(text("CREATE TABLE alert_logs (id INTEGER PRIMARY KEY, message TEXT)"))
        self.raw.execute(text("CREATE TABLE notification_logs (id INTEGER PRIMARY KEY, message TEXT)"))
        self.raw.execute(text("INSERT INTO notification_logs VALUES (7, 'historical reminder')"))
        if partial:
            self.raw.execute(text("ALTER TABLE notification_logs ADD COLUMN incident_id INT NULL"))
            self.raw.execute(text("UPDATE notification_logs SET incident_id=23 WHERE id=7"))
        self.raw.commit()

    def ddl_count(self):
        return sum(sql.startswith(("ALTER TABLE", "CREATE INDEX")) for sql, _ in self.statements)


@pytest.fixture
def schema_store():
    conn = SchemaConnection()
    try:
        yield conn
    finally:
        conn.raw.close()
        conn.engine.dispose()


@pytest.mark.parametrize("partial", [False, True])
async def test_legacy_upgrade_preserves_rows_and_is_repeatable(schema_store, partial):
    conn = schema_store
    conn.seed_legacy(partial=partial)
    added = await ensure_schema(conn, create_tables=False)
    assert added >= 6 - partial
    row = conn.raw.execute(text(
        "SELECT id, message, incident_id, notify_seq FROM notification_logs"
    )).one()
    assert tuple(row) == (7, "historical reminder", 23 if partial else None, None)
    for table in ("alert_logs", "notification_logs"):
        assert {"incident_id", "notify_seq"} <= {c["name"] for c in inspect(conn.raw).get_columns(table)}
        assert f"ix_{table}_incident_id" in {i["name"] for i in inspect(conn.raw).get_indexes(table)}
    first_ddl_count = conn.ddl_count()
    assert await ensure_schema(conn, create_tables=False) == 0
    assert conn.ddl_count() == first_ddl_count
    assert not conn.lock.locked()
    assert all(conn.created_under_lock)


async def test_fresh_install_creates_all_models_under_upgrade_lock(schema_store):
    conn = schema_store
    assert await ensure_schema(conn) == 0
    assert {"admins", "nodes", "samples", "notification_logs", "alert_incidents"} <= set(inspect(conn.raw).get_table_names())
    assert conn.created_under_lock == [True]
    assert not conn.lock.locked()


async def test_engine_conversion_and_missing_index_are_repaired(schema_store):
    conn = schema_store
    await ensure_schema(conn)
    conn.table_engine = "MyISAM"
    conn.raw.execute(text("DROP INDEX ix_alert_incidents_next_notify_at"))
    assert await ensure_schema(conn) == 2
    assert conn.table_engine == "InnoDB"


@pytest.mark.parametrize("lock_result", [0, None])
async def test_lock_failure_performs_no_ddl(schema_store, lock_result):
    conn = schema_store
    conn.lock_result = lock_result
    with pytest.raises(SchemaMigrationError, match="升级锁"):
        await ensure_schema(conn)
    assert conn.ddl_count() == 0
    assert conn.created_under_lock == []


async def test_failed_upgrade_releases_lock_and_can_resume(schema_store):
    conn = schema_store
    conn.seed_legacy()
    conn.fail_on = "ALTER TABLE notification_logs"
    with pytest.raises(SchemaMigrationError) as caught:
        await ensure_schema(conn, create_tables=False)
    assert "ALTER denied" in str(caught.value.__cause__)
    assert not conn.lock.locked()
    conn.fail_on = None
    assert await ensure_schema(conn, create_tables=False) > 0
    assert await ensure_schema(conn, create_tables=False) == 0


async def test_release_failure_discards_connection(schema_store):
    conn = schema_store
    conn.fail_on = "RELEASE_LOCK"
    with pytest.raises(SchemaMigrationError):
        await ensure_schema(conn)
    assert conn.invalidated


async def test_concurrent_startups_share_lock_and_only_migrate_once(schema_store):
    conn = schema_store
    conn.seed_legacy()
    counts = await asyncio.gather(
        ensure_schema(conn, create_tables=False),
        ensure_schema(conn, create_tables=False),
    )
    assert sorted(counts)[0] == 0
    assert sorted(counts)[1] > 0
    assert not conn.lock.locked()


async def test_init_db_completes_schema_before_creating_sessions(monkeypatch):
    calls = []
    engine = object()
    monkeypatch.setattr(base, "get_engine", lambda **kwargs: engine)
    async def upgrade(received):
        assert received is engine
        calls.append("schema")
    def sessions(*args, **kwargs):
        calls.append("sessions")
        return object()
    monkeypatch.setattr("app.models.schema.ensure_schema", upgrade)
    monkeypatch.setattr(base, "async_sessionmaker", sessions)
    monkeypatch.setattr(base, "_AsyncSessionLocal", None)
    assert await base.init_db() is engine
    assert calls == ["schema", "sessions"]


async def test_init_db_without_create_tables_never_runs_schema_ddl(monkeypatch):
    engine = object()
    upgrade = AsyncMock()
    monkeypatch.setattr(base, "get_engine", lambda **kwargs: engine)
    monkeypatch.setattr(base, "async_sessionmaker", lambda *args, **kwargs: object())
    monkeypatch.setattr(base, "_AsyncSessionLocal", None)
    monkeypatch.setattr("app.models.schema.ensure_schema", upgrade)
    assert await base.init_db(create_tables=False) is engine
    upgrade.assert_not_awaited()


async def test_application_stops_on_schema_failure_before_loading_business_data(monkeypatch):
    from app import main
    from app.services.config import AppConfig
    repository = SimpleNamespace(get_all_nodes=AsyncMock())
    monkeypatch.setattr(base, "init_db", AsyncMock(side_effect=SchemaMigrationError("ALTER denied")))
    monkeypatch.setattr("app.repositories.mysql_repository.get_mysql_repository", lambda: repository)
    monkeypatch.setattr(main, "_db_available", True)
    with pytest.raises(SchemaMigrationError):
        await main._init_database(AppConfig())
    assert main._db_available is False
    repository.get_all_nodes.assert_not_awaited()


async def test_bootstrap_uses_same_upgrade_path(monkeypatch):
    from scripts import bootstrap_database
    from app.services.config import AppConfig
    engine = SimpleNamespace(dispose=AsyncMock())
    upgrade = AsyncMock(return_value=4)
    monkeypatch.setattr(bootstrap_database, "get_engine", lambda **kwargs: engine)
    monkeypatch.setattr(bootstrap_database, "ensure_schema", upgrade)
    assert await bootstrap_database.run_phase2_migration(AppConfig()) == 4
    upgrade.assert_awaited_once_with(engine, create_tables=False, report=bootstrap_database.log)
    engine.dispose.assert_awaited_once()
