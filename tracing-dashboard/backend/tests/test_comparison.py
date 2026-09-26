"""对照判定单元测试：延迟分位/劣化、结构新增/消失、未成型状态。

这些测试直接喂「并入前快照」，判定器结构上无法接触被判定样本本身；
先判定后并样本的端到端次序在 test_baselines.py 里验证。
"""
from __future__ import annotations

from app.comparison import (
    BUCKET_ABOVE_P99,
    BUCKET_BETWEEN_P95_P99,
    BUCKET_NORMAL,
    STATUS_DEGRADED,
    STATUS_DEGRADED_DRIFTED,
    STATUS_NORMAL,
    STATUS_UNFORMED,
    BaselineSnapshot,
    compare,
    compare_latency,
    compare_structure,
)
from app.entries import EntryKey, RequestFeatures
from app.structure import StructureSnapshot

ENTRY = EntryKey("gateway", "createOrder")
AB = ("gateway", "order")
BC = ("order", "mysql")
BD = ("order", "inventory")
BX = ("order", "riskcheck")


def features(
    duration: float = 100.0,
    edges=frozenset({AB, BC, BD}),
    trace_id: str = "t-new",
) -> RequestFeatures:
    return RequestFeatures(
        trace_id=trace_id,
        entry=ENTRY,
        duration=duration,
        edges=frozenset(edges),
        start_time=0.0,
        root_span_id="root",
    )


def snapshot(
    formed: bool = True,
    p50=95.0,
    p95=120.0,
    p99=180.0,
    version: int = 30,
    regular=frozenset({AB, BC, BD}),
    edge_counts=None,
    slow_factor: float = 1.5,
    threshold_freq: float = 0.8,
) -> BaselineSnapshot:
    structure = None
    if formed:
        structure = StructureSnapshot(
            sample_count=version,
            edge_counts=edge_counts or {AB: 30, BC: 30, BD: 29},
            regular_edges=frozenset(regular),
            frequency_threshold=threshold_freq,
        )
    return BaselineSnapshot(
        entry=ENTRY,
        version=version,
        formed=formed,
        min_samples=20,
        p50=p50 if formed else None,
        p95=p95 if formed else None,
        p99=p99 if formed else None,
        slow_factor=slow_factor,
        structure=structure,
    )


class TestLatencyComparison:
    def test_normal_duration_not_flagged(self) -> None:
        verdict = compare_latency(110.0, snapshot())
        assert verdict.degraded is False
        assert verdict.status == STATUS_NORMAL
        assert verdict.bucket == BUCKET_NORMAL
        # 阈值 = p95 * 倍数 = 120 * 1.5 = 180
        assert verdict.threshold == 180.0

    def test_between_p95_and_threshold_not_flagged(self) -> None:
        """高于 p95 但没超过 p95*倍数：慢，但不构成本口径下的劣化。"""
        verdict = compare_latency(150.0, snapshot())
        assert verdict.bucket == BUCKET_BETWEEN_P95_P99
        assert verdict.degraded is False

    def test_above_threshold_is_degradation(self) -> None:
        verdict = compare_latency(300.0, snapshot())
        assert verdict.degraded is True
        assert verdict.status == STATUS_DEGRADED
        assert verdict.bucket == BUCKET_ABOVE_P99
        assert verdict.ratio > 1.0

    def test_slow_factor_is_configurable(self) -> None:
        verdict = compare_latency(130.0, snapshot(slow_factor=1.0))
        # 倍数为 1 时阈值就是 p95=120，130 立即劣化
        assert verdict.degraded is True

    def test_exactly_at_threshold_not_degraded(self) -> None:
        verdict = compare_latency(180.0, snapshot())
        assert verdict.degraded is False  # 严格大于才算


class TestStructureComparison:
    def test_identical_structure_no_drift(self) -> None:
        verdict = compare_structure(frozenset({AB, BC, BD}), snapshot())
        assert verdict.drifted is False
        assert verdict.added_edges == []
        assert verdict.missing_edges == []

    def test_unexpected_edge_marked_added(self) -> None:
        verdict = compare_structure(
            frozenset({AB, BC, BD, BX}), snapshot()
        )
        assert verdict.added_edges == [BX]
        assert verdict.missing_edges == []
        assert verdict.drifted is True

    def test_missing_regular_edge_marked_missing(self) -> None:
        verdict = compare_structure(frozenset({AB, BC}), snapshot())
        assert verdict.added_edges == []
        assert verdict.missing_edges == [BD]

    def test_non_regular_edge_absent_is_not_drift(self) -> None:
        """一条偶发边（不在常规集合里）这次没出现，不能算消失。"""
        snap = snapshot(
            regular=frozenset({AB, BC}),
            edge_counts={AB: 30, BC: 30, BX: 2},
        )
        verdict = compare_structure(frozenset({AB, BC}), snap)
        assert verdict.drifted is False


class TestUnformedBaseline:
    def test_few_samples_gives_no_verdict(self) -> None:
        result = compare(features(duration=9999.0), snapshot(formed=False, version=3))
        assert result.status == STATUS_UNFORMED
        assert result.baseline_formed is False
        assert result.baseline_version == 3
        assert result.is_anomaly is False
        assert result.latency.degraded is False
        assert result.structure.added_edges == []
        assert result.structure.missing_edges == []
        assert result.latency.bucket == "unknown"


class TestCombinedResult:
    def test_combined_latency_and_structure_anomaly(self) -> None:
        result = compare(
            features(duration=500.0, edges=frozenset({AB, BC, BD, BX})),
            snapshot(),
        )
        assert result.status == STATUS_DEGRADED_DRIFTED
        assert result.is_anomaly is True
        assert result.latency.degraded is True
        assert result.structure.added_edges == [BX]

    def test_result_references_baseline_version_and_snapshot(self) -> None:
        result = compare(features(), snapshot(version=42))
        assert result.baseline_version == 42
        assert result.entry == ENTRY
        assert set(result.baseline_quantiles) == {"p50", "p95", "p99"}
        assert frozenset(result.baseline_regular_edges) == {AB, BC, BD}

    def test_normal_result_payload(self) -> None:
        result = compare(features(), snapshot())
        assert result.status == STATUS_NORMAL
        payload = result.to_dict()
        assert payload["baseline_version"] == 30
        assert payload["baseline"]["quantiles"]["p95"] == 120.0
        assert payload["structure"]["added_edges"] == []
