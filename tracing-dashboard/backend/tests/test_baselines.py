"""入口基线与对照判定测试。

锁住的关系：
* 延迟劣化：稳定样本后来一次远超平时的请求必判劣化，平时范围内不误报；
* 结构新增边 / 消失边 / 完全一致三种结构对照结果；
* 先判定后并样本：异常请求自身的加入不得改变判定它所用的基线；
* 样本不足最低门槛时一律“基线尚未成型”，跨过门槛后才正式判定；
* 无清晰根片段（全挂占位节点 / 多根）的请求不入任何入口、不改任何基线；
* 重启回放：只持久化片段，基线与对照结果能确定性重建。
"""
from __future__ import annotations

import pytest

from app.config import Settings
from app.entries import DEFAULT_ENTRY_OPERATION, EntryKey
from app.latency_baseline import LatencyBaseline
from app.models import SpanRecord
from app.service import TracingService
from app.structure_baseline import StructureBaseline


def cfg(**overrides) -> Settings:
    base = dict(
        database_path=":memory:",
        max_pending_wait_seconds=30,
        baseline_min_samples=5,
        latency_slow_multiplier=1.5,
        structure_regular_ratio=0.8,
    )
    base.update(overrides)
    return Settings(**base)


def span(
    trace_id: str,
    span_id: str,
    parent: str | None,
    service_name: str = "gateway",
    start: float = 0.0,
    end: float = 100.0,
    operation: str | None = "place_order",
) -> SpanRecord:
    return SpanRecord(trace_id, span_id, parent, service_name, start, end, 200,
                      operation=operation if parent is None else None)


def standard_trace(trace_id: str, duration: float = 100.0, t0: float = 0.0) -> list[SpanRecord]:
    """常规结构：gateway -> auth / order，端到端 duration ms，t0 为开始时间。"""
    return [
        span(trace_id, "root", None, "gateway", t0, t0 + duration),
        span(trace_id, "auth", "root", "auth", t0 + 10.0, t0 + 40.0),
        span(trace_id, "order", "root", "order", t0 + 20.0, t0 + 90.0),
    ]


ENTRY = EntryKey("gateway", "place_order")


@pytest.fixture
def service() -> TracingService:
    svc = TracingService(cfg())
    yield svc
    svc.close()


def feed(svc: TracingService, trace_id: str, spans: list[SpanRecord]) -> dict:
    svc.ingest(spans)
    result = svc.get_comparison(trace_id)
    assert result is not None, "完整树应当场定版并产生对照结果"
    return result


class TestLatencyBaselineUnit:
    def test_incremental_quantiles_tight_after_many_samples(self) -> None:
        """P² 增量维护：样本增多时分位数跟着更新，且大样本下贴合真实分位。"""
        baseline = LatencyBaseline()
        for i in range(1, 2001):
            baseline.observe(float(i))
        q = baseline.quantiles()
        assert baseline.sample_count == 2000
        assert 900 < q["p50"] < 1100
        assert 1850 < q["p95"] < 1950
        assert 1940 < q["p99"] < 2000


class TestStructureBaselineUnit:
    def test_rare_edge_is_not_regular(self) -> None:
        """只在极个别请求里出现一两次的边不算常规结构。"""
        baseline = StructureBaseline(regular_ratio=0.8)
        common = frozenset({("a", "b")})
        rare = frozenset({("a", "b"), ("a", "c")})
        for _ in range(9):
            baseline.observe(common)
        baseline.observe(rare)
        assert baseline.sample_count == 10
        assert baseline.frequency(("a", "b")) == 10
        assert baseline.is_regular(("a", "b"))
        assert baseline.frequency(("a", "c")) == 1
        assert not baseline.is_regular(("a", "c"))


class TestLatencyDegradation:
    def test_slow_request_flagged_normal_request_not(self, service: TracingService) -> None:
        """稳定样本后来一次远超平时的请求必判劣化；平时范围不误报。"""
        for i in range(5):
            r = feed(service, f"normal-{i}", standard_trace(f"normal-{i}", 100 + i))
            assert r["status"] == "immature"  # 判定时基线里还不到 5 条

        # 平时范围内的请求：p95≈104，阈值≈156，110 不算慢
        normal = feed(service, "normal-later", standard_trace("normal-later", 110))
        assert normal["status"] == "ok"
        assert normal["latency"]["verdict"] == "normal"
        assert normal["is_anomaly"] is False

        # 远超平时：必判延迟劣化
        slow = feed(service, "slow-1", standard_trace("slow-1", 1000))
        assert slow["status"] == "degraded"
        assert slow["latency"]["verdict"] == "degraded"
        assert slow["anomaly_types"] == ["latency"]
        assert slow["latency"]["band"] == "above_p99"
        assert slow["is_anomaly"] is True
        # 结构与平时一致，不能顺带报结构漂移
        assert slow["structure"]["added_edges"] == []
        assert slow["structure"]["missing_edges"] == []

    def test_band_location_reported(self, service: TracingService) -> None:
        # 平时耗时在 90~110 之间小幅波动，分位之间才有间隔可区分档位
        durations = [95, 100, 105, 98, 102]
        for i, d in enumerate(durations):
            feed(service, f"n-{i}", standard_trace(f"n-{i}", d))
        detail = service.get_entry_detail("gateway", "place_order")
        p50, p95 = detail["latency"]["p50_ms"], detail["latency"]["p95_ms"]
        mid = feed(service, "mid", standard_trace("mid", 104))
        assert p50 < 104 <= p95
        assert mid["latency"]["band"] == "p50_to_p95"
        assert mid["latency"]["verdict"] == "normal"


