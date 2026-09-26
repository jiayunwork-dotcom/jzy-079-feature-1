"""入口基线与对照的端到端测试。

锁住的判据：
1. 远超历史的请求判延迟劣化，平时范围内的请求不误报；
2. 多出基线从未有过的边 -> 新增；缺常规边 -> 消失；结构一致 -> 无偏差；
3. 先判定后并样本：判定依据的基线版本/分位不含被判定请求自身；
4. 样本不足最低门槛 -> 基线未成型，不给任何正式异常判定；
5. 无清晰根片段的请求不入任何入口样本、不改任何基线。
另含重启重建后对照结果与基线保持一致。
"""
from __future__ import annotations

import os

import pytest

from app.config import Settings
from app.models import SpanRecord
from app.service import TracingService


def make_settings(**overrides) -> Settings:
    defaults = dict(
        database_path=":memory:",
        max_pending_wait_seconds=30,
        baseline_min_samples=5,
        latency_slow_factor=1.5,
        structure_edge_frequency=0.8,
    )
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.fixture
def service() -> TracingService:
    svc = TracingService(make_settings())
    yield svc
    svc.close()


def span(trace, sid, parent, service_name, start, end, operation=None) -> SpanRecord:
    return SpanRecord(trace, sid, parent, service_name, start, end, 200, operation)


# 标准入口 gateway/createOrder 的常规结构：
# gateway -> order -> mysql/inventory
def order_trace(
    trace: str,
    base: float = 1000.0,
    duration: float = 100.0,
    extra_edge: bool = False,
    drop_inventory: bool = False,
    operation: str = "createOrder",
    root_service: str = "gateway",
) -> list[SpanRecord]:
    spans = [
        span(trace, "root", None, root_service, base, base + duration, operation),
        span(trace, "order", "root", "order", base + 5, base + duration - 5),
        span(trace, "mysql", "order", "mysql", base + 10, base + 40),
    ]
    if not drop_inventory:
        spans.append(
            span(trace, "inventory", "order", "inventory", base + 12, base + 30)
        )
    if extra_edge:
        spans.append(
            span(trace, "riskcheck", "order", "riskcheck", base + 14, base + 28)
        )
    return spans


def warm_up(service: TracingService, n: int = 6, **kwargs) -> None:
    for i in range(n):
        service.ingest(order_trace(f"normal-{i}", base=10_000.0 + i * 1000, **kwargs))


class TestLatencyDegradation:
    def test_much_slower_request_is_degraded(self, service: TracingService) -> None:
        warm_up(service, n=6)  # 每次都是整齐的 100ms
        service.ingest(order_trace("slow-1", base=90_000.0, duration=400.0))
        result = service.get_comparison("slow-1")
        assert result["status"] in ("degraded", "degraded_drifted")
        assert result["latency"]["degraded"] is True
        assert result["is_anomaly"] is True
        # 阈值 = 基线 p95 * 1.5；平时 100ms，阈值在 150 附近
        assert result["latency"]["threshold"] < 200.0
        assert result["latency"]["duration"] == 400.0

    def test_normal_request_is_not_falsely_flagged(
        self, service: TracingService
    ) -> None:
        warm_up(service, n=6)
        service.ingest(order_trace("fine-1", base=90_000.0, duration=100.0))
        result = service.get_comparison("fine-1")
        assert result["latency"]["degraded"] is False
        assert result["status"] == "normal"
        assert result["is_anomaly"] is False

    def test_moderately_slower_within_factor_not_flagged(
        self, service: TracingService
    ) -> None:
        """比 p95 慢一些但没超过 p95*倍数：不判劣化。"""
        warm_up(service, n=6)
        service.ingest(order_trace("mid-1", base=90_000.0, duration=140.0))
        result = service.get_comparison("mid-1")
        assert result["latency"]["degraded"] is False


class TestStructureDrift:
    def test_unseen_edge_marked_as_added(self, service: TracingService) -> None:
        warm_up(service, n=6)
        service.ingest(order_trace("extra-1", base=90_000.0, extra_edge=True))
        result = service.get_comparison("extra-1")
        added = {(e["caller"], e["callee"]) for e in result["structure"]["added_edges"]}
        assert ("order", "riskcheck") in added
        assert result["structure"]["missing_edges"] == []
        assert result["status"] in ("drifted", "degraded_drifted")

    def test_missing_regular_edge_marked_as_missing(
        self, service: TracingService
    ) -> None:
        warm_up(service, n=6)
        service.ingest(order_trace("missing-1", base=90_000.0, drop_inventory=True))
        result = service.get_comparison("missing-1")
        missing = {
            (e["caller"], e["callee"])
            for e in result["structure"]["missing_edges"]
        }
        assert ("order", "inventory") in missing
        assert result["structure"]["added_edges"] == []

    def test_identical_structure_has_no_drift(self, service: TracingService) -> None:
        warm_up(service, n=6)
        service.ingest(order_trace("same-1", base=90_000.0))
        result = service.get_comparison("same-1")
        assert result["structure"]["added_edges"] == []
        assert result["structure"]["missing_edges"] == []
        assert result["structure"]["drifted"] is False
        assert result["status"] == "normal"

    def test_added_edge_is_not_in_baseline_used_for_verdict(
        self, service: TracingService
    ) -> None:
        warm_up(service, n=6)
        service.ingest(order_trace("extra-2", base=90_000.0, extra_edge=True))
        result = service.get_comparison("extra-2")
        regular = {
            (e["caller"], e["callee"]) for e in result["baseline"]["regular_edges"]
        }
        # 判定依据的常规边集合里绝不能包含它自己刚带来的新边
        assert ("order", "riskcheck") not in regular


