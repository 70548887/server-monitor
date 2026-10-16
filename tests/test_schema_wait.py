"""启动结构升级的可观察等待和取消回收；仅使用测试连接，不接触业务数据库。"""

import asyncio

import pytest

from app.models import schema


class Scalar:
    def __init__(self, value):
        self.value = value

    def scalar(self):
        return self.value


class LockConnection:
    def __init__(self, clock, results):
        self.clock = clock
        self.results = iter(results)
        self.waits = []
        self.invalidated = False
        self.released = False

    async def execute(self, statement, params=None):
        sql = str(statement)
        if sql == "SELECT DATABASE()":
            return Scalar("startup_wait_test")
        if "GET_LOCK" in sql:
            self.waits.append(params["timeout"])
            result = next(self.results)
            if isinstance(result, BaseException):
                raise result
            if result == 0:
                self.clock[0] += params["timeout"]
            return Scalar(result)
        if "RELEASE_LOCK" in sql:
            self.released = True
            return Scalar(1)
        pytest.fail(f"Unexpected SQL: {sql}")

    async def invalidate(self):
        self.invalidated = True


@pytest.fixture
def lock_clock(monkeypatch):
    monkeypatch.delenv("MONITOR_SCHEMA_LOCK_WAIT_SECONDS", raising=False)
    clock = [0]
    monkeypatch.setattr(schema, "perf_counter", lambda: clock[0])
    return clock


async def test_waits_in_observable_chunks_until_previous_instance_finishes(lock_clock):
    conn = LockConnection(lock_clock, [0, 0, 1])
    messages = []
    async with schema.schema_lock(conn, report=messages.append):
        assert lock_clock[0] == 60
    assert conn.waits == [30, 30, 30]
    assert ["已等待 30 秒" in messages[0], "已等待 60 秒" in messages[1]] == [True, True]
    assert conn.released and not conn.invalidated


async def test_custom_deadline_caps_last_lock_wait(lock_clock, monkeypatch):
    monkeypatch.setenv("MONITOR_SCHEMA_LOCK_WAIT_SECONDS", "45")
    conn = LockConnection(lock_clock, [0, 1])
    async with schema.schema_lock(conn):
        pass
    assert conn.waits == [30, 15]
    assert conn.released


async def test_lock_deadline_explains_competing_migration_and_discards_connection(lock_clock, monkeypatch):
    monkeypatch.setenv("MONITOR_SCHEMA_LOCK_WAIT_SECONDS", "31")
    conn = LockConnection(lock_clock, [0, 0])
    messages = []
    with pytest.raises(schema.SchemaMigrationError, match="不要反复重启"):
        async with schema.schema_lock(conn, report=messages.append):
            pytest.fail("A blocked migration must not enter DDL")
    assert conn.waits == [30, 1]
    assert "已等待 31 秒" in messages[-1]
    assert conn.invalidated and not conn.released


async def test_null_lock_response_fails_without_retry(lock_clock):
    conn = LockConnection(lock_clock, [None, 1])
    with pytest.raises(schema.SchemaMigrationError, match="返回 NULL"):
        async with schema.schema_lock(conn):
            pytest.fail("A failed lock must not enter DDL")
    assert conn.waits == [30]
    assert conn.invalidated and not conn.released


@pytest.mark.parametrize("value", ["", "0", "-1", "86401", "3.0", "invalid"])
async def test_invalid_lock_wait_config_fails_before_database_access(monkeypatch, value):
    monkeypatch.setenv("MONITOR_SCHEMA_LOCK_WAIT_SECONDS", value)
    conn = LockConnection([0], [])
    with pytest.raises(schema.SchemaMigrationError, match="MONITOR_SCHEMA_LOCK_WAIT_SECONDS"):
        async with schema.schema_lock(conn):
            pytest.fail("Invalid configuration must not enter DDL")
    assert not conn.waits


def test_default_lock_wait_and_valid_maximum(monkeypatch):
    monkeypatch.delenv("MONITOR_SCHEMA_LOCK_WAIT_SECONDS", raising=False)
    assert schema._schema_lock_wait_seconds() == 1800
    monkeypatch.setenv("MONITOR_SCHEMA_LOCK_WAIT_SECONDS", "86400")
    assert schema._schema_lock_wait_seconds() == 86400


async def test_acquisition_error_discards_connection(lock_clock):
    conn = LockConnection(lock_clock, [RuntimeError("connection lost")])
    with pytest.raises(RuntimeError, match="connection lost"):
        async with schema.schema_lock(conn):
            pytest.fail("A failed lock must not enter DDL")
    assert conn.invalidated and not conn.released


async def test_cancelled_lock_acquisition_discards_possibly_locked_connection(monkeypatch):
    monkeypatch.delenv("MONITOR_SCHEMA_LOCK_WAIT_SECONDS", raising=False)
    started = asyncio.Event()

    class WaitingConnection(LockConnection):
        async def execute(self, statement, params=None):
            if "GET_LOCK" in str(statement):
                # 服务器可能已拿锁，但客户端响应尚未完成。
                started.set()
                await asyncio.Future()
            return await super().execute(statement, params)

    conn = WaitingConnection([0], [])

    async def acquire():
        async with schema.schema_lock(conn):
            pytest.fail("A cancelled lock must not enter DDL")

    task = asyncio.create_task(acquire())
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert conn.invalidated and not conn.released


@pytest.mark.parametrize("outcome", ["success", "error", "cancel"])
async def test_ddl_progress_does_not_end_operation_and_ticker_stops_on_exit(monkeypatch, outcome):
    monkeypatch.setattr(schema, "SCHEMA_PROGRESS_INTERVAL_SECONDS", 0.001)
    finished = asyncio.Event()
    reported = asyncio.Event()
    messages = []

    class DDLConnection:
        async def execute(self, statement):
            assert str(statement) == "ALTER TABLE samples ENGINE=InnoDB"
            await finished.wait()
            if outcome == "error":
                raise RuntimeError("ALTER failed")

    def report(message):
        messages.append(message)
        reported.set()

    task = asyncio.create_task(schema._convert_table_engine(DDLConnection(), "samples", report))
    try:
        await asyncio.wait_for(reported.wait(), timeout=1)
        assert not task.done()
        assert "samples 仍在转换为 InnoDB，已等待" in messages[0]
        if outcome == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            finished.set()
            if outcome == "error":
                with pytest.raises(RuntimeError, match="ALTER failed"):
                    await task
            else:
                await task
        count = len(messages)
        await asyncio.sleep(0.005)
        assert len(messages) == count
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
