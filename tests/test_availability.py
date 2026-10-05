"""可用性统计测试"""

from datetime import datetime, timedelta

import pytest

from app.services.availability import (
    AvailabilityService,
    AvailabilityStats,
    HeartbeatRecord,
    HourlyStats,
)


def make_record(node_name="n1", success=True, latency_ms=100.0, ts=None, status_code=200):
    return HeartbeatRecord(
        node_name=node_name,
        timestamp=ts or datetime.utcnow(),
        success=success,
        latency_ms=latency_ms,
        status_code=status_code,
        error_type=None if success else "Timeout",
    )


class TestHeartbeatRecord:
    """记录数据类"""

    def test_create_record(self):
        rec = make_record()
        assert rec.node_name == "n1"
        assert rec.success is True
        assert rec.status_code == 200

    def test_create_failed_record(self):
        rec = make_record(success=False, status_code=0)
        assert rec.success is False
        assert rec.error_type == "Timeout"


class TestAvailabilityStats:
    """统计结果默认值"""

    def test_default_values(self):
        stats = AvailabilityStats(node_name="n1")
        assert stats.uptime_24h == 0.0
        assert stats.total_checks_24h == 0
        assert stats.current_status == "unknown"
        assert stats.last_success is True


class TestHourlyStats:
    """每小时统计"""

    def test_availability_calculation(self):
        s = HourlyStats(hour=datetime.utcnow(), total=100, success=95)
        assert s.availability == 95.0

    def test_availability_no_data(self):
        """无数据时视为 100%，避免图表上出现空洞"""
        s = HourlyStats(hour=datetime.utcnow())
        assert s.availability == 100.0

    @pytest.mark.parametrize("total,success,expected", [
        (1000, 1000, "up"),
        (1000, 995, "up"),
        (1000, 950, "degraded"),
        (1000, 900, "degraded"),
        (1000, 500, "down"),
        (1000, 0, "down"),
    ])
    def test_status_thresholds(self, total, success, expected):
        s = HourlyStats(hour=datetime.utcnow(), total=total, success=success)
        assert s.status == expected

    def test_status_up(self):
        s = HourlyStats(hour=datetime.utcnow(), total=10, success=10)
        assert s.status == "up"

    def test_status_degraded(self):
        s = HourlyStats(hour=datetime.utcnow(), total=10, success=9)
        assert s.status == "degraded"

    def test_status_down(self):
        s = HourlyStats(hour=datetime.utcnow(), total=10, success=5)
        assert s.status == "down"


class TestAvailabilityService:
    """可用性服务"""

    @pytest.fixture
    def service(self):
        return AvailabilityService()

    def test_add_record(self, service):
        service.add_record(make_record())
        stats = service.get_stats("n1")
        assert stats.total_checks_24h == 1
        assert stats.success_checks_24h == 1

    def test_add_multiple_records(self, service):
        service.add_records([make_record() for _ in range(10)])
        stats = service.get_stats("n1")
        assert stats.total_checks_24h == 10

    def test_current_status_up(self, service):
        service.add_record(make_record(success=True))
        assert service.get_stats("n1").current_status == "up"

    def test_current_status_down(self, service):
        service.add_record(make_record(success=False))
        stats = service.get_stats("n1")
        assert stats.current_status == "down"
        assert stats.last_success is False

    def test_get_all_stats(self, service):
        service.add_record(make_record(node_name="a"))
        service.add_record(make_record(node_name="b"))
        all_stats = service.get_all_stats()
        assert set(all_stats) == {"a", "b"}

    def test_unknown_node_returns_empty_stats(self, service):
        """查询未知节点不应抛异常，返回空统计"""
        stats = service.get_stats("never-seen")
        assert stats.node_name == "never-seen"
        assert stats.total_checks_24h == 0

    def test_max_records_limit(self, service):
        """内存占用有上限，防止长时间运行后无限增长"""
        limit = AvailabilityService.MAX_RECORDS_PER_NODE
        for _ in range(limit + 200):
            service.add_record(make_record())

        assert len(service._records["n1"]) <= limit

    def test_memory_stats(self, service):
        service.add_record(make_record())
        info = service.get_memory_stats()
        assert info["node_count"] >= 1
        assert info["total_records"] == 1
        assert info["retention_hours"] == AvailabilityService.RETENTION_HOURS
        assert info["max_records_per_node"] == AvailabilityService.MAX_RECORDS_PER_NODE

    def test_remove_node(self, service):
        service.add_record(make_record())
        service.remove_node("n1")
        assert "n1" not in service._records
        assert service.get_stats("n1").total_checks_24h == 0

    def test_availability_percentage(self, service):
        """可用性百分比 = 成功数 / 总数"""
        for _ in range(8):
            service.add_record(make_record(success=True))
        for _ in range(2):
            service.add_record(make_record(success=False))

        stats = service.get_stats("n1")
        assert stats.uptime_24h == 80.0

    def test_get_hourly_trend(self, service):
        """趋势按小时聚合，且返回固定数量的数据点"""
        now = datetime.utcnow()
        for i in range(10):
            service.add_record(make_record(ts=now - timedelta(minutes=i)))

        trend = service.get_hourly_trend("n1", hours=6)
        assert len(trend) == 6
        assert all(isinstance(h, HourlyStats) for h in trend)

    def test_hourly_trend_empty_for_unknown_node(self, service):
        assert service.get_hourly_trend("never-seen", hours=6) == []

    def test_get_latency_trend(self, service):
        """延迟趋势包含平均/最小/最大等字段"""
        now = datetime.utcnow()
        for i in range(10):
            service.add_record(make_record(latency_ms=100 + i, ts=now - timedelta(minutes=i)))

        trend = service.get_latency_trend("n1", hours=6)
        assert len(trend) == 6
        for key in ("hour", "avg_latency", "min_latency", "max_latency",
                    "total", "success", "availability"):
            assert key in trend[0], f"缺少字段 {key}"

    def test_latency_trend_empty_for_unknown_node(self, service):
        assert service.get_latency_trend("never-seen", hours=6) == []

    def test_separate_nodes_tracked_independently(self, service):
        service.add_record(make_record(node_name="a", success=True))
        service.add_record(make_record(node_name="b", success=False))

        assert service.get_stats("a").current_status == "up"
        assert service.get_stats("b").current_status == "down"


class TestAvailabilityServiceAccessor:
    """服务实例获取"""

    def test_get_availability_service_returns_instance(self):
        from app.services.availability import get_availability_service

        service = get_availability_service()
        assert isinstance(service, AvailabilityService)
        # 再次获取应返回同一实例（进程内共享可用性数据）
        assert get_availability_service() is service
