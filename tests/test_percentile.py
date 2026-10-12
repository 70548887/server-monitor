"""
分位数计算测试

背景：聚合表的 latency_p50/p95/p99 三列自建表起就存在，
但 rollup 从未写入——代码注释写着"不做近似，避免给出
看起来精确却无依据的数字"。

MySQL 5.7 无窗口函数（PERCENT_RANK_CONT 需 8.0+），
因此采用：SQL 取回每个窗口的延迟数组，Python 侧排序求分位。
实测单次聚合约 2300 条样本、窗口内单节点仅 5 条，开销可忽略。
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from app.repositories.mysql_repository import _percentile


class TestPercentileMath:
    """分位数数学正确性 —— 期望值均可手算"""

    def test_empty_returns_none(self):
        assert _percentile([], 0.5) is None
        assert _percentile([], 0.95) is None

    def test_single_sample(self):
        assert _percentile([100.0], 0.5) == 100.0
        assert _percentile([100.0], 0.99) == 100.0

    def test_two_samples_interpolates(self):
        # pos = (2-1)*0.5 = 0.5 -> 100 与 200 的中点
        assert _percentile([100.0, 200.0], 0.5) == 150.0

    def test_median_of_1_to_10(self):
        """1..10 的中位数 = (5+6)/2 = 5.5"""
        vals = [float(i) for i in range(1, 11)]
        assert _percentile(vals, 0.5) == 5.5

    def test_p95_of_1_to_10(self):
        """pos = 9*0.95 = 8.55 -> vals[8]*(0.45) + vals[9]*(0.55) = 9.55"""
        vals = [float(i) for i in range(1, 11)]
        assert _percentile(vals, 0.95) == 9.55

    def test_p99_of_1_to_10(self):
        """pos = 9*0.99 = 8.91 -> 8*0.09 + 9*0.91 = 9.91"""
        vals = [float(i) for i in range(1, 11)]
        assert _percentile(vals, 0.99) == 9.91

    def test_quarter_boundary(self):
        """pos 落在整数上时应精确取到该值"""
        vals = [10.0, 20.0, 30.0, 40.0, 50.0]
        # pos = 4*0.5 = 2.0 -> vals[2] = 30
        assert _percentile(vals, 0.5) == 30.0

    def test_q0_returns_min(self):
        vals = [10.0, 20.0, 30.0]
        assert _percentile(vals, 0.0) == 10.0

    def test_q1_returns_max(self):
        vals = [10.0, 20.0, 30.0]
        assert _percentile(vals, 1.0) == 30.0

    def test_all_equal(self):
        vals = [50.0] * 8
        for q in (0.5, 0.95, 0.99):
            assert _percentile(vals, q) == 50.0

    def test_rounding_to_two_decimals(self):
        vals = [1.23456, 7.89123]
        result = _percentile(vals, 0.5)
        assert result == round(result, 2)

    def test_realistic_heartbeat_values(self):
        """
        真实心跳量级（45~120ms）不应出现异常放大

        样本：[45.2, 47.1, 50.3, 52.8, 61.4, 89.9, 120.5, 45.0, 46.2, 48.0]
        排序后：[45.0, 45.2, 46.2, 47.1, 48.0, 50.3, 52.8, 61.4, 89.9, 120.5]
        pos = (10-1)*0.5 = 4.5 -> 48.0 与 50.3 的中点 = 49.15
        """
        vals = [45.2, 47.1, 50.3, 52.8, 61.4, 89.9, 120.5, 45.0, 46.2, 48.0]
        assert _percentile(vals, 0.5) == 49.15
        assert 45.0 <= _percentile(vals, 0.5) <= 120.5

    def test_unsorted_input_gives_same_result(self):
        """
        输入无需预排序

        早期实现把 sorted_values 写进参数名，隐含"调用方负责排序"，
        实际调用处一旦漏排就会得到错误结果（p95 > p99 出现过）。
        函数必须自洽。
        """
        ascending = [10.0, 20.0, 30.0, 40.0, 50.0]
        shuffled = [30.0, 10.0, 50.0, 20.0, 40.0]
        for q in (0.5, 0.95, 0.99):
            assert _percentile(ascending, q) == _percentile(shuffled, q)

    def test_unsorted_input_keeps_monotonic(self):
        """未排序输入下 p50 <= p95 <= p99 仍须成立"""
        vals = [1167.23, 2701.75, 885.17, 42.0, 999.0, 1300.0, 2600.0]
        p50 = _percentile(vals, 0.50)
        p95 = _percentile(vals, 0.95)
        p99 = _percentile(vals, 0.99)
        assert p50 <= p95 + 0.01
        assert p95 <= p99 + 0.01


class TestPercentileInvariants:
    """必须成立的不变式"""

    def test_always_within_min_max(self):
        """分位数必然落在样本的 min~max 之间"""
        import random

        random.seed(42)  # 固定种子保证可复现
        for _ in range(200):
            n = random.randint(1, 50)
            vals = [random.uniform(10, 5000) for _ in range(n)]
            mn, mx = min(vals), max(vals)
            for q in (0.5, 0.95, 0.99):
                p = _percentile(vals, q)
                assert mn - 0.01 <= p <= mx + 0.01

    def test_monotonic_in_q(self):
        """p50 <= p95 <= p99 恒成立"""
        import random

        random.seed(7)
        for _ in range(200):
            n = random.randint(1, 80)
            vals = [random.uniform(1, 3000) for _ in range(n)]
            p50 = _percentile(vals, 0.50)
            p95 = _percentile(vals, 0.95)
            p99 = _percentile(vals, 0.99)
            assert p50 <= p95 + 0.01
            assert p95 <= p99 + 0.01

    def test_matches_numpy_linear_convention(self):
        """
        与 NumPy 默认 linear 插值一致

        便于日后升级到 MySQL 8 用 PERCENT_RANK_CONT 时数值可比对。
        """
        import random

        random.seed(123)
        try:
            import numpy as np
        except ImportError:
            pytest.skip("未安装 numpy，跳过交叉验证")

        for _ in range(50):
            n = random.randint(2, 60)
            vals = [random.uniform(1, 2000) for _ in range(n)]
            for q in (0.5, 0.95, 0.99):
                ours = _percentile(vals, q)
                theirs = float(np.percentile(vals, q * 100))
                assert abs(ours - theirs) < 0.01, (
                    f"q={q} 我们={ours} numpy={theirs}"
                )


class TestRollupWritesPercentiles:
    """rollup 必须写入三列"""

    def test_aggregate_model_has_percentile_columns(self):
        from app.models.sample import AggregateModel

        cols = AggregateModel.__table__.columns
        for name in ("latency_p50_ms", "latency_p95_ms", "latency_p99_ms"):
            assert name in cols, f"聚合表缺少 {name}"

    def test_rollup_source_uses_percentile_helper(self):
        """
        写入逻辑必须调用分位数计算

        防止将来重构时又退回"不写分位数"的状态 ——
        那是本次修复前的行为。
        """
        src = (
            ROOT / "app" / "repositories" / "mysql_repository.py"
        ).read_text(encoding="utf-8")
        assert "_percentile(lat, 0.50)" in src
        assert "_percentile(lat, 0.95)" in src
        assert "_percentile(lat, 0.99)" in src

    def test_rollup_fetches_latency_samples(self):
        """必须取回样本级延迟数据，否则 sorted_lat 恒为空"""
        src = (
            ROOT / "app" / "repositories" / "mysql_repository.py"
        ).read_text(encoding="utf-8")
        assert "latency_groups" in src
        assert "latency_stmt" in src


class TestReadmeDocumentsLimitation:
    """文档需反映 p50/p95/p99 已实现"""

    def test_no_longer_listed_as_unimplemented(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        # 旧的"未落库"表述应已移除
        assert "p50/p95/p99 延迟未落库" not in readme