class TestCompareBeforeMerge:
    def test_baseline_version_excludes_the_request_being_judged(
        self, service: TracingService
    ) -> None:
        warm_up(service, n=6)
        entry = service.baselines.entries()[0]
        count_before = service.baselines.sample_count(entry)
        assert count_before == 6

        service.ingest(order_trace("slow-order", base=90_000.0, duration=500.0))
        result = service.get_comparison("slow-order")
        # 判定所用基线版本 = 并入前样本数；并入之后入口已有 7 个样本
        assert result["baseline_version"] == 6
        assert service.baselines.sample_count(entry) == 7

    def test_judgement_quantiles_equal_pre_merge_snapshot(
        self, service: TracingService
    ) -> None:
        warm_up(service, n=6)
        entry = service.baselines.entries()[0]
        # 并入前把基线快照记下来
        before = service.baselines.quantiles(entry)

        service.ingest(order_trace("slow-order-2", base=90_000.0, duration=900.0))
        result = service.get_comparison("slow-order-2")

        # 对照结果里记录的分位必须与并入前快照完全一致
        assert result["baseline"]["quantiles"]["p50"] == round(before["p50"], 3)
        assert result["baseline"]["quantiles"]["p95"] == round(before["p95"], 3)
        assert result["baseline"]["quantiles"]["p99"] == round(before["p99"], 3)
        # 且这次异常本身确实在判定之后才进入样本池
        assert result["baseline_version"] == service.baselines.sample_count(entry) - 1

    def test_anomaly_cannot_dilute_its_own_verdict(
        self, service: TracingService
    ) -> None:
        """构造一条极端慢请求：即使它大到能影响分位，也不影响对它的判定。"""
        warm_up(service, n=6)
        # 常规 100ms，阈值约 150ms；800ms 无论如何都是劣化
        service.ingest(order_trace("huge", base=90_000.0, duration=800.0))
        result = service.get_comparison("huge")
        assert result["latency"]["degraded"] is True
        assert result["latency"]["threshold"] < 200.0
        assert result["baseline_formed"] is True

    def test_trace_is_judged_only_once(self, service: TracingService) -> None:
        warm_up(service, n=6)
        service.ingest(order_trace("once", base=90_000.0, duration=100.0))
        entry = service.baselines.entries()[0]
        count_after = service.baselines.sample_count(entry)
        # 重复触发 settle（迟到片段、周期 tick）不能二次入样
        service._settle({"once"}, 99_000.0)
        service._settle_all()
        assert service.baselines.sample_count(entry) == count_after


class TestBaselineUnformed:
    def test_under_min_samples_all_verdict_unformed(self) -> None:
        svc = TracingService(make_settings(baseline_min_samples=5))
        # 只积累 3 个样本
        for i in range(3):
            svc.ingest(order_trace(f"few-{i}", base=10_000.0 + i * 1000))
        for i in range(3):
            result = svc.get_comparison(f"few-{i}")
            assert result["status"] == "unformed"
            assert result["baseline_formed"] is False
            assert result["is_anomaly"] is False

        # 哪怕这条慢得离谱，样本不足也不能指认它异常
        svc.ingest(order_trace("few-slow", base=90_000.0, duration=9999.0))
        slow = svc.get_comparison("few-slow")
        assert slow["status"] == "unformed"
        assert slow["latency"]["degraded"] is False
        svc.close()

    def test_formal_verdict_starts_after_crossing_threshold(self) -> None:
        svc = TracingService(make_settings(baseline_min_samples=5))
        for i in range(5):
            svc.ingest(order_trace(f"ramp-{i}", base=10_000.0 + i * 1000))
        # 第 6 个请求到来时基线已有 5 个样本，跨过门槛，开始正式判定
        svc.ingest(order_trace("ramp-slow", base=90_000.0, duration=600.0))
        result = svc.get_comparison("ramp-slow")
        assert result["baseline_formed"] is True
        assert result["baseline_version"] == 5
        assert result["latency"]["degraded"] is True

        svc.ingest(order_trace("ramp-ok", base=91_000.0, duration=100.0))
        ok = svc.get_comparison("ramp-ok")
        assert ok["baseline_formed"] is True
        assert ok["status"] == "normal"
        svc.close()

    def test_entry_list_reports_formed_state(self) -> None:
        svc = TracingService(make_settings(baseline_min_samples=5))
        for i in range(2):
            svc.ingest(order_trace(f"s-{i}", base=10_000.0 + i * 1000))
        row = svc.list_entries()["entries"][0]
        assert row["sample_count"] == 2
        assert row["baseline_formed"] is False
        assert row["min_samples"] == 5
        svc.close()


