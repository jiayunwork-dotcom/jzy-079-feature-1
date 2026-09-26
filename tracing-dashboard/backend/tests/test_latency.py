"""延迟基线测试：增量分位、常量序列、小样本精确回退。"""
from __future__ import annotations

import random

from app.latency import P2Quantile, LatencyBaseline, exact_quantile


class TestExactQuantile:
    def test_single_value(self) -> None:
        assert exact_quantile([42.0], 0.95) == 42.0

    def test_linear_interpolation_matches_known_positions(self) -> None:
        values = [10.0, 20.0, 30.0, 40.0, 50.0]
        assert exact_quantile(values, 0.0) == 10.0
        assert exact_quantile(values, 0.5) == 30.0
        assert exact_quantile(values, 1.0) == 50.0
        # p25: 位置 0.25*4 = 1 -> 20
        assert exact_quantile(values, 0.25) == 20.0


class TestP2Quantile:
    def test_constant_stream_is_exact_at_all_quantiles(self) -> None:
        """所有观测相等时，任何分位都必须等于该常量。"""
        for p in (0.5, 0.95, 0.99):
            est = P2Quantile(p)
            for _ in range(500):
                est.add(100.0)
            assert est.value() == 100.0
            assert est.count == 500

    def test_monotone_in_quantile(self) -> None:
        """同一分布上 p50 <= p95 <= p99。"""
        rng = random.Random(7)
        med = P2Quantile(0.5)
        p95 = P2Quantile(0.95)
        p99 = P2Quantile(0.99)
        for _ in range(2000):
            value = rng.lognormvariate(4.0, 0.8)
            med.add(value)
            p95.add(value)
            p99.add(value)
        assert med.value() <= p95.value() <= p99.value()

    def test_tracks_uniform_distribution(self) -> None:
        """均匀分布 U(0,1000) 上，大样本 p95 估计应贴近真实 950（允许误差）。"""
        rng = random.Random(2026)
        est = P2Quantile(0.95)
        for _ in range(5000):
            est.add(rng.uniform(0, 1000))
        assert abs(est.value() - 950.0) < 25.0

    def test_tail_heavy_outlier_moves_high_quantile_more(self) -> None:
        rng = random.Random(3)
        p50 = P2Quantile(0.5)
        p99 = P2Quantile(0.99)
        for _ in range(2000):
            value = rng.uniform(90, 110)
            p50.add(value)
            p99.add(value)
        for _ in range(40):  # 2% 的极端慢请求
            p50.add(1000.0)
            p99.add(1000.0)
        # 中位数几乎不动，p99 必须被长尾显著抬高
        assert abs(p50.value() - 100.0) < 15.0
        assert p99.value() > 300.0

    def test_few_observations_falls_back_to_exact(self) -> None:
        est = P2Quantile(0.5)
        est.add(10.0)
        est.add(30.0)
        assert est.count == 2
        # 不足 5 个观测：用缓存原始值做精确中位数
        assert est.value() == 20.0

    def test_duplicates_do_not_crash(self) -> None:
        """大量重复值（相邻标志点高度相同）不能触发除零或产生逆序。"""
        est = P2Quantile(0.95)
        for value in (1.0, 1.0, 1.0, 1.0, 1.0, 2.0, 2.0, 2.0, 1.0, 1.0):
            est.add(value)
        for _ in range(100):
            est.add(1.0)
        assert 1.0 <= est.value() <= 2.0


class TestLatencyBaseline:
    def test_empty_quantiles_are_none(self) -> None:
        baseline = LatencyBaseline()
        assert baseline.count == 0
        assert baseline.quantile("p95") is None

    def test_incremental_update(self) -> None:
        """逐样本更新后分位随样本推进，不需要重算历史。"""
        baseline = LatencyBaseline()
        for _ in range(100):
            baseline.add(100.0)
        assert baseline.count == 100
        assert baseline.quantile("p50") == baseline.quantile("p99") == 100.0
        # 持续并入更慢的样本后，基线分位跟着抬升（真实中位数为 200）
        for _ in range(100):
            baseline.add(300.0)
        assert baseline.quantile("p50") > 150.0
        assert baseline.quantile("p50") <= baseline.quantile("p95")

    def test_quantiles_are_independent(self) -> None:
        """三档分位各自维护，高档分位不受中低档实现细节影响。"""
        baseline = LatencyBaseline()
        for value in [10.0, 20.0, 30.0]:
            baseline.add(float(value))
        q = baseline.quantiles()
        assert set(q) == {"p50", "p95", "p99"}
        assert q["p50"] == 20.0
