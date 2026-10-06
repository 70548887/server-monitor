"""
pytest 共享夹具

设计原则：
    - 单元测试不依赖 MySQL / Redis。数据库层的测试用夹具模拟，
      需要真实连接的集成测试默认跳过，避免"没起数据库就一片红"。
    - 时间相关逻辑统一用 freeze_time 夹具驱动，不依赖真实时钟，
      这样"跨午夜"这类场景才能被确定性地测出来。
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

# 确保项目根目录可导入
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


def _now():
    """
    从 alerting 模块取"当前时间"

    该模块的 datetime 会被 frozen_datetime 夹具替换为假时钟，
    内存版 IncidentService 跟着走，才能与闸门共用同一个时钟。
    """
    import app.services.alerting as alerting_mod

    return alerting_mod.datetime.now()


class FrozenClock:
    """
    可推进的假时钟

    用法:
        clock.set(2026, 10, 4, 20, 0)          # 本地 20:00
        clock.set(2026, 10, 4, 20, 0, utc=True) # UTC 20:00
        clock.advance(minutes=30)
        clock.now()

    时区语义很重要：生产里心跳时间戳来自 datetime.utcnow()（UTC 朴素时间），
    而静默时段配置是用户按本地时刻填写的。测试必须能分别构造这两种，
    否则无法验证时区转换是否正确。
    """

    def __init__(self):
        self.current = datetime(2026, 10, 4, 20, 0, 0)
        # UTC 模式的偏移量：让 utcnow() 相对于本地时刻体现真实时差
        self.offset_hours = _local_utc_offset_hours()

    def set(self, year, month, day, hour=0, minute=0, second=0, utc=False):
        if utc:
            # 给出一个 UTC 朴素时间戳，使其本地时刻等于所填的
            naive_local = datetime(year, month, day, hour, minute, second)
            self.current = naive_local - timedelta(hours=self.offset_hours)
        else:
            self.current = datetime(year, month, day, hour, minute, second)
        return self.current

    def advance(self, seconds=0, minutes=0, hours=0, days=0):
        self.current += timedelta(
            seconds=seconds, minutes=minutes, hours=hours, days=days
        )
        return self.current

    def now(self):
        return self.current

    def set_local(self, year, month, day, hour=0, minute=0, second=0):
        """
        按**本地时刻**设置假时钟

        与 set() 的区别：set() 直接写入给定值（视为 UTC 朴素时间，
        与生产的 utcnow() 语义一致）；set_local() 会按本地时区折算，
        用于测试静默时段这类"用户按本地填写"的配置。
        """
        offset = datetime.now().astimezone().utcoffset()
        offset_hours = (offset.total_seconds() / 3600) if offset else 0.0
        local_naive = datetime(year, month, day, hour, minute, second)
        # 本地时间 = UTC + 偏移，因此 UTC = 本地 - 偏移
        self.current = local_naive - timedelta(hours=offset_hours)
        return self.current

    def as_local(self, value: datetime) -> datetime:
        """把 UTC 朴素时间显示为本地时间，便于断言与调试"""
        offset = datetime.now().astimezone().utcoffset()
        if offset is None:
            return value
        return value + offset


def _local_utc_offset_hours() -> float:
    """本地时区与 UTC 的小时差（向东为正）"""
    offset = datetime.now().astimezone().utcoffset()
    return (offset.total_seconds() / 3600) if offset else 0.0


@pytest.fixture
def clock():
    """可推进的假时钟"""
    return FrozenClock()


@pytest.fixture
def frozen_datetime(clock, monkeypatch):
    """
    把 alerting / incident 模块里的时间来源替换为假时钟

    心跳间隔、抑制窗口、阶梯节奏都依赖时间，必须能控制才能断言
    "跨午夜不再爆发"这类行为。

    要 patch 两处，缺一不可：
      1. 模块内的 datetime 类 —— 供 datetime.now() / utcnow() 调用
      2. 模块内的 utcnow 函数 —— 时间已统一收敛到 timeutil.utcnow()，
         它是以模块级名字导入的，替换 datetime 类对它无效
    """

    class PatchedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.current

        @classmethod
        def utcnow(cls):
            return clock.current

    def patched_utcnow():
        return clock.current

    import app.services.alerting as alerting_mod

    monkeypatch.setattr(alerting_mod, "datetime", PatchedDatetime)
    monkeypatch.setattr(alerting_mod, "utcnow", patched_utcnow)

    try:
        import app.services.incident as incident_mod

        monkeypatch.setattr(incident_mod, "datetime", PatchedDatetime)
        # incident 模块可能没有 utcnow 这个名字（视其是否使用而定）
        monkeypatch.setattr(incident_mod, "utcnow", patched_utcnow, raising=False)
    except ImportError:
        pass

    return clock


@pytest.fixture
def make_heartbeat_result(clock):
    """
    构造 HeartbeatResult 的工厂夹具

    时间戳默认取假时钟的当前值 —— 闸门与静默判定都以检测时间戳为准，
    若这里用真实时钟，测试就无法控制时间推进。
    """

    def _make(
        node_name="test-node",
        timestamp=None,
        success=True,
        status_code=200,
        latency_ms=100.0,
        url=None,
        error_type=None,
        error_message=None,
    ):
        from app.services.heartbeat import HeartbeatResult

        return HeartbeatResult(
            node_name=node_name,
            url=url or f"http://{node_name}/health",
            timestamp=timestamp or clock.now(),
            status_code=status_code,
            latency_ms=latency_ms,
            success=success,
            error_type=error_type,
            error_message=error_message,
            response_size=None,
        )

    return _make


@pytest.fixture(autouse=True)
def _reset_singletons():
    """
    每个测试后清空全局单例

    - 告警服务的节点状态会跨测试残留，导致顺序相关的诡异失败
    - 配置单例会被"写回配置"的测试改掉，污染后续用例
    """
    yield
    try:
        import app.services.alerting as alerting_mod

        alerting_mod._alerting_service = None
    except (ImportError, AttributeError):
        pass

    try:
        import app.services.config as config_mod

        config_mod._config = None
    except (ImportError, AttributeError):
        pass


class MemoryIncidentService:
    """
    内存版 IncidentService，接口与真实实现一致

    真实实现依赖 MySQL。事件闸门是本次告警改造的核心逻辑，
    必须能在无数据库环境下被完整测试，否则只能靠手工验证。
    """

    def __init__(self):
        self.rows = {}
        self._next_id = 1
        self.deferrals = []
    async def get_open_incident(self, node_name, alert_type):
        for r in self.rows.values():
            if (
                r["node_name"] == node_name
                and r["alert_type"] == alert_type
                and r["status"] == "open"
            ):
                return dict(r)
        return None

    async def open_incident(self, node_name, alert_type, node_url, started_at,
                            last_error=None):
        rec = {
            "id": self._next_id,
            "node_name": node_name,
            "alert_type": alert_type,
            "node_url": node_url,
            "started_at": started_at,
            "last_notified_at": None,
            # 首次提醒立即放行（阶梯首档为 0 分钟）
            "next_notify_at": started_at,
            "notify_count": 0,
            "status": "open",
            "resolved_at": None,
            "duration_seconds": None,
            "last_error": last_error,
            "total_failures": 0,
        }
        self.rows[self._next_id] = rec
        self._next_id += 1
        return dict(rec)

    async def should_notify(self, incident, now):
        nxt = incident.get("next_notify_at")
        if nxt is None:
            return True, "no gate set"
        if now < nxt:
            return False, f"还需 {int((nxt - now).total_seconds())}s"
        return True, "gate passed"

    async def record_notification(self, incident_id, intervals, loop_minutes,
                                  last_error=None, failure_delta=0, now=None):
        from app.services.incident import compute_next_notify_at

        r = self.rows[incident_id]
        r["notify_count"] += 1
        # 与真实实现一致：闸门推进使用调用方传入的检测时间戳
        sent_at = now or _now()
        r["last_notified_at"] = sent_at
        r["next_notify_at"] = compute_next_notify_at(
            started_at=r["started_at"],
            notify_count=r["notify_count"],
            intervals=intervals,
            loop_minutes=loop_minutes,
            now=sent_at,
        )
        if last_error:
            r["last_error"] = last_error
        r["total_failures"] += failure_delta
        return dict(r)

    async def defer_notification(self, incident_id, until, last_error=None,
                                 now=None):
        r = self.rows.get(incident_id)
        if not r:
            return None
        r["total_failures"] += 1
        if last_error:
            r["last_error"] = last_error
        if until is not None:
            r["next_notify_at"] = until
        self.deferrals.append((now or _now(), until))
        return dict(r)

    async def bump_failures(self, incident_id, delta=1, last_error=None):
        r = self.rows.get(incident_id)
        if r:
            r["total_failures"] += delta
            if last_error:
                r["last_error"] = last_error

    async def resolve_incident(self, incident_id, resolved_at=None):
        r = self.rows.get(incident_id)
        if not r:
            return None
        resolved_at = resolved_at or _now()
        snapshot = dict(r)
        r["status"] = "resolved"
        r["resolved_at"] = resolved_at
        r["duration_seconds"] = int((resolved_at - r["started_at"]).total_seconds())
        # 关闭后清空闸门，避免残留状态影响下一次故障
        r["next_notify_at"] = None
        snapshot["status"] = "resolved"
        snapshot["duration_seconds"] = r["duration_seconds"]
        return snapshot


@pytest.fixture
def incident_store(monkeypatch):
    """
    用内存版替换真实 IncidentService

    alerting._apply_incident_gate 里是函数内导入（from .incident import ...），
    因此补丁要打在 incident 模块上；alerting 模块的属性一并打上，
    以便直接引用该名字的代码也走内存实现。
    """
    store = MemoryIncidentService()

    import app.services.alerting as alerting_mod
    import app.services.incident as incident_mod

    monkeypatch.setattr(incident_mod, "IncidentService", store)
    monkeypatch.setattr(alerting_mod, "IncidentService", store, raising=False)
    return store