class TestStructureDrift:
    def _train(self, service: TracingService, prefix: str, n: int = 5) -> None:
        for i in range(n):
            feed(service, f"{prefix}-{i}", standard_trace(f"{prefix}-{i}"))

    def test_added_edge_marked(self, service: TracingService) -> None:
        """多出一条基线里从没有过的调用边：标为新增。"""
        self._train(service, "base")
        spans = standard_trace("extra", 100)
        # 多出来的意外调用 gateway -> cache
        spans.append(span("extra", "cache", "root", "cache", 30.0, 60.0))
        result = feed(service, "extra", spans)
        added = result["structure"]["added_edges"]
        missing = result["structure"]["missing_edges"]
        assert {(e["source"], e["target"]) for e in added} == {("gateway", "cache")}
        assert added[0]["difference"] == "added"
        assert missing == []
        assert result["structure"]["verdict"] == "drifted"
        assert "structure" in result["anomaly_types"]

    def test_missing_edge_marked(self, service: TracingService) -> None:
        """缺了某条常规边：标为消失。"""
        self._train(service, "base")
        # 本次只剩 gateway -> auth，常规的 gateway -> order 没了
        spans = [
            span("only", "root", None, "gateway", 0.0, 100.0),
            span("only", "auth", "root", "auth", 10.0, 40.0),
        ]
        result = feed(service, "only", spans)
        missing = result["structure"]["missing_edges"]
        assert {(e["source"], e["target"]) for e in missing} == {("gateway", "order")}
        assert missing[0]["difference"] == "missing"
        assert result["structure"]["added_edges"] == []
        assert result["structure"]["verdict"] == "drifted"
        assert "structure" in result["anomaly_types"]

    def test_identical_structure_no_diff(self, service: TracingService) -> None:
        """结构与平时完全一致：不产生任何结构偏差。"""
        self._train(service, "base")
        result = feed(service, "same", standard_trace("same"))
        assert result["structure"]["added_edges"] == []
        assert result["structure"]["missing_edges"] == []
        assert result["structure"]["verdict"] == "normal"


class TestJudgeBeforeMerge:
    def test_anomaly_does_not_dilute_its_own_baseline(self, service: TracingService) -> None:
        """异常请求对照的基线必须不含它自身。

        构造一个临界情形：5 个 ~100ms 样本的 p95≈104，阈值≈156，
        1000ms 必判劣化；假如先把这 1000ms 并进基线再判，p95 会被抬到
        1000、阈值 1500，结论反而变成正常。判定为劣化即证明用的是并入前基线。
        """
        for i in range(5):
            feed(service, f"n-{i}", standard_trace(f"n-{i}", 100))

        slow = feed(service, "anomaly", standard_trace("anomaly", 1000))
        # 比的是第 5 版基线（5 个历史样本），快照分位里不可能出现 1000
        assert slow["baseline_version"] == 5
        assert slow["sample_count_before"] == 5
        assert slow["latency"]["quantiles"]["p95"] < 200
        assert slow["latency"]["verdict"] == "degraded"

        # 并入之后基线才是 6 条、分位才被这次异常抬动
        detail = service.get_entry_detail("gateway", "place_order")
        assert detail["sample_count"] == 6
        assert detail["latency"]["p95_ms"] > 500

        # 对照结果是固化的快照：后续再来样本也不改写它当年依据的基线版本
        for i in range(6, 9):
            feed(service, f"later-{i}", standard_trace(f"later-{i}", 100))
        again = service.get_comparison("anomaly")
        assert again["baseline_version"] == 5
        assert again["latency"]["quantiles"]["p95"] < 200
        assert again["is_anomaly"] is True

    def test_identical_to_judging_from_history_only(
        self, tmp_path
    ) -> None:
        """另一种验证：只喂历史样本的基线 + 单独对照，结论必须与流水线一致。"""
        db = str(tmp_path / "trace.db")
        settings = cfg(database_path=db)
        svc = TracingService(settings)
        for i in range(5):
            feed(svc, f"n-{i}", standard_trace(f"n-{i}", 100))
        pipeline_result = feed(svc, "anomaly", standard_trace("anomaly", 1000))
        svc.close()

        # 独立基线：只含 5 个历史样本，不含 anomaly
        latency = LatencyBaseline()
        structure = StructureBaseline(regular_ratio=0.8)
        for i in range(5):
            latency.observe(100.0)
            structure.observe(
                frozenset({("gateway", "auth"), ("gateway", "order")})
            )
        assert latency.quantiles()["p95"] == pipeline_result["latency"]["quantiles"]["p95"]
        assert structure.regular_edges() == {("gateway", "auth"), ("gateway", "order")}


