"""Node renames preserve runtime history without external probes or notifications."""

import asyncio
from collections import deque
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest

from app.repositories import redis_repository as redis_module
from app.services.alerting import AlertingService, NodeAlertState
from app.services.availability import AvailabilityService, HeartbeatRecord
from app.services.config import AlertingConfig
from app.services.node_identity import node_mutation_lock
from app.utils.timeutil import utcnow


def make_record(name, *, success=True, timestamp=None):
    return HeartbeatRecord(
        node_name=name,
        timestamp=timestamp or utcnow(),
        success=success,
        latency_ms=25 if success else 500,
        status_code=200 if success else 0,
    )


def test_availability_rename_preserves_records_statistics_and_capacity():
    service = AvailabilityService()
    now = utcnow()
    service.add_records([
        make_record("old", timestamp=now - timedelta(minutes=2)),
        make_record("old", success=False, timestamp=now - timedelta(minutes=1)),
    ])
    records = service._records["old"]
    stats = service.get_stats("old")
    before = dict(vars(stats))

    service.rename_node("old", "renamed 节点")

    assert "old" not in service._records
    assert "old" not in service.get_all_stats()
    assert service._records["renamed 节点"] is records
    assert records.maxlen == service.MAX_RECORDS_PER_NODE
    assert [r.success for r in records] == [True, False]
    assert all(r.node_name == "renamed 节点" for r in records)
    assert service.get_stats("renamed 节点") is stats
    assert vars(stats) == {**before, "node_name": "renamed 节点"}
    service.add_record(make_record("renamed 节点"))
    assert service.get_stats("renamed 节点").total_checks_24h == 3
    assert service.get_stats("old").total_checks_24h == 0


def test_availability_rename_does_not_mix_stale_target_history():
    service = AvailabilityService()
    service.add_record(make_record("old", success=False))
    service.add_records([make_record("new"), make_record("other")])

    service.rename_node("old", "new")

    assert service.get_stats("new").total_checks_24h == 1
    assert service.get_stats("new").current_status == "down"
    assert service.get_stats("other").total_checks_24h == 1
    service.rename_node("missing", "new")
    assert "new" not in service._records
    assert "new" not in service.get_all_stats()


def test_availability_same_name_rename_is_noop():
    service = AvailabilityService()
    service.add_record(make_record("same"))
    records = service._records["same"]
    stats = service.get_stats("same")

    service.rename_node("same", "same")

    assert service._records["same"] is records
    assert service.get_stats("same") is stats


def make_alert_service(persist=True):
    service = AlertingService(AlertingConfig(suppress_persist=persist))
    state = NodeAlertState(
        node_name="old",
        consecutive_failures=7,
        last_success=False,
        last_check_at=utcnow(),
        suppressed_alerts={"latency_high": datetime.now()},
        latencies=deque([10, 30, 1000], maxlen=100),
    )
    service._states["old"] = state
    return service, state


async def test_alert_rename_retains_counts_latency_and_suppression(monkeypatch):
    service, state = make_alert_service()
    before = service.get_node_state("old")
    latencies = state.latencies
    suppressions = state.suppressed_alerts
    service._states["new"] = NodeAlertState("new", consecutive_failures=99)
    repo = Mock()
    repo.is_connected.return_value = True

    async def verify_redis_migration(old, new):
        assert old == "old" and new == "new"
        assert service.get_node_state("old") is None
        assert service.get_node_state("new")["consecutive_failures"] == 7
        # A different thread must be able to acquire the service lock while
        # Redis I/O is in flight; an RLock test on this same thread would pass.
        def lock_is_free():
            acquired = service._lock.acquire(blocking=False)
            if acquired:
                service._lock.release()
            return acquired
        assert await asyncio.to_thread(lock_is_free)

    repo.rename_node_state = AsyncMock(side_effect=verify_redis_migration)
    monkeypatch.setattr(redis_module, "get_redis_repository", lambda: repo)

    await service.rename_node("old", "new")

    assert service._states["new"] is state
    assert service.get_node_state("new") == {**before, "node_name": "new"}
    assert state.latencies is latencies
    assert list(latencies) == [10, 30, 1000]
    assert latencies.maxlen == 100
    assert state.suppressed_alerts is suppressions
    assert service._is_suppressed(state, "latency_high")
    repo.rename_node_state.assert_awaited_once_with("old", "new")


@pytest.mark.parametrize("persist,connected", [(False, True), (True, False)])
async def test_alert_rename_skips_unavailable_persistence(monkeypatch, persist, connected):
    service, state = make_alert_service(persist)
    repo = Mock()
    repo.is_connected.return_value = connected
    repo.rename_node_state = AsyncMock()
    factory = Mock(return_value=repo)
    monkeypatch.setattr(redis_module, "get_redis_repository", factory)

    await service.rename_node("old", "new")

    assert service._states["new"] is state
    repo.rename_node_state.assert_not_awaited()
    if not persist:
        factory.assert_not_called()


