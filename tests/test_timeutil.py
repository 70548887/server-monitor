"""
时间工具测试

锁定两件事：
    1. utcnow() 与已弃用的 datetime.utcnow() 行为完全一致
       —— 迁移不能改变任何时间戳的取值，否则历史数据会错乱
    2. 时区转换在 UTC 与本地之间可逆，不丢信息
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.utils.timeutil import (
    format_iso_utc,
    to_local_time,
    to_utc_naive,
    utcnow,
    utcnow_aware,
)


class TestUtcnow:
    """UTC 取值"""

    def test_matches_utcnow(self):
        """
        与 datetime.utcnow() 取值一致（毫秒级容差）

        这是迁移的安全前提：若两者有偏差，所有新写入的时间戳
        都会与历史数据错位。
        """
        before = datetime.utcnow()
        ours = utcnow()
        after = datetime.utcnow()

        assert before <= ours <= after

    def test_returns_naive_datetime(self):
        """
        返回朴素时间

        数据库列是朴素 DateTime（未启用 timezone=True），
        写入带时区的对象会导致与已有数据混用时产生偏差。
        """
        assert utcnow().tzinfo is None

    def test_is_close_to_real_utc(self):
        """与真实 UTC 时间的偏差在合理范围内"""
        delta = abs((utcnow() - datetime.utcnow()).total_seconds())
        assert delta < 1.0

    def test_aware_variant(self):
        """带时区版本带 UTC tzinfo"""
        ts = utcnow_aware()
        assert ts.tzinfo == timezone.utc


class TestToLocalTime:
    """UTC → 本地"""

    def test_naive_assumed_utc(self):
        """朴素时间戳默认按 UTC 解释（与心跳写入约定一致）"""
        ts = datetime(2026, 10, 4, 15, 0)
        local = to_local_time(ts)

        assert local.tzinfo is not None
        expected_hour = (15 + self._offset_hours()) % 24
        assert local.hour == expected_hour

    def test_assume_utc_false_keeps_local(self):
        """已是本地时间时不做换算"""
        ts = datetime(2026, 10, 4, 15, 0)
        assert to_local_time(ts, assume_utc=False) == ts

    def test_aware_passthrough_conversion(self):
        """带时区的时间直接换算"""
        ts = datetime(2026, 10, 4, 15, 0, tzinfo=timezone.utc)
        local = to_local_time(ts)
        assert local.hour == (15 + self._offset_hours()) % 24

    def test_none_passthrough(self):
        assert to_local_time(None) is None

    def test_round_trip_preserves_instant(self):
        """转换再换回，时刻不变"""
        ts = datetime(2026, 10, 4, 15, 30, 45)
        local = to_local_time(ts)
        back = to_utc_naive(local)
        assert back == ts

    def _offset_hours(self):
        offset = datetime.now().astimezone().utcoffset() or timedelta(0)
        return round(offset.total_seconds() / 3600)


class TestToUtcNaive:
    """本地/带时区 → UTC 朴素"""

    def test_naive_utc_unchanged(self):
        ts = datetime(2026, 10, 4, 15, 0)
        assert to_utc_naive(ts, assume_utc=True) == ts

    def test_naive_local_converted(self):
        """
        assume_utc=False 时把本地朴素时间转成 UTC

        静默时段计算中，本地 07:00 需转成 UTC 才能与事件时间戳比较。
        """
        local_ts = datetime(2026, 10, 4, 15, 0)
        utc = to_utc_naive(local_ts, assume_utc=False)

        assert utc.tzinfo is None
        expected_hour = (15 - self._offset_hours()) % 24
        assert utc.hour == expected_hour

    def test_aware_converted(self):
        ts = datetime(2026, 10, 4, 15, 0, tzinfo=timezone.utc)
        utc = to_utc_naive(ts)
        assert utc == datetime(2026, 10, 4, 15, 0)
        assert utc.tzinfo is None

    def test_none_passthrough(self):
        assert to_utc_naive(None) is None

    def test_crosses_day_boundary(self):
        """跨日边界转换正确（本地 01:00 在 UTC+8 下是前一天 17:00）"""
        offset = self._offset_hours()
        if offset < 0.5:
            pytest.skip("需要非零时区偏移")

        local_ts = datetime(2026, 10, 5, 1, 0)
        utc = to_utc_naive(local_ts, assume_utc=False)
        assert utc.day == 4

    def _offset_hours(self):
        offset = datetime.now().astimezone().utcoffset() or timedelta(0)
        return round(offset.total_seconds() / 3600)


class TestFormatIsoUtc:
    """ISO 格式化"""

    def test_appends_z(self):
        ts = datetime(2026, 10, 4, 15, 0, tzinfo=timezone.utc)
        out = format_iso_utc(ts)
        assert out.endswith("Z")
        assert "+00:00" not in out

    def test_naive_assumed_utc(self):
        out = format_iso_utc(datetime(2026, 10, 4, 15, 0))
        assert out.startswith("2026-10-04T15:00")
        assert out.endswith("Z")

    def test_none_passthrough(self):
        assert format_iso_utc(None) is None


class TestNoDeprecatedCalls:
    """防止已弃用的调用回归"""

    def test_no_utcnow_in_app_code(self):
        """
        项目代码不应再直接引用 datetime.utcnow

        Python 3.12 起该方法已弃用，未来版本会移除。
        时间获取统一走 app.utils.timeutil.utcnow()。

        注意匹配两种形态：
          - 调用形式：datetime.utcnow()
          - 引用形式：default=datetime.utcnow（无括号，作为默认值传入）
        后者曾被漏掉 —— 早期版本只检查带括号的形式，
        导致 4 处引用形式的弃用调用长期存在。
        """
        import re
        from pathlib import Path

        root = Path(__file__).parent.parent / "app"
        offenders = []

        # 同时匹配调用与引用形式
        pattern = re.compile(r"datetime\s*\.\s*utcnow")

        for py_file in root.rglob("*.py"):
            if py_file.name == "timeutil.py":
                continue  # 定义处不算
            content = py_file.read_text(encoding="utf-8")
            if pattern.search(content):
                offenders.append(py_file.relative_to(root).as_posix())

        assert not offenders, (
            f"以下文件仍在引用已弃用的 datetime.utcnow: {offenders}"
        )

    def test_no_utcnow_in_tests_either(self):
        """
        测试代码同样不应实际调用弃用方法

        注释与文档字符串里提及 "datetime.utcnow" 是合理的
        （本文件就在解释为何弃用它），因此只检查代码行。
        """
        import re
        from pathlib import Path

        root = Path(__file__).parent
        offenders = []
        # 匹配真实调用：datetime.utcnow( 后跟左括号
        call_pattern = re.compile(r"datetime\s*\.\s*utcnow\s*\(")
        # 本文件需要直接调用弃用方法做等价性对照，故豁免
        exempt = {Path(__file__).name}

        for py_file in root.rglob("*.py"):
            if py_file.name in exempt:
                continue
            content = py_file.read_text(encoding="utf-8")

            # 逐行剔除注释；文档字符串内的提及不算
            in_docstring = False
            for lineno, line in enumerate(content.splitlines(), 1):
                stripped = line.strip()

                # 跟踪三引号文档字符串
                if stripped.count('"""') == 1:
                    in_docstring = not in_docstring
                    continue
                if in_docstring:
                    continue
                if stripped.startswith("#"):
                    continue
                # 行内注释之前的部分
                code = line.split("#", 1)[0]

                if call_pattern.search(code):
                    offenders.append(f"{py_file.name}:{lineno}")

        assert not offenders, (
            f"测试代码仍在调用已弃用的 datetime.utcnow(): {offenders}"
        )