class TestImmatureBaseline:
    def test_no_verdict_below_threshold(self) -> None:
        """样本不足门槛：任何请求都标“基线尚未成型”，不给正式判定。"""
        svc = TracingService(cfg(baseline_min_samples=10))
        try:
            for i in range(9):
                result = feed(svc, f"few-{i}", standard_trace(f"few-{i}", 100))
                # 判定时基线里只有 i 条，i < 10，一律未成型
                assert result["status"] == "immature"
                assert result["latency"] is None
                assert result["structure"] is None
                assert result["is_anomaly"] is False
                assert result["baseline_version"] == i
            detail = svc.get_entry_detail("gateway", "place_order")
            assert detail["sample_count"] == 9
            assert detail["baseline_ready"] is False
            # 第 10 个请求判定时基线仍只有 9 条：即使 10 万 ms 也不能被指认为异常
            wild = feed(svc, "wild", standard_trace("wild", 100_000))
            assert wild["status"] == "immature"
            assert wild["is_anomaly"] is False
            assert wild["latency"] is None
        finally:
            svc.close()

    def test_formal_verdict_after_crossing_threshold(self) -> None:
        """跨过门槛后才开始正式判定（判定时已有样本数 >= 门槛）。"""
        svc = TracingService(cfg(baseline_min_samples=5))
        try:
            for i in range(5):
                feed(svc, f"n-{i}", standard_trace(f"n-{i}", 100))
            ready = feed(svc, "ready", standard_trace("ready", 100))
            assert ready["status"] == "ok"
            assert ready["latency"] is not None
            assert ready["structure"] is not None
            assert ready["baseline_version"] == 5
        finally:
            svc.close()


class TestArrivalOrdering:
    def test_tree_with_pending_child_not_sampled_until_complete(self) -> None:
        """根已到、但有子片段在等待父片段时不入样；归位后才判定并入。

        构造“根 + 一个等不到父的孤儿”的混合树：pending 未清空前整个 trace
        不会提前入样；孤儿超时归位到占位节点后，根唯一，正常入样
        （孤儿不产生边）。
        """
        svc = TracingService(cfg(max_pending_wait_seconds=0))
        try:
            svc.ingest([
                span("mix", "root", None, "gateway", 1000.0, 1100.0),
                # 父片段不存在，等待超时立即归位到占位节点
                span("mix", "orphan", "ghost", "notify", 1010.0, 1040.0),
            ])
            # max_wait=0：ingest 当场 flush_expired，孤儿立刻归位、trace 当场定版
            result = svc.get_comparison("mix")
            assert result is not None
            assert result["entry"] == {
                "service": "gateway",
                "operation": "place_order",
            }
            assert result["status"] == "immature"  # 首个样本，基线未成型
            detail = svc.get_entry_detail("gateway", "place_order")
            pairs = {(e["source"], e["target"]) for e in detail["structure"]["edges"]}
            assert pairs == set()  # 孤儿在占位节点下，不产生边
        finally:
            svc.close()

    def test_pending_blocks_finalization_until_parent_attaches(self) -> None:
        """子片段先到处于 pending 时不定版；父片段晚到把它挂正后才入样。"""
        svc = TracingService(cfg())
        try:
            # 子片段先到：没有根、在 pending，不能进任何入口
            svc.ingest([span("late", "child", "root", "order", 10, 90)])
            assert svc.get_comparison("late") is None
            assert svc.list_entries()["entries"] == []

            # 根（父）片段到达：挂正后定版，结构边 gateway -> order 被采集
            svc.ingest([span("late", "root", None, "gateway", 0, 100)])
            result = svc.get_comparison("late")
            assert result is not None
            assert result["entry"] == {
                "service": "gateway",
                "operation": "place_order",
            }
            detail = svc.get_entry_detail("gateway", "place_order")
            pairs = {(e["source"], e["target"]) for e in detail["structure"]["edges"]}
            assert ("gateway", "order") in pairs
        finally:
            svc.close()

    def test_root_only_tree_samples_immediately(self) -> None:
        """根片段自身就是一棵完整树：没有挂起片段，立即入样（边为空）。"""
        svc = TracingService(cfg())
        try:
            svc.ingest([span("solo", "root", None, "gateway", 0, 100)])
            result = svc.get_comparison("solo")
            assert result is not None
            assert result["entry"] == {
                "service": "gateway",
                "operation": "place_order",
            }
            detail = svc.get_entry_detail("gateway", "place_order")
            assert detail["sample_count"] == 1
        finally:
            svc.close()

    def test_orphan_waiting_then_timeout_marked_unclassified(self) -> None:
        """孤儿片段等待期间不定版；超时归位为占位节点后判不可归类。"""
        svc = TracingService(cfg(max_pending_wait_seconds=30))
        try:
            import time as _time

            wall = _time.time() * 1000.0
            svc.ingest([span("orphan-wait", "o1", "ghost", "notify", wall, wall + 100)])
            assert svc.get_comparison("orphan-wait") is None
            assert not svc.baselines.is_unclassified("orphan-wait")
            # 超时归位后定版：无清晰根片段，判不可归类
            flushed = svc.assembler.flush_expired(now_ms=wall + 60_000.0)
            assert "orphan-wait" in flushed
            svc._finalize_if_ready(set(flushed), wall + 60_000.0)
            assert svc.baselines.is_unclassified("orphan-wait")
            assert svc.list_entries()["entries"] == []
        finally:
            svc.close()


