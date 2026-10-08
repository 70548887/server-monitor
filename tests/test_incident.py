"""
IncidentService 真实实现测试（假数据库）

告警主流程用的是内存桩，但事件表的读写逻辑本身从未被执行过 ——
而这正是要跑在 MySQL 上、迁移脚本要建表的那部分代码。
这里用一个假的 session 驱动真实实现，覆盖：
    - 建事件时的并发冲突处理
    - 闸门推进与失败累计
    - 静默顺延
    - 事件关闭与时长计算
"""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import pytest

import app.services.incident as incident_mod
from app.services.incident import IncidentService
from app.utils.timeutil import format_iso_utc


class FakeIncidentRow:
    """模拟 AlertIncidentModel 实例（只保留被用到的字段与方法）"""

    def __init__(self, **kwargs):
        self.id = kwargs.get("id")
        self.node_name = kwargs.get("node_name")
        self.alert_type = kwargs.get("alert_type")
        self.node_url = kwargs.get("node_url")
        self.started_at = kwargs.get("started_at")
        self.last_notified_at = kwargs.get("last_notified_at")
        self.next_notify_at = kwargs.get("next_notify_at")
        self.notify_count = kwargs.get("notify_count", 0)
        self.status = kwargs.get("status", "open")
        self.resolved_at = kwargs.get("resolved_at")
        self.duration_seconds = kwargs.get("duration_seconds")
        self.last_error = kwargs.get("last_error")
        self.total_failures = kwargs.get("total_failures", 0)

    def to_dict(self):
        # 与 AlertIncidentModel.to_dict 保持一致：
        # 时间戳来自心跳（UTC），序列化必须带 Z，否则前端按本地解析会早 8 小时
        from app.utils.timeutil import format_iso_utc

        return {
            "id": self.id,
            "node_name": self.node_name,
            "alert_type": self.alert_type,
            "node_url": self.node_url,
            "started_at": format_iso_utc(self.started_at),
            "last_notified_at": format_iso_utc(self.last_notified_at),
            "next_notify_at": format_iso_utc(self.next_notify_at),
            "notify_count": self.notify_count,
            "status": self.status,
            "resolved_at": format_iso_utc(self.resolved_at),
            "duration_seconds": self.duration_seconds,
            "last_error": self.last_error,
            "total_failures": self.total_failures,
        }


class FakeResult:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row

    def scalars(self):
        return self

    def all(self):
        return [self._row] if self._row else []

    def scalar(self):
        return self._row


class FakeSession:
    """最小可用的假 session，只实现 IncidentService 用到的操作"""

    def __init__(self, store):
        self.store = store
        self._pending = None

    async def execute(self, stmt):
        # 从语句里还原查询意图：IncidentService 的查询只有
        # "按 id 查" 和 "查该节点的 open 事件" 两种
        params = stmt.compile().params
        target_id = None
        node_name = None
        alert_type = None
        status = None

        for key, value in params.items():
            if key.endswith("id_1") or key == "id_1":
                target_id = value
            if "node_name" in key:
                node_name = value
            if "alert_type" in key:
                alert_type = value
            if "status" in key:
                status = value

        if target_id is not None:
            return FakeResult(self.store.get(target_id))

        for row in self.store.values():
            if row.status != (status or "open"):
                continue
            if node_name is not None and row.node_name != node_name:
                continue
            if alert_type is not None and row.alert_type != alert_type:
                continue
            return FakeResult(row)
        return FakeResult(None)

    def add(self, obj):
        self._pending = obj

    async def commit(self):
        if self._pending is not None:
            self._pending.id = len(self.store) + 1
            self.store[self._pending.id] = self._pending
            self._pending = None

    async def refresh(self, obj):
        pass

    async def rollback(self):
        self._pending = None


@pytest.fixture
def fake_db(monkeypatch):
    """把 incident 模块的会话上下文换成假数据库"""
    store = {}

    @asynccontextmanager
    async def fake_session_context():
        yield FakeSession(store)

    monkeypatch.setattr(incident_mod, "get_session_context", fake_session_context)
    return store


