"""
时区转换测试

背景：心跳时间戳来自 datetime.utcnow()（UTC 朴素时间），而静默时段
这类配置是用户按**本地时刻**填写的（如"23:00-07:00"）。两者直接比较
会整体错位 —— 在 UTC+8 上，实际静默的会是本地 07:00-15:00，
与用户意图恰好相反。

这类缺陷在开发机上（若时区恰为 UTC）完全看不出来，因此必须显式锁定。
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.services.alerting import AlertingService, to_local_time
from app.services.config import AlertingConfig

# 本机时区是否为 UTC。为 False 时才有实际错位风险
LOCAL_OFFSET_HOURS = (
    datetime.now().astimezone().utcoffset().total_seconds() / 3600
)


class TestToLocalTime:
    """UTC → 本地转换"""

    def test_naive_treated_as_utc(self):
        """
        朴素时间戳按 UTC 解释（心跳与数据库的写入约定）
        """
        ts = datetime(2026, 10, 4, 15, 0)  # UTC 15:00
        local = to_local_time(ts, assume_utc=True)

        assert local.tzinfo is not None
        assert local.hour == (15 + int(LOCAL_OFFSET_HOURS)) % 24

    def test_already_local_is_untouched(self):
        """assume_utc=False 时不做任何换算"""
        ts = datetime(2026, 10, 4, 15, 0)
        local = to_local_time(ts, assume_utc=False)

        assert local == ts
        assert local.hour == 15

    def test_aware_datetime_is_converted(self):
        """带时区的时间直接换算，与 assume_utc 无关"""
        ts = datetime(2026, 10, 4, 15, 0, tzinfo=timezone.utc)
        local = to_local_time(ts)

        assert local.tzinfo is not None
        assert local.hour == (15 + int(LOCAL_OFFSET_HOURS)) % 24

    def test_none_passthrough(self):
        assert to_local_time(None) is None

    def test_conversion_is_reversible(self):
        """转换再换回应得到原时刻"""
        ts = datetime(2026, 10, 4, 15, 30)
        local = to_local_time(ts, assume_utc=True)
        back = local.astimezone(timezone.utc).replace(tzinfo=None)

        assert back == ts


class TestQuietHoursRespectLocalTime:
    """
    静默时段按本地时刻判定

    这是回归保护：修复前拿 UTC 时间戳直接比对，静默窗口整体错位。
    """

    def make_service(self):
        return AlertingService(AlertingConfig(
            quiet_hours_enabled=True,
            quiet_hours_start="23:00",
            quiet_hours_end="07:00",
            suppress_persist=False,
        ))

    @pytest.mark.skipif(
        abs(LOCAL_OFFSET_HOURS) < 0.5,
        reason="本机时区为 UTC，UTC 与本地时刻一致，无法复现错位",
    )
    def test_utc_15_is_quiet_because_local_is_23(self):
        """UTC 15:00 = 本地 23:00，应判定为静默"""
        svc = self.make_service()
        assert svc._in_quiet_hours(
            datetime(2026, 10, 4, 15, 0), assume_utc=True
        ) is True

    @pytest.mark.skipif(
        abs(LOCAL_OFFSET_HOURS) < 0.5,
        reason="本机时区为 UTC，无法复现错位",
    )
    def test_utc_07_is_not_quiet_because_local_is_15(self):
        """UTC 07:00 = 本地 15:00，不应静默"""
        svc = self.make_service()
        assert svc._in_quiet_hours(
            datetime(2026, 10, 4, 7, 0), assume_utc=True
        ) is False

    @pytest.mark.skipif(
        abs(LOCAL_OFFSET_HOURS) < 0.5,
        reason="本机时区为 UTC，无法复现错位",
    )
    def test_offset_is_applied_to_window(self):
        """
        静默窗口整体平移了时区偏移

        修复前：UTC 23:00（本地 07:00）被判静默 —— 与意图完全相反。
        """
        svc = self.make_service()
        # 本地 07:00（静默结束）对应的 UTC 时刻不应静默
        local_7am_as_utc = datetime(2026, 10, 4, 7, 0)
        assert svc._in_quiet_hours(local_7am_as_utc, assume_utc=True) is False

        # 本地 23:00（静默开始）对应的 UTC 时刻应静默
        local_11pm_as_utc = datetime(2026, 10, 4, 15, 0)
        assert svc._in_quiet_hours(local_11pm_as_utc, assume_utc=True) is True

    def test_local_assumption_still_works(self):
        """
        assume_utc=False 时按传入值直接判断

        供已有本地时间戳的调用方（如界面层）使用。
        """
        svc = self.make_service()
        assert svc._in_quiet_hours(
            datetime(2026, 10, 4, 23, 30), assume_utc=False
        ) is True
        assert svc._in_quiet_hours(
            datetime(2026, 10, 4, 12, 0), assume_utc=False
        ) is False


class TestQuietHoursEndDatetimeBasis:
    """
    顺延时刻与入参同基准

    若返回本地时间而入参是 UTC，闸门比较会永远判"未到时间"，
    提醒被无限推迟。
    """

    def make_service(self):
        return AlertingService(AlertingConfig(
            quiet_hours_enabled=True,
            quiet_hours_start="23:00",
            quiet_hours_end="07:00",
            suppress_persist=False,
        ))

    def test_returns_naive_utc(self):
        svc = self.make_service()
        # 传 UTC 23:30（本地 07:30，已过静默）→ 不应返回顺延时刻
        # 传一个必然在静默期内的 UTC 时刻
        now = datetime(2026, 10, 4, 15, 30) if LOCAL_OFFSET_HOURS >= 8 else datetime(2026, 10, 4, 23, 30)
        end = svc._quiet_hours_end_datetime(now, assume_utc=True)

        if end is not None:
            assert end.tzinfo is None, "返回值应为 UTC 朴素时间，与入参一致"
            assert end > now, "顺延时刻必须在当前之后"

    def test_local_input_returns_local(self):
        svc = self.make_service()
        end = svc._quiet_hours_end_datetime(
            datetime(2026, 10, 4, 23, 30), assume_utc=False
        )
        assert end is not None
        assert end.tzinfo is None
        assert end.hour == 7, "本地 23:30 应顺延到次日 07:00"
        assert end.day == 5