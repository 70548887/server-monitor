"""
聚合快照测试

背景：内存可用性服务只保留 48 小时记录，因此 uptime_7d / uptime_30d
此前是用"最近 48 小时的数据"算出来的，却按 7 天/30 天展示 —— 静默失真。
聚合表按窗口长期保留，为长周期统计提供真实数据来源。
"""

from datetime import datetime, timedelta

import pytest

from app.repositories.mysql_repository import MySQLRepository


class FakeAggRow:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class FakeScalarResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class TestAggregateRepositoryContract:
    """聚合方法的存在性与签名"""

    def test_rollup_method_exists(self):
        assert hasattr(MySQLRepository, "rollup_samples_to_aggregates")

    def test_read_method_exists(self):
        assert hasattr(MySQLRepository, "get_aggregated_uptime")

    def test_cleanup_method_exists(self):
        assert hasattr(MySQLRepository, "cleanup_old_aggregates")

    def test_rollup_signature(self):
        import inspect

        sig = inspect.signature(MySQLRepository.rollup_samples_to_aggregates)
        assert "window_minutes" in sig.parameters
        assert "hours" in sig.parameters


class TestRollupBehavior:
    """汇总行为（用假 session 驱动真实实现）"""

    @pytest.fixture
    def fake_repo(self, monkeypatch):
        """构造一个 session 可替换的仓库"""
        repo = MySQLRepository(use_retry=False)
        added = []
        deleted = []

        class FakeSession:
            def add(self, obj):
                added.append(obj)

            async def execute(self, stmt):
                deleted.append(stmt)
                return FakeScalarResult([])

            async def commit(self):
                pass

        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def fake_session_manager():
            yield FakeSession()

        monkeypatch.setattr(repo, "_get_session_manager", fake_session_manager)
        repo._added = added
        repo._deleted = deleted
        return repo

    async def test_no_samples_returns_zero(self, fake_repo, monkeypatch):
        """没有样本时不写入任何行，也不报错"""
        class EmptyResult:
            def all(self):
                return []

        class FakeSession:
            def add(self, obj):
                pass

            async def execute(self, stmt):
                return EmptyResult()

            async def commit(self):
                pass

        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def mgr():
            yield FakeSession()

        monkeypatch.setattr(fake_repo, "_get_session_manager", mgr)
        result = await fake_repo.rollup_samples_to_aggregates()

        assert result["windows"] == 0
        assert result["written"] == 0

    async def test_writes_one_row_per_window(self, monkeypatch):
        """每个 (节点, 窗口) 组合产出一行聚合"""
        repo = MySQLRepository(use_retry=False)

        # bucket 值：同一节点的两个不同窗口 + 另一节点的一个窗口
        rows = [
            FakeAggRow(node_name="n1", bucket=1000, total_count=3,
                       success_count=3, latency_min=10.0, latency_max=20.0,
                       latency_avg=15.0),
            FakeAggRow(node_name="n1", bucket=1001, total_count=3,
                       success_count=2, latency_min=10.0, latency_max=30.0,
                       latency_avg=18.0),
            FakeAggRow(node_name="n2", bucket=1000, total_count=2,
                       success_count=0, latency_min=0.0, latency_max=0.0,
                       latency_avg=0.0),
        ]

        # rollup 现在会发两条查询：延迟样本 + 聚合计数。
        # 延迟查询按 (node, bucket) 提供样本级数据。
        latency_rows = [
            FakeAggRow(node_name="n1", bucket=1000, latency_ms=10.0),
            FakeAggRow(node_name="n1", bucket=1000, latency_ms=15.0),
            FakeAggRow(node_name="n1", bucket=1000, latency_ms=20.0),
            FakeAggRow(node_name="n1", bucket=1001, latency_ms=10.0),
            FakeAggRow(node_name="n1", bucket=1001, latency_ms=25.0),
            FakeAggRow(node_name="n1", bucket=1001, latency_ms=30.0),
            FakeAggRow(node_name="n2", bucket=1000, latency_ms=0.0),
            FakeAggRow(node_name="n2", bucket=1000, latency_ms=0.0),
        ]

        added = []

        class AggResult:
            def all(self):
                return rows

        class LatencyResult:
            def all(self):
                return latency_rows

        class FakeSession:
            def add(self, obj):
                added.append(obj)

            async def execute(self, stmt):
                # 聚合查询含 avg/latency_min，延迟查询只取 latency_ms
                sql = str(stmt).lower()
                if "avg(" in sql or "min(" in sql:
                    return AggResult()
                return LatencyResult()

            async def commit(self):
                pass

        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def mgr():
            yield FakeSession()

        monkeypatch.setattr(repo, "_get_session_manager", mgr)
        result = await repo.rollup_samples_to_aggregates(window_minutes=5)

        assert result["windows"] == 3
        assert len(added) == 3
        # total_count 来自聚合计数查询：3 + 3 + 2
        assert result["samples"] == 8

    async def test_success_count_and_rate(self, monkeypatch):
        """成功数、失败数、错误率计算正确"""
        repo = MySQLRepository(use_retry=False)
        rows = [FakeAggRow(node_name="n1", bucket=1000, total_count=3,
                           success_count=2, latency_min=1.0, latency_max=9.0,
                           latency_avg=5.0)]
        latency_rows = [
            FakeAggRow(node_name="n1", bucket=1000, latency_ms=1.0),
            FakeAggRow(node_name="n1", bucket=1000, latency_ms=5.0),
            FakeAggRow(node_name="n1", bucket=1000, latency_ms=9.0),
        ]
        added = []

        class AggResult:
            def all(self):
                return rows

        class LatencyResult:
            def all(self):
                return latency_rows

        class FakeSession:
            def add(self, obj):
                added.append(obj)

            async def execute(self, stmt):
                sql = str(stmt).lower()
                if "avg(" in sql or "min(" in sql:
                    return AggResult()
                return LatencyResult()

            async def commit(self):
                pass

        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def mgr():
            yield FakeSession()

        monkeypatch.setattr(repo, "_get_session_manager", mgr)
        await repo.rollup_samples_to_aggregates()

        row = added[0]
        assert row.total_count == 3
        assert row.success_count == 2
        assert row.error_count == 1
        assert row.error_rate == round(1 / 3, 4)
        assert row.availability == round(2 / 3, 4)
        # 分位数此时也应写入。
        # 样本 [1, 5, 9]：
        #   p50: pos=(3-1)*0.5=1.0   -> vals[1]=5.0
        #   p95: pos=(3-1)*0.95=1.9  -> 5*0.1 + 9*0.9 = 8.6
        #   p99: pos=(3-1)*0.99=1.98 -> 5*0.02 + 9*0.98 = 8.92
        assert row.latency_p50_ms == 5.0
        assert row.latency_p95_ms == 8.6
        assert row.latency_p99_ms == 8.92

    async def test_existing_windows_deleted_first(self, monkeypatch):
        """重跑同一窗口是幂等的：先删后写"""
        repo = MySQLRepository(use_retry=False)
        rows = [FakeAggRow(node_name="n1", bucket=1000, total_count=1,
                           success_count=1, latency_min=1.0, latency_max=1.0,
                           latency_avg=1.0)]
        latency_rows = [FakeAggRow(node_name="n1", bucket=1000, latency_ms=1.0)]
        executed = []

        class AggResult:
            def all(self):
                return rows

        class LatencyResult:
            def all(self):
                return latency_rows

        class FakeSession:
            def add(self, obj):
                pass

            async def execute(self, stmt):
                executed.append(stmt)
                sql = str(stmt).lower()
                if "avg(" in sql or "min(" in sql:
                    return AggResult()
                return LatencyResult()

            async def commit(self):
                pass

        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def mgr():
            yield FakeSession()

        monkeypatch.setattr(repo, "_get_session_manager", mgr)
        await repo.rollup_samples_to_aggregates()

        # 应包含一条 DELETE（清掉同窗口旧数据），保证重跑幂等
        statements = [str(s).upper() for s in executed]
        assert any(s.lstrip().startswith("DELETE") for s in statements), (
            "应先删除同窗口旧数据，否则重跑会写出重复行"
        )

    async def test_db_error_is_swallowed(self, monkeypatch):
        """数据库异常不应抛出，否则会打断调度器"""
        repo = MySQLRepository(use_retry=False)

        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def broken():
            raise RuntimeError("数据库不可用")
            yield  # pragma: no cover

        monkeypatch.setattr(repo, "_get_session_manager", broken)
        result = await repo.rollup_samples_to_aggregates()

        assert result["windows"] == 0
        assert "error" in result