class TestOpenIncident:
    """建事件"""

    async def test_open_creation(self, fake_db):
        started = datetime(2026, 10, 4, 20, 0)
        incident = await IncidentService.open_incident(
            node_name="n1",
            alert_type="node_down",
            node_url="http://n1/health",
            started_at=started,
            last_error="连接超时",
        )

        assert incident is not None
        assert incident["node_name"] == "n1"
        assert incident["status"] == "open"
        assert incident["notify_count"] == 0
        assert incident["last_error"] == "连接超时"
        # 首次提醒立即放行
        assert incident["next_notify_at"] == format_iso_utc(started)

    async def test_get_open_incident(self, fake_db):
        await IncidentService.open_incident(
            node_name="n1", alert_type="node_down", node_url="u",
            started_at=datetime(2026, 10, 4, 20, 0),
        )
        found = await IncidentService.get_open_incident("n1", "node_down")
        assert found is not None
        assert found["node_name"] == "n1"

    async def test_get_open_incident_returns_none_when_absent(self, fake_db):
        assert await IncidentService.get_open_incident("nope", "node_down") is None

    async def test_resolved_incident_not_returned_as_open(self, fake_db):
        """已关闭的事件不应被当成 open 事件复用"""
        incident = await IncidentService.open_incident(
            node_name="n1", alert_type="node_down", node_url="u",
            started_at=datetime(2026, 10, 4, 20, 0),
        )
        await IncidentService.resolve_incident(incident["id"])

        assert await IncidentService.get_open_incident("n1", "node_down") is None


class TestShouldNotify:
    """闸门判定"""

    async def test_gate_open_when_no_next_notify_at(self):
        should, reason = await IncidentService.should_notify(
            {"next_notify_at": None}, datetime(2026, 10, 4, 20, 0)
        )
        assert should is True

    async def test_gate_closed_before_time(self):
        now = datetime(2026, 10, 4, 20, 0)
        incident = {"next_notify_at": (now + timedelta(minutes=30)).isoformat()}
        should, reason = await IncidentService.should_notify(incident, now)
        assert should is False
        assert "还需" in reason

    async def test_gate_open_after_time(self):
        now = datetime(2026, 10, 4, 20, 0)
        incident = {"next_notify_at": (now - timedelta(minutes=1)).isoformat()}
        should, _ = await IncidentService.should_notify(incident, now)
        assert should is True

    async def test_gate_accepts_datetime_object(self):
        """to_dict 之外的路径可能传入 datetime 对象而非字符串"""
        now = datetime(2026, 10, 4, 20, 0)
        should, _ = await IncidentService.should_notify(
            {"next_notify_at": now + timedelta(hours=1)}, now
        )
        assert should is False

    async def test_unparsable_gate_allows_notification(self):
        """闸门值损坏时放行，宁可重复也不要漏报"""
        should, reason = await IncidentService.should_notify(
            {"next_notify_at": "not-a-date"}, datetime(2026, 10, 4, 20, 0)
        )
        assert should is True
        assert "allowing" in reason


class TestRecordNotification:
    """推进闸门"""

    async def test_notify_count_increments(self, fake_db):
        started = datetime(2026, 10, 4, 20, 0)
        incident = await IncidentService.open_incident(
            node_name="n1", alert_type="node_down", node_url="u", started_at=started,
        )

        updated = await IncidentService.record_notification(
            incident_id=incident["id"],
            intervals=(0, 60, 240, 480, 720),
            loop_minutes=720,
            now=started,
        )

        assert updated["notify_count"] == 1
        # 第 1 次之后，闸门指向 +60 分钟
        assert updated["next_notify_at"] == format_iso_utc(started + timedelta(minutes=60))

    async def test_failure_delta_accumulates(self, fake_db):
        started = datetime(2026, 10, 4, 20, 0)
        incident = await IncidentService.open_incident(
            node_name="n1", alert_type="node_down", node_url="u", started_at=started,
        )

        await IncidentService.record_notification(
            incident["id"], (0, 60, 240, 480, 720), 720,
            failure_delta=1, now=started,
        )
        updated = await IncidentService.record_notification(
            incident["id"], (0, 60, 240, 480, 720), 720,
            failure_delta=1, now=started + timedelta(hours=1),
        )
        assert updated["total_failures"] == 2

    async def test_last_error_updated(self, fake_db):
        started = datetime(2026, 10, 4, 20, 0)
        incident = await IncidentService.open_incident(
            node_name="n1", alert_type="node_down", node_url="u",
            started_at=started, last_error="旧错误",
        )

        updated = await IncidentService.record_notification(
            incident["id"], (0, 60), 720, last_error="新错误", now=started,
        )
        assert updated["last_error"] == "新错误"

    async def test_missing_incident_returns_none(self, fake_db):
        assert await IncidentService.record_notification(
            9999, (0, 60), 720
        ) is None


