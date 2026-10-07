"""配置加载测试"""

import os
from pathlib import Path

import pytest

from app.services.config import (
    AlertingConfig,
    AppConfig,
    DatabaseConfig,
    SchedulerConfig,
    load_config,
)

CONFIG_DIR = Path(__file__).parent.parent / "configs"


class TestConfigLoader:
    """配置加载器"""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.config = load_config(
            app_config_path=str(CONFIG_DIR / "app.yaml"),
            nodes_config_path=str(CONFIG_DIR / "nodes.yaml"),
        )

    def test_config_structure(self):
        """加载出的配置包含所有必备段落"""
        assert isinstance(self.config, AppConfig)
        assert self.config.name
        assert self.config.database is not None
        assert self.config.scheduler is not None
        assert self.config.alerting is not None
        assert self.config.logging is not None
        assert self.config.server is not None

    def test_alert_config(self):
        """告警段落的关键参数都从文件读到，而非落在默认值上"""
        alerting = self.config.alerting
        assert alerting.enabled is True
        assert alerting.consecutive_failures == 3
        assert alerting.latency_p95_ms == 2000
        # 抑制窗口已从 15 分钟调整为 240 分钟（抖动抑制）
        assert alerting.suppress_minutes == 240

    def test_get_config_singleton(self):
        """get_config 在已加载后返回同一实例，避免重复解析文件"""
        from app.services import config as config_mod

        config_mod._config = None
        first = config_mod.get_config()
        second = config_mod.get_config()
        assert first is second

    def test_missing_file_falls_back_to_defaults(self, tmp_path):
        """配置文件不存在时回退到默认值而不是抛异常"""
        cfg = load_config(
            app_config_path=str(tmp_path / "nope.yaml"),
            nodes_config_path=str(tmp_path / "nope.yaml"),
        )
        assert cfg.scheduler.interval_seconds == 60
        assert cfg.alerting.consecutive_failures == 3
        assert cfg.nodes == []

    def test_nodes_loaded(self):
        """节点配置能正确加载并解析"""
        assert len(self.config.nodes) > 0
        node = self.config.nodes[0]
        assert node.name
        assert node.url.startswith("http")
        assert node.expect is not None


class TestDatabaseConfig:
    """数据库配置"""

    def test_mysql_config(self):
        cfg = DatabaseConfig()
        assert cfg.mysql_port == 3306
        assert cfg.mysql_database

    def test_redis_config(self):
        cfg = DatabaseConfig()
        assert cfg.redis_port == 6379
        assert cfg.redis_prefix


class TestSchedulerConfig:
    """调度器配置"""

    def test_scheduler_config(self):
        cfg = SchedulerConfig()
        assert cfg.interval_seconds == 60
        assert cfg.concurrency > 0
        assert cfg.retry_count >= 0
        assert cfg.retry_delay_ms >= 0

    def test_no_dead_timeout_field(self):
        """
        timeout_seconds 已移除

        节点的超时由各自的 NodeConfig.timeout_ms 决定，
        调度器级的 timeout_seconds 从未被消费 —— 保留它会让人
        以为改这里能影响实际超时。
        """
        cfg = SchedulerConfig()
        assert not hasattr(cfg, "timeout_seconds")

    def test_retry_config_is_wired(self):
        """重试配置真的传到了心跳服务（曾出现配置声明有但代码没实现）"""
        from app.services.scheduler import SchedulerService

        svc = SchedulerService(retry_count=5, retry_delay_ms=50)
        assert svc.heartbeat_service.retry_count == 5
        assert svc.heartbeat_service.retry_delay_ms == 50


class TestAlertingConfig:
    """告警配置 - 含事件化提醒节奏"""

    def test_defaults(self):
        cfg = AlertingConfig()
        assert cfg.enabled is True
        assert cfg.consecutive_failures == 3

    def test_reminder_ladder_defaults(self):
        """阶梯提醒默认值：立即 / 1h / 5h / 13h / 25h"""
        cfg = AlertingConfig()
        assert cfg.reminder_intervals == (0, 60, 240, 480, 720)
        assert cfg.intervals_loop_minutes == 720
        assert cfg.max_notifications_per_incident == 0

    def test_reminder_ladder_is_ascending(self):
        """阶梯必须升序，否则闸门计算会出错"""
        cfg = AlertingConfig()
        intervals = cfg.reminder_intervals
        assert list(intervals) == sorted(intervals)

    def test_quiet_hours_defaults(self):
        cfg = AlertingConfig()
        assert cfg.quiet_hours_enabled is False
        assert cfg.quiet_hours_start == "23:00"
        assert cfg.quiet_hours_end == "07:00"

    def test_suppress_persist_default(self):
        cfg = AlertingConfig()
        assert cfg.suppress_persist is True

    def test_yaml_values_are_loaded(self):
        """app.yaml 中的告警配置确实生效，而不是被默认值覆盖"""
        cfg = load_config(
            app_config_path=str(CONFIG_DIR / "app.yaml"),
            nodes_config_path=str(CONFIG_DIR / "nodes.yaml"),
        )
        assert cfg.alerting.suppress_minutes == 240
        assert list(cfg.alerting.reminder_intervals) == [0, 60, 240, 480, 720]

    def test_yaml_retry_config_loaded(self):
        """app.yaml 声明的重试配置真的被解析（曾是虚假承诺）"""
        cfg = load_config(
            app_config_path=str(CONFIG_DIR / "app.yaml"),
            nodes_config_path=str(CONFIG_DIR / "nodes.yaml"),
        )
        assert cfg.scheduler.retry_count == 3
        assert cfg.scheduler.retry_delay_ms == 200

    def test_no_dead_database_engine_field(self):
        """database.engine 已移除 —— 解析后从无任何分支消费"""
        cfg = DatabaseConfig()
        assert not hasattr(cfg, "engine")


class TestEnvironmentOverride:
    """环境变量覆盖"""

    def test_environment_override(self, monkeypatch):
        """MYSQL_HOST 等环境变量应优先于配置文件"""
        monkeypatch.setenv("MYSQL_HOST", "192.168.1.100")
        from app.services.config import _parse_database_config

        cfg = _parse_database_config({
            "mysql": {"host": "127.0.0.1", "password": "x"},
        })
        assert cfg.mysql_host == "192.168.1.100"


class TestNodeConfig:
    """节点配置"""

    def test_node_config_structure(self):
        cfg = load_config(
            app_config_path=str(CONFIG_DIR / "app.yaml"),
            nodes_config_path=str(CONFIG_DIR / "nodes.yaml"),
        )
        for node in cfg.nodes:
            assert node.name
            assert node.url.startswith(("http://", "https://"))
            assert node.timeout_ms > 0
            assert isinstance(node.tags, list)

    def test_node_config_with_expect(self):
        cfg = load_config(
            app_config_path=str(CONFIG_DIR / "app.yaml"),
            nodes_config_path=str(CONFIG_DIR / "nodes.yaml"),
        )
        with_expect = [n for n in cfg.nodes if n.expect.body_contains]
        assert len(with_expect) > 0, "示例配置里应有带 body_contains 断言的节点"
        assert all(n.expect.status == 200 for n in cfg.nodes)


class TestAppConfigDefaults:
    """AppConfig 默认值"""

    def test_default_config(self):
        cfg = AppConfig()
        assert cfg.name == "server-monitor"
        assert isinstance(cfg.database, DatabaseConfig)
        assert isinstance(cfg.scheduler, SchedulerConfig)
        assert isinstance(cfg.alerting, AlertingConfig)
