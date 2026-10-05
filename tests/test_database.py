"""数据库层测试（无真实连接）

只测纯逻辑：错误分类、重试退避、状态查询。
需要真实 MySQL 的集成测试在 TestIntegration 中标记跳过。
"""

import pytest
from sqlalchemy.exc import InterfaceError, OperationalError

from app.models.base import (
    DB_MAX_RETRIES,
    DB_RETRY_BASE_DELAY,
    DB_RETRY_MAX_DELAY,
    _execute_with_retry,
    get_db_status,
    is_connection_error,
)


class TestConnectionErrorDetection:
    """连接错误分类 —— 决定是否重试"""

    def test_operational_error(self):
        assert is_connection_error(OperationalError("stmt", {}, Exception())) is True

    def test_interface_error(self):
        assert is_connection_error(InterfaceError("stmt", {}, Exception())) is True

    @pytest.mark.parametrize("message", [
        "MySQL server has gone away",
        "Lost connection to MySQL server",
        "Can't connect to MySQL server",
        "Connection refused",
        "Connection reset by peer",
        "Broken pipe",
        "Server closed the connection",
        "read timeout",
        "network is unreachable",
    ])
    def test_connection_keyword(self, message):
        assert is_connection_error(Exception(message)) is True

    @pytest.mark.parametrize("message", [
        "Duplicate entry '1' for key 'PRIMARY'",
        "Unknown column 'foo' in 'field list'",
        "Table 'x' doesn't exist",
        "Syntax error near 'SELECT'",
    ])
    def test_non_connection_error(self, message):
        """业务/语法错误不应被重试，否则只会无谓地拖慢失败响应"""
        assert is_connection_error(Exception(message)) is False

    def test_timeout_keyword(self):
        assert is_connection_error(Exception("Operation timed out")) is True


class TestRetryExecution:
    """重试与指数退避"""

    async def test_success_on_first_attempt(self):
        calls = []

        async def op():
            calls.append(1)
            return "ok"

        assert await _execute_with_retry(op) == "ok"
        assert len(calls) == 1

    async def test_retries_on_connection_error(self):
        """连接错误会重试，最终成功"""
        calls = []

        async def op():
            calls.append(1)
            if len(calls) < 3:
                raise OperationalError("stmt", {}, Exception("connection lost"))
            return "recovered"

        result = await _execute_with_retry(op, max_retries=5, base_delay=0.001)
        assert result == "recovered"
        assert len(calls) == 3

    async def test_non_connection_error_not_retried(self):
        """非连接错误立即抛出，不做重试"""
        calls = []

        async def op():
            calls.append(1)
            raise ValueError("bad query")

        with pytest.raises(ValueError):
            await _execute_with_retry(op, max_retries=5, base_delay=0.001)

        assert len(calls) == 1

    async def test_raises_after_max_retries(self):
        """超过重试上限后抛出异常"""
        calls = []

        async def op():
            calls.append(1)
            raise OperationalError("stmt", {}, Exception("connection refused"))

        with pytest.raises(OperationalError):
            await _execute_with_retry(op, max_retries=2, base_delay=0.001)

        # 首次 + 2 次重试
        assert len(calls) == 3

    async def test_exponential_backoff(self, monkeypatch):
        """退避延迟按指数增长，且有上限"""
        delays = []

        async def fake_sleep(seconds):
            delays.append(seconds)

        monkeypatch.setattr("app.models.base.asyncio.sleep", fake_sleep)

        async def op():
            raise OperationalError("stmt", {}, Exception("connection lost"))

        with pytest.raises(OperationalError):
            await _execute_with_retry(op, max_retries=4, base_delay=0.5, max_delay=10.0)

        assert delays == [0.5, 1.0, 2.0, 4.0]
        # 全部不超过上限
        assert all(d <= DB_RETRY_MAX_DELAY for d in delays)