class TestAggregatedUptime:
    """长周期可用性读取"""

    async def test_uptime_percentage(self, monkeypatch):
        repo = MySQLRepository(use_retry=False)
        rows = [FakeAggRow(node_name="n1", total=1000, success=995)]

        class Result:
            def all(self):
                return rows

        class FakeSession:
            async def execute(self, stmt):
                return Result()

        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def mgr():
            yield FakeSession()

        monkeypatch.setattr(repo, "_get_session_manager", mgr)
        out = await repo.get_aggregated_uptime(days=7)

        assert out["n1"]["uptime"] == 99.5
        assert out["n1"]["total_checks"] == 1000

    async def test_empty_table_returns_empty_dict(self, monkeypatch):
        repo = MySQLRepository(use_retry=False)

        class Result:
            def all(self):
                return []

        class FakeSession:
            async def execute(self, stmt):
                return Result()

        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def mgr():
            yield FakeSession()

        monkeypatch.setattr(repo, "_get_session_manager", mgr)
        assert await repo.get_aggregated_uptime() == {}

    async def test_zero_total_gives_none_not_fake_100(self, monkeypatch):
        """
        无样本时返回 None 而不是 100%

        否则新建节点会显示"30 天可用性 100%"，掩盖了"从未采集"的事实。
        """
        repo = MySQLRepository(use_retry=False)
        rows = [FakeAggRow(node_name="n1", total=0, success=0)]

        class Result:
            def all(self):
                return rows

        class FakeSession:
            async def execute(self, stmt):
                return Result()

        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def mgr():
            yield FakeSession()

        monkeypatch.setattr(repo, "_get_session_manager", mgr)
        out = await repo.get_aggregated_uptime()

        assert out["n1"]["uptime"] is None

    async def test_db_error_returns_empty(self, monkeypatch):
        repo = MySQLRepository(use_retry=False)

        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def broken():
            raise RuntimeError("数据库不可用")
            yield  # pragma: no cover

        monkeypatch.setattr(repo, "_get_session_manager", broken)
        assert await repo.get_aggregated_uptime() == {}
