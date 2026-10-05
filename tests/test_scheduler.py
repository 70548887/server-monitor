"""
调度器测试

重点锁定两件事：
    1. 心跳任务照常注册（回归保护）
    2. 数据留存清理任务确实被注册 —— 此前清理方法写好了却从未被调度，
       样本表会无限增长，这是本次修复的核心
"""

from datetime import datetime

import pytest

from app.services.config import AppConfig, RetentionConfig
from app.services.scheduler import SchedulerService


class TestRetentionConfig:
    """留存配置解析"""

    def test_defaults(self):
        cfg = RetentionConfig()
        assert cfg.samples_days == 30
        assert cfg.aggregates_days == 90
        assert cfg.cleanup_hour == 3
        assert cfg.enabled is True

    def test_loaded_from_yaml(self):
        from pathlib import Path
        from app.services.config import load_config

        root = Path(__file__).parent.parent / "configs"
        cfg = load_config(
            app_config_path=str(root / "app.yaml"),
            nodes_config_path=str(root / "nodes.yaml"),
        )
        # app.yaml 里的 retention 段此前完全没有被解析
        assert cfg.retention.samples_days == 30
        assert cfg.retention.cleanup_hour == 3

    def test_app_config_exposes_retention(self):
        assert isinstance(AppConfig().retention, RetentionConfig)


class TestSchedulerJobs:
    """
    任务注册

    这些测试必须是异步的：AsyncIOScheduler.start() 内部会调用
    asyncio.get_running_loop()，同步测试里没有事件循环会直接报错。
    """

    def _scheduler_with_config(self, monkeypatch, **retention_kwargs):
        """构造调度器并注入指定留存配置"""
        import app.services.scheduler as sched_mod

        cfg = AppConfig()
        cfg.retention = RetentionConfig(**retention_kwargs)
        monkeypatch.setattr(sched_mod, "get_config", lambda: cfg)

        svc = SchedulerService(interval_seconds=60)
        return svc

    async def test_heartbeat_job_registered(self, monkeypatch):
        svc = self._scheduler_with_config(monkeypatch)
        svc.start()
        try:
            job_ids = {j.id for j in svc._scheduler.get_jobs()}
            assert "heartbeat_task" in job_ids
        finally:
            svc.stop()

    async def test_cleanup_job_registered(self, monkeypatch):
        """
        留存清理任务必须被注册

        这是回归保护：该方法早已实现，但调度器只注册了心跳任务，
        导致样本表从不清理。
        """
        svc = self._scheduler_with_config(monkeypatch)
        svc.start()
        try:
            job_ids = {j.id for j in svc._scheduler.get_jobs()}
            assert "retention_cleanup" in job_ids, (
                "数据留存清理任务未注册，样本表会无限增长"
            )
        finally:
            svc.stop()

    async def test_cleanup_job_uses_configured_hour(self, monkeypatch):
        """清理时刻取自配置，而不是写死的常量"""
        svc = self._scheduler_with_config(monkeypatch, cleanup_hour=5)
        svc.start()
        try:
            job = svc._scheduler.get_job("retention_cleanup")
            assert job is not None
            # CronTrigger 的字段字符串里应体现 5 点
            assert "5" in str(job.trigger)
        finally:
            svc.stop()

    async def test_cleanup_job_not_registered_when_disabled(self, monkeypatch):
        """显式禁用时不应注册清理任务"""
        svc = self._scheduler_with_config(monkeypatch, enabled=False)
        svc.start()
        try:
            job_ids = {j.id for j in svc._scheduler.get_jobs()}
            assert "retention_cleanup" not in job_ids
            # 但心跳任务不受影响
            assert "heartbeat_task" in job_ids
        finally:
            svc.stop()

    async def test_start_is_idempotent(self, monkeypatch):
        """重复 start 不应重复注册任务"""
        svc = self._scheduler_with_config(monkeypatch)
        svc.start()
        try:
            before = len(svc._scheduler.get_jobs())
            svc.start()  # 第二次调用应直接返回
            assert len(svc._scheduler.get_jobs()) == before
            assert before == 3, "应有 心跳/聚合/清理 三个任务"
        finally:
            svc.stop()

    async def test_aggregate_rollup_job_registered(self, monkeypatch):
        """
        聚合任务必须被注册

        7 天/30 天可用性依赖 aggregates 表；没有聚合任务，
        长周期指标永远是 None。
        """
        svc = self._scheduler_with_config(monkeypatch)
        svc.start()
        try:
            job_ids = {j.id for j in svc._scheduler.get_jobs()}
            assert "aggregate_rollup" in job_ids, (
                "聚合任务未注册，7d/30d 可用性将无数据来源"
            )
        finally:
            svc.stop()

    async def test_rollup_runs_hourly(self, monkeypatch):
        """聚合按小时执行，否则窗口会越积越多"""
        svc = self._scheduler_with_config(monkeypatch)
        svc.start()
        try:
            job = svc._scheduler.get_job("aggregate_rollup")
            assert job is not None
            # CronTrigger(minute=5) 生成的是 "5 * * * *" 这类表达式
            trigger_repr = str(job.trigger)
            assert "minute='5'" in trigger_repr or "5" in trigger_repr
        finally:
            svc.stop()

    async def test_stop_clears_running_flag(self, monkeypatch):
        svc = self._scheduler_with_config(monkeypatch)
        svc.start()
        assert svc.is_running is True
        svc.stop()
        assert svc.is_running is False