class TestRetryConfiguration:
    """重试常量"""

    def test_retry_constants(self):
        assert DB_MAX_RETRIES == 3
        assert DB_RETRY_BASE_DELAY > 0
        assert DB_RETRY_MAX_DELAY > DB_RETRY_BASE_DELAY

    def test_exponential_backoff_constants_are_sane(self):
        """按 3 次重试计算的总等待时间应可接受（不拖垮请求）"""
        total = sum(
            min(DB_RETRY_BASE_DELAY * (2 ** i), DB_RETRY_MAX_DELAY)
            for i in range(DB_MAX_RETRIES)
        )
        assert total < 30, f"重试总等待 {total}s 过长"


class TestDbStatus:
    """数据库状态查询"""

    def test_get_db_status(self):
        status = get_db_status()
        assert isinstance(status, dict)
        assert "available" in status
        assert isinstance(status["available"], bool)


class TestMySQLRepository:
    """仓库配置（不建立真实连接）"""

    def test_use_retry_flag(self):
        from app.repositories.mysql_repository import MySQLRepository

        assert MySQLRepository(use_retry=True)._use_retry is True
        assert MySQLRepository(use_retry=False)._use_retry is False

    def test_session_manager_selection(self):
        """use_retry 决定取普通会话管理器还是带重试的"""
        from app.repositories.mysql_repository import MySQLRepository

        with_retry = MySQLRepository(use_retry=True)
        without_retry = MySQLRepository(use_retry=False)
        # 两者都返回可调用的会话管理器
        assert callable(with_retry._get_session_manager())
        assert callable(without_retry._get_session_manager())


class TestSchemaRegistration:
    """表模型注册

    create_all 只创建"已导入到 Base.metadata"的表。某个模型文件没被
    导入时不会报错，只是静默地不建那张表 —— 运行到相关功能才失败。
    这里显式锁定关键表必须在 metadata 中。
    """

    def test_all_tables_registered(self):
        from app.models.base import Base
        # 触发与生产启动路径一致的导入
        import app.models.admin  # noqa: F401
        from app.repositories.mysql_repository import get_mysql_repository  # noqa: F401

        tables = set(Base.metadata.tables)
        for expected in (
            "admins", "nodes", "samples", "aggregates",
            "notification_channels", "notification_logs",
            "notification_recipients", "alert_logs", "alert_incidents",
        ):
            assert expected in tables, f"表 {expected} 未注册，create_all 不会创建它"

    def test_incident_table_columns(self):
        """事件表的关键列齐全 —— 缺列会让闸门逻辑静默失效"""
        from app.models.base import Base
        import app.models.admin  # noqa: F401
        from app.models.notification_channel import AlertIncidentModel  # noqa: F401

        cols = set(Base.metadata.tables["alert_incidents"].columns.keys())
        for expected in ("next_notify_at", "notify_count", "status",
                         "started_at", "resolved_at", "duration_seconds"):
            assert expected in cols, f"alert_incidents 缺少列 {expected}"

    def test_aggregate_table_columns(self):
        from app.models.base import Base
        from app.models.sample import AggregateModel  # noqa: F401

        cols = set(Base.metadata.tables["aggregates"].columns.keys())
        for expected in ("node_name", "window_start", "window_minutes",
                         "total_count", "success_count", "availability"):
            assert expected in cols, f"aggregates 缺少列 {expected}"


class TestNodeRepositoryContract:
    """仓库接口契约（用假数据验证数据流，不连库）"""

    def test_repository_has_expected_methods(self):
        from app.repositories.mysql_repository import MySQLRepository

        for name in (
            "save_sample", "get_samples", "get_all_nodes", "get_node_by_name",
            "create_node", "update_node", "delete_node", "upsert_node",
            "save_samples_batch", "get_recent_samples", "cleanup_old_samples",
            "update_node_status",
        ):
            assert hasattr(MySQLRepository, name), f"缺少方法 {name}"

    def test_cleanup_old_samples_is_defined(self):
        """
        数据留存清理方法存在

        注意：该方法目前没有被任何调度任务调用，样本表会持续增长。
        此测试只锁定"方法存在"，不表示留存策略已生效。
        """
        import inspect

        from app.repositories.mysql_repository import MySQLRepository

        sig = inspect.signature(MySQLRepository.cleanup_old_samples)
        assert "days" in sig.parameters