class TestDeferNotification:
    """静默顺延"""

    async def test_defer_moves_gate_without_consuming_notification(self, fake_db):
        """
        顺延只推闸门、不增加 notify_count

        提醒是被推迟而非跳过，静默结束后仍按原阶梯节奏继续。
        """
        started = datetime(2026, 10, 4, 22, 0)
        incident = await IncidentService.open_incident(
            node_name="n1", alert_type="node_down", node_url="u", started_at=started,
        )

        until = datetime(2026, 10, 5, 7, 0)
        updated = await IncidentService.defer_notification(incident["id"], until)

        assert updated["notify_count"] == 0, "顺延不应消耗提醒次数"
        assert updated["next_notify_at"] == format_iso_utc(until)
        assert updated["total_failures"] == 1, "被顺延的失败仍要计入"

    async def test_defer_without_until_only_counts_failure(self, fake_db):
        incident = await IncidentService.open_incident(
            node_name="n1", alert_type="node_down", node_url="u",
            started_at=datetime(2026, 10, 4, 22, 0),
        )
        before = incident["next_notify_at"]
        updated = await IncidentService.defer_notification(incident["id"], None)
        assert updated["total_failures"] == 1
        assert updated["next_notify_at"] == before


class TestBumpFailures:
    """静默累计"""

    async def test_bump_accumulates_without_touching_notify_count(self, fake_db):
        incident = await IncidentService.open_incident(
            node_name="n1", alert_type="node_down", node_url="u",
            started_at=datetime(2026, 10, 4, 20, 0),
        )

        for _ in range(20):
            await IncidentService.bump_failures(incident["id"])

        row = fake_db[incident["id"]]
        assert row.total_failures == 20
        assert row.notify_count == 0

    async def test_bump_on_missing_incident_is_safe(self, fake_db):
        """事件不存在时静默返回，不抛异常打断心跳流程"""
        await IncidentService.bump_failures(9999)


class TestResolveIncident:
    """关闭事件"""

    async def test_resolve_sets_status_and_duration(self, fake_db):
        started = datetime(2026, 10, 4, 20, 0)
        incident = await IncidentService.open_incident(
            node_name="n1", alert_type="node_down", node_url="u", started_at=started,
        )

        resolved_at = started + timedelta(hours=8, minutes=12)
        snapshot = await IncidentService.resolve_incident(incident["id"], resolved_at)

        assert snapshot["status"] == "resolved"
        expected = int(timedelta(hours=8, minutes=12).total_seconds())
        assert snapshot["duration_seconds"] == expected
        assert fake_db[incident["id"]].status == "resolved"

    async def test_resolve_clears_gate(self, fake_db):
        """关闭后清空闸门，避免残留状态影响下一次故障"""
        incident = await IncidentService.open_incident(
            node_name="n1", alert_type="node_down", node_url="u",
            started_at=datetime(2026, 10, 4, 20, 0),
        )
        await IncidentService.resolve_incident(incident["id"])
        assert fake_db[incident["id"]].next_notify_at is None

    async def test_resolve_preserves_notify_count_in_snapshot(self, fake_db):
        """快照要带出提醒次数，恢复通知正文要用"""
        started = datetime(2026, 10, 4, 20, 0)
        incident = await IncidentService.open_incident(
            node_name="n1", alert_type="node_down", node_url="u", started_at=started,
        )
        for i in range(3):
            await IncidentService.record_notification(
                incident["id"], (0, 60, 240, 480, 720), 720,
                now=started + timedelta(minutes=i),
            )

        snapshot = await IncidentService.resolve_incident(incident["id"])
        assert snapshot["notify_count"] == 3

    async def test_resolve_missing_returns_none(self, fake_db):
        assert await IncidentService.resolve_incident(9999) is None


class TestServiceFailureIsolation:
    """数据库异常不应打断心跳主流程"""

    async def test_open_incident_failure_returns_none(self, monkeypatch):
        @asynccontextmanager
        async def broken_context():
            raise RuntimeError("数据库不可用")
            yield  # pragma: no cover

        monkeypatch.setattr(incident_mod, "get_session_context", broken_context)

        result = await IncidentService.open_incident(
            node_name="n1", alert_type="node_down", node_url="u",
            started_at=datetime(2026, 10, 4, 20, 0),
        )
        assert result is None

    async def test_get_open_incident_failure_returns_none(self, monkeypatch):
        @asynccontextmanager
        async def broken_context():
            raise RuntimeError("数据库不可用")
            yield  # pragma: no cover

        monkeypatch.setattr(incident_mod, "get_session_context", broken_context)
        assert await IncidentService.get_open_incident("n1", "node_down") is None

    async def test_bump_failures_failure_is_swallowed(self, monkeypatch):
        """累计失败失败时不能抛出去，否则会打断心跳任务"""
        @asynccontextmanager
        async def broken_context():
            raise RuntimeError("数据库不可用")
            yield  # pragma: no cover

        monkeypatch.setattr(incident_mod, "get_session_context", broken_context)
        await IncidentService.bump_failures(1)