async def test_alert_rename_redis_failure_keeps_renamed_memory(monkeypatch, caplog):
    service, state = make_alert_service()
    repo = Mock()
    repo.is_connected.return_value = True
    repo.rename_node_state = AsyncMock(side_effect=RuntimeError("redis offline"))
    monkeypatch.setattr(redis_module, "get_redis_repository", lambda: repo)

    await service.rename_node("old", "new")

    assert service._states["new"] is state
    assert "old" not in service._states
    assert "keeping renamed memory state" in caplog.text


async def test_alert_rename_with_no_memory_still_moves_redis(monkeypatch):
    service = AlertingService(AlertingConfig(suppress_persist=True))
    repo = Mock()
    repo.is_connected.return_value = True
    repo.rename_node_state = AsyncMock()
    monkeypatch.setattr(redis_module, "get_redis_repository", lambda: repo)
    service._states["new"] = NodeAlertState("new", consecutive_failures=99)

    await service.rename_node("old", "new")

    assert "new" not in service._states
    repo.rename_node_state.assert_awaited_once_with("old", "new")


async def test_alert_same_name_rename_is_noop(monkeypatch):
    service, state = make_alert_service()
    factory = Mock(side_effect=AssertionError("same-name rename must not use Redis"))
    monkeypatch.setattr(redis_module, "get_redis_repository", factory)

    await service.rename_node("old", "old")

    assert service._states["old"] is state
    factory.assert_not_called()


async def test_redis_rename_uses_bound_keys_and_atomic_ttl_preserving_rename():
    repo = redis_module.RedisRepository()
    repo._prefix = "test-only:"
    client = Mock()
    client.eval = AsyncMock(return_value=1)
    repo._get_client = AsyncMock(return_value=client)
    old = "old'); redis.call('FLUSHALL'); --"
    new = "renamed 中文/节点"

    assert await repo.rename_node_state(old, new) is True

    client.eval.assert_awaited_once()
    script, numkeys, *keys = client.eval.await_args.args
    assert old not in script and new not in script
    assert numkeys == 10
    assert keys == [
        f"test-only:{kind.format(name)}"
        for kind in (
            "alert:suppress:{}:node_down",
            "alert:suppress:{}:node_recovery",
            "alert:suppress:{}:latency_high",
            "node:{}:status",
            "node:{}:errors",
        )
        for name in (old, new)
    ]
    # Redis RENAME carries the source expiry and overwrites the destination.
    assert "redis.call('RENAME', KEYS[i], KEYS[i + 1])" in script
    assert "redis.call('DEL', KEYS[i + 1])" in script
    assert "SET" not in script and "EXPIRE" not in script


async def test_redis_same_name_rename_never_connects():
    repo = redis_module.RedisRepository()
    repo._get_client = AsyncMock(side_effect=AssertionError("unexpected connection"))

    assert await repo.rename_node_state("same", "same") is True
    repo._get_client.assert_not_awaited()


@pytest.mark.parametrize("fail_connect", [True, False])
async def test_redis_rename_failure_is_best_effort(fail_connect, caplog):
    repo = redis_module.RedisRepository()
    client = Mock()
    client.eval = AsyncMock(side_effect=RuntimeError("redis unavailable"))
    repo._get_client = AsyncMock(
        return_value=client,
        side_effect=RuntimeError("connect failed") if fail_connect else None,
    )

    assert await repo.rename_node_state("old", "new") is False
    assert "using renamed memory state" in caplog.text


async def test_node_mutation_lock_serializes_workers():
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()
    order = []
    lock = node_mutation_lock()
    assert node_mutation_lock() is lock

    async def first():
        async with node_mutation_lock():
            order.append("first entered")
            first_entered.set()
            await release_first.wait()
            order.append("first left")

    async def second():
        second_started.set()
        async with node_mutation_lock():
            order.append("second entered")

    first_task = asyncio.create_task(first())
    await first_entered.wait()
    second_task = asyncio.create_task(second())
    await second_started.wait()
    assert order == ["first entered"]
    release_first.set()
    await asyncio.gather(first_task, second_task)
    assert order == ["first entered", "first left", "second entered"]


def test_node_mutation_lock_is_isolated_per_event_loop():
    async def get_lock():
        lock = node_mutation_lock()
        async with lock:
            assert node_mutation_lock() is lock
        return lock

    first = asyncio.run(get_lock())
    second = asyncio.run(get_lock())
    assert first is not second


async def test_serialize_node_mutation_holds_lock_and_preserves_signature():
    import inspect
    from app.services.node_identity import serialize_node_mutation

    @serialize_node_mutation
    async def change_name(old_name: str, new_name: str):
        assert node_mutation_lock().locked()
        return old_name, new_name

    assert list(inspect.signature(change_name).parameters) == ["old_name", "new_name"]
    assert await change_name("old", new_name="new") == ("old", "new")
    assert not node_mutation_lock().locked()
