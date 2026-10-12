"""
升级提醒测试（Phase 4）

故障持续超过阈值后：
    - 邮件主题带 [升级] 前缀，正文带醒目提示
    - 跳过"单节点每日发送上限"

取舍说明：每日上限本来是为了抑制重复提醒，但它的副作用是
"故障持续很久时反而收不到通知"——那恰恰是最需要被告知的时刻。
升级机制把这两个目标分开：平时靠阶梯节奏收敛，长时间未恢复时放开送达。
"""

from datetime import datetime

import pytest

from app.services.alerting import AlertEvent, AlertingService, get_alerting_service
from app.services.config import AlertingConfig

LADDER = (0, 60, 240, 480, 720)


def make_service(**kwargs):
    params = dict(
        enabled=True,
        suppress_persist=False,
        reminder_intervals=LADDER,
        intervals_loop_minutes=720,
        escalate_after_minutes=0,
    )
    params.update(kwargs)
    return AlertingService(AlertingConfig(**params))


def down_event(seq=1, incident_duration_s=None):
    return AlertEvent(
        alert_type="node_down",
        node_name="n1",
        node_url="http://n1/health",
        message="m",
        notify_seq=seq,
        incident_duration_s=incident_duration_s,
    )


class TestEscalationDisabledByDefault:
    """默认不升级"""

    def test_disabled_when_zero(self):
        svc = make_service(escalate_after_minutes=0)
        # 即使提醒序号很大也不升级
        assert svc.is_escalated(down_event(seq=20)) is False

    def test_disabled_when_negative(self):
        svc = make_service(escalate_after_minutes=-1)
        assert svc.is_escalated(down_event(seq=20)) is False


class TestElapsedForSeq:
    """按阶梯反推持续时长"""

    def test_first_alert_is_zero_elapsed(self):
        svc = make_service()
        assert svc._elapsed_for_seq(1) == 0

    def test_matches_ladder_offsets(self):
        """第 N 次提醒对应阶梯第 N 档的时刻"""
        svc = make_service()
        assert svc._elapsed_for_seq(2) == 60
        assert svc._elapsed_for_seq(3) == 240
        assert svc._elapsed_for_seq(4) == 480
        assert svc._elapsed_for_seq(5) == 720

    def test_overflow_uses_loop_interval(self):
        """超出阶梯长度后按循环间隔累加"""
        svc = make_service()
        # 第 6 次 = 最后一档 720 + 循环 720
        assert svc._elapsed_for_seq(6) == 1440
        assert svc._elapsed_for_seq(7) == 2160

    def test_not_uniform_spacing(self):
        """
        不能假设"每次间隔相等"

        阶梯是递减的（立即/1h/4h/8h/12h），若按固定间隔推算，
        升级时机会明显偏早或偏晚。
        """
        svc = make_service()
        assert svc._elapsed_for_seq(3) == 240
        # 若按"每次 1 小时"错误推算，第 3 次会是 120
        assert svc._elapsed_for_seq(3) != 120


class TestEscalationTriggering:
    """升级触发"""

    def test_escalates_after_threshold(self):
        svc = make_service(escalate_after_minutes=240)
        # 第 3 档 = 240 分钟，达到阈值
        assert svc.is_escalated(down_event(seq=3)) is True

    def test_not_escalated_before_threshold(self):
        svc = make_service(escalate_after_minutes=240)
        assert svc.is_escalated(down_event(seq=2)) is False

    def test_prefers_actual_duration(self):
        """有实际持续时长时以它为准，而不是反推"""
        svc = make_service(escalate_after_minutes=240)
        # seq=1 反推是 0 分钟，但实际已持续 5 小时
        event = down_event(seq=1, incident_duration_s=5 * 3600)
        assert svc.is_escalated(event) is True

        event2 = down_event(seq=1, incident_duration_s=60)
        assert svc.is_escalated(event2) is False

    def test_recovery_never_escalated(self):
        """恢复通知不升级 —— 它是好消息，且用于关闭事件"""
        svc = make_service(escalate_after_minutes=60)
        event = AlertEvent(
            alert_type="node_recovery",
            node_name="n1",
            node_url="http://n1",
            message="已恢复",
            notify_seq=10,
            incident_duration_s=10 * 3600,
        )
        assert svc.is_escalated(event) is False

    def test_no_seq_does_not_crash(self):
        svc = make_service(escalate_after_minutes=60)
        event = AlertEvent(
            alert_type="node_down",
            node_name="n1",
            node_url="http://n1",
            message="m",
        )
        assert svc.is_escalated(event) is False

    def test_global_service_used_by_process_alerts(self):
        """process_alerts 用的就是这个单例，行为必须一致"""
        svc = get_alerting_service()
        assert hasattr(svc, "is_escalated")