class TestRootlessTraces:
    def test_placeholder_only_trace_not_classified(self) -> None:
        """整棵树都挂在“父片段缺失”占位节点下：不入任何入口、不改任何基线。"""
        svc = TracingService(cfg(max_pending_wait_seconds=0))
        try:
            svc.ingest([span("orphan-1", "o1", "ghost", "notify", 0, 100)])
            assert svc.get_comparison("orphan-1") is None
            assert svc.baselines.is_unclassified("orphan-1")
            assert svc.list_entries()["entries"] == []

            # 再来一个正常入口的请求，基线样本里不能混入孤儿
            for i in range(5):
                feed(svc, f"ok-{i}", standard_trace(f"ok-{i}", 100))
            detail = svc.get_entry_detail("gateway", "place_order")
            assert detail["sample_count"] == 5
            edges = {(e["source"], e["target"]) for e in detail["structure"]["edges"]}
            assert "notify" not in {s for pair in edges for s in pair}
        finally:
            svc.close()

    def test_multiple_roots_not_classified(self, service: TracingService) -> None:
        """同 trace 有两个根片段：入口归属有歧义，不归类。"""
        service.ingest([
            span("dup", "r1", None, "gateway", 0, 100),
            span("dup", "r2", None, "billing", 0, 100),
        ])
        assert service.get_comparison("dup") is None
        assert service.baselines.is_unclassified("dup")
        assert service.list_entries()["entries"] == []

    def test_operation_defaults_and_partitions(self, service: TracingService) -> None:
        """operation 缺省归一到 default；不同入口各自独立样本池。"""
        no_op = [
            SpanRecord("a", "root", None, "gateway", 0, 100, 200, operation=None),
        ]
        other = [
            SpanRecord("b", "root", None, "gateway", 0, 100, 200, operation="refund"),
        ]
        feed(service, "a", no_op)
        feed(service, "b", other)
        entries = {
            (e["service"], e["operation"]): e
            for e in service.list_entries()["entries"]
        }
        assert ("gateway", DEFAULT_ENTRY_OPERATION) in entries
        assert ("gateway", "refund") in entries
        # 两个入口样本池互不串
        assert entries[("gateway", DEFAULT_ENTRY_OPERATION)]["sample_count"] == 1
        assert entries[("gateway", "refund")]["sample_count"] == 1


class TestReplay:
    def test_baselines_rebuilt_from_spans_only(self, tmp_path) -> None:
        """重启回放：只持久化片段，基线与对照结果确定性重建。"""
        db = str(tmp_path / "trace.db")
        settings = cfg(database_path=db)
        svc = TracingService(settings)
        for i in range(5):
            feed(svc, f"n-{i}", standard_trace(f"n-{i}", 100, t0=float(i) * 1000))
        before = feed(svc, "anomaly", standard_trace("anomaly", 1000, t0=9000))
        svc.close()

        svc2 = TracingService(settings)
        try:
            after = svc2.get_comparison("anomaly")
            assert after is not None
            assert after["status"] == "degraded"
            assert after["baseline_version"] == before["baseline_version"]
            assert after["latency"] == before["latency"]
            assert after["structure"] == before["structure"]
            detail = svc2.get_entry_detail("gateway", "place_order")
            assert detail["sample_count"] == 6
            recent = {r["trace_id"] for r in detail["recent_anomalies"]}
            assert "anomaly" in recent
        finally:
            svc2.close()