class TestCleanupTask:
    """清理任务的执行"""

    async def test_cleanup_calls_repository(self, monkeypatch):
        """清理任务调用仓库的删除方法，并传入配置的天数"""
        import app.services.scheduler as sched_mod
        from app.repositories import mysql_repository

        cfg = AppConfig()
        cfg.retention = RetentionConfig(samples_days=15, enabled=True)
        monkeypatch.setattr(sched_mod, "get_config", lambda: cfg)

        calls = {}

        class FakeRepo:
            async def cleanup_old_samples(self, days=30):
                calls["days"] = days
                return 42

        monkeypatch.setattr(
            mysql_repository, "get_mysql_repository", lambda: FakeRepo()
        )

        svc = SchedulerService()
        await svc._run_cleanup_task()

        assert calls.get("days") == 15

    async def test_cleanup_skipped_when_disabled(self, monkeypatch):
        """禁用时不执行任何删除"""
        import app.services.scheduler as sched_mod
        from app.repositories import mysql_repository

        cfg = AppConfig()
        cfg.retention = RetentionConfig(enabled=False)
        monkeypatch.setattr(sched_mod, "get_config", lambda: cfg)

        called = []

        class FakeRepo:
            async def cleanup_old_samples(self, days=30):
                called.append(days)
                return 0

        monkeypatch.setattr(
            mysql_repository, "get_mysql_repository", lambda: FakeRepo()
        )

        svc = SchedulerService()
        await svc._run_cleanup_task()

        assert called == []

    async def test_cleanup_failure_does_not_raise(self, monkeypatch):
        """清理失败不应抛出，否则会打断调度器"""
        import app.services.scheduler as sched_mod
        from app.repositories import mysql_repository

        cfg = AppConfig()
        cfg.retention = RetentionConfig(enabled=True)
        monkeypatch.setattr(sched_mod, "get_config", lambda: cfg)

        class BrokenRepo:
            async def cleanup_old_samples(self, days=30):
                raise RuntimeError("数据库不可用")

        monkeypatch.setattr(
            mysql_repository, "get_mysql_repository", lambda: BrokenRepo()
        )

        svc = SchedulerService()
        # 不应抛异常
        await svc._run_cleanup_task()

    async def test_notification_log_cleanup_failure_is_isolated(self, monkeypatch):
        """通知记录清理失败不应影响样本清理"""
        import app.services.scheduler as sched_mod
        from app.repositories import mysql_repository
        from app.services import notification as notif_mod

        cfg = AppConfig()
        cfg.retention = RetentionConfig(enabled=True)
        monkeypatch.setattr(sched_mod, "get_config", lambda: cfg)

        samples_cleaned = []

        class FakeRepo:
            async def cleanup_old_samples(self, days=30):
                samples_cleaned.append(days)
                return 10

        monkeypatch.setattr(
            mysql_repository, "get_mysql_repository", lambda: FakeRepo()
        )

        class BrokenLogService:
            @staticmethod
            async def cleanup_old_logs(days=30):
                raise RuntimeError("日志表清理失败")

        monkeypatch.setattr(
            notif_mod, "NotificationLogService", BrokenLogService
        )

        svc = SchedulerService()
        await svc._run_cleanup_task()

        assert samples_cleaned, "样本清理应照常完成"