class TestEscalationBypassesDailyLimit:
    """升级时跳过每日上限"""

    async def test_skip_daily_limit_flag(self, monkeypatch):
        """
        skip_daily_limit=True 时不再检查每日总量

        注意 check_send_limits 仍会检查最小间隔与可发送时段，
        避免升级后变成瞬时刷屏。
        """
        from app.services.notification import NotificationService

        channel = {
            "id": 1,
            "daily_limit_per_node": 1,       # 上限设为 1
            "min_interval_seconds": 0,
            "send_time_start": None,
            "send_time_end": None,
        }

        called = {"count": 0}

        async def fake_today_count(channel_id, node_name):
            called["count"] += 1
            return 999  # 远超上限

        monkeypatch.setattr(
            "app.services.notification.NotificationLogService.get_today_count_for_node",
            staticmethod(fake_today_count),
        )

        # 未升级：应被上限拦截
        ok, reason = await NotificationService.check_send_limits(
            channel, "n1", skip_daily_limit=False
        )
        assert ok is False
        assert "上限" in reason

        # 升级：跳过上限检查，不再查询计数
        called["count"] = 0
        ok2, _ = await NotificationService.check_send_limits(
            channel, "n1", skip_daily_limit=True
        )
        assert ok2 is True
        assert called["count"] == 0, "跳过上限时不应再查询当日计数"

    async def test_min_interval_still_enforced_when_escalated(self, monkeypatch):
        """升级不等于无视节流：最小间隔仍然生效"""
        from app.services.notification import NotificationService

        channel = {
            "id": 1,
            "daily_limit_per_node": 10,
            "min_interval_seconds": 3600,
            "send_time_start": None,
            "send_time_end": None,
        }

        async def fake_last_send(channel_id, node_name=None):
            return datetime.now()

        monkeypatch.setattr(
            "app.services.notification.NotificationLogService.get_last_send_time",
            staticmethod(fake_last_send),
        )

        ok, reason = await NotificationService.check_send_limits(
            channel, "n1", skip_daily_limit=True
        )
        assert ok is False
        assert "间隔" in reason

    async def test_send_notification_accepts_escalated(self, monkeypatch):
        """send_notification 的 escalated 参数会传递到限制检查"""
        from app.services.notification import NotificationService

        captured = {}

        async def fake_check(channel, node_name=None, skip_daily_limit=False):
            captured["skip"] = skip_daily_limit
            return False, "test-stop"

        monkeypatch.setattr(NotificationService, "check_send_limits", fake_check)

        async def fake_channel(cid):
            return {"id": 1, "name": "c", "enabled": True, "channel_type": "email"}

        monkeypatch.setattr(NotificationService, "get_channel_by_id", fake_channel)

        async def fake_log(**kwargs):
            return {"id": 1}

        monkeypatch.setattr(
            "app.services.notification.NotificationLogService.add_log",
            staticmethod(fake_log),
        )

        await NotificationService.send_notification(
            channel_id=1, subject="s", message="m",
            alert_type="node_down", node_name="n1", escalated=True,
        )
        assert captured["skip"] is True

        await NotificationService.send_notification(
            channel_id=1, subject="s", message="m",
            alert_type="node_down", node_name="n1", escalated=False,
        )
        assert captured["skip"] is False