class TestRootlessNotSampled:
    def test_orphan_only_trace_creates_no_entry(self, service: TracingService) -> None:
        # 父片段永远不存在；超时后整棵树落在占位节点下
        service.ingest([
            span("orphan-1", "o1", "ghost", "notify", 1000.0, 1100.0),
        ])
        service.assembler.flush_expired(now_ms=float("inf"))
        service._settle({"orphan-1"}, float("inf"))

        assert service.baselines.is_excluded("orphan-1") is True
        assert service.list_entries()["entries"] == []
        assert service.get_comparison("orphan-1") is None

    def test_rootless_trace_does_not_pollute_existing_entry(
        self, service: TracingService
    ) -> None:
        warm_up(service, n=6)
        entry = service.baselines.entries()[0]
        before = service.baselines.sample_count(entry)

        service.ingest([
            span("orphan-2", "o2", "ghost", "gateway", 5000.0, 5100.0),
        ])
        service.assembler.flush_expired(now_ms=float("inf"))
        service._settle({"orphan-2"}, float("inf"))

        # 服务名恰好也是 gateway，也绝不能被算进 gateway/createOrder
        assert service.baselines.sample_count(entry) == before
        assert service.baselines.is_excluded("orphan-2") is True

    def test_trace_with_root_plus_orphan_is_still_sampled(
        self, service: TracingService
    ) -> None:
        """有真实根片段、只是附带等不到父的孤儿片段：请求照常归类。"""
        warm_up(service, n=6)
        service.ingest([
            span("mix", "root", None, "gateway", 6000.0, 6100.0, "createOrder"),
            span("mix", "order", "root", "order", 6010.0, 6090.0),
            span("mix", "mysql", "order", "mysql", 6020.0, 6080.0),
            span("mix", "inventory", "order", "inventory", 6025.0, 6070.0),
            span("mix", "lost", "never", "stray", 6030.0, 6060.0),
        ])
        service.assembler.flush_expired(now_ms=float("inf"))
        service._settle({"mix"}, float("inf"))
        result = service.get_comparison("mix")
        assert result is not None
        assert result["entry"]["root_service"] == "gateway"


class TestEntryIsolation:
    def test_different_operations_keep_separate_baselines(
        self, service: TracingService
    ) -> None:
        warm_up(service, n=6)  # createOrder
        for i in range(3):
            service.ingest(
                order_trace(
                    f"cancel-{i}",
                    base=20_000.0 + i * 1000,
                    operation="cancelOrder",
                )
            )
        entries = {e["name"]: e for e in service.list_entries()["entries"]}
        assert set(entries) == {"gateway:createOrder", "gateway:cancelOrder"}
        assert entries["gateway:createOrder"]["baseline_formed"] is True
        # cancelOrder 自己样本不足，不能蹭 createOrder 的基线
        assert entries["gateway:cancelOrder"]["baseline_formed"] is False

        # cancelOrder 下一次极慢请求仍是「未成型」，而 createOrder 已能判劣化
        service.ingest(
            order_trace("cancel-slow", base=90_000.0, duration=9999.0,
                        operation="cancelOrder")
        )
        assert service.get_comparison("cancel-slow")["status"] == "unformed"


class TestRebuildAcrossRestart:
    def test_baseline_and_verdict_survive_restart(self, tmp_path) -> None:
        db = os.path.join(tmp_path, "trace.db")
        cfg = make_settings(database_path=db)
        svc1 = TracingService(cfg)
        warm_up(svc1, n=6)
        svc1.ingest(order_trace("slow-restart", base=90_000.0, duration=500.0))
        expected = svc1.get_comparison("slow-restart")
        svc1.close()

        svc2 = TracingService(make_settings(database_path=db))
        restored = svc2.get_comparison("slow-restart")
        assert restored["status"] == expected["status"]
        assert restored["baseline_version"] == 6
        entry = svc2.baselines.entries()[0]
        assert svc2.baselines.sample_count(entry) == 7
        # 重启后再来的请求基线延续（版本号接着涨），不重头积累
        svc2.ingest(order_trace("after-restart", base=95_000.0, duration=500.0))
        after = svc2.get_comparison("after-restart")
        assert after["baseline_version"] == 7
        assert after["baseline_formed"] is True
        svc2.close()
