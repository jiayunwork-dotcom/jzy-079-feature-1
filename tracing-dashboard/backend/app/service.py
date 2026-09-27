"""门面服务：把存储、拼接器、聚合器、入口基线、事件总线串起来。

HTTP 路由只跟 TracingService 打交道，不直接碰内部模块。

请求定版与入样的时机（基线相关逻辑的唯一约定，始终一致）：
* 一棵树所有片段都已归位（没有挂起等待父片段的片段）时定版；
* 定版时先按根片段归类——无清晰根片段（全挂占位节点 / 多根）的请求
  登记为不可归类，不进任何入口的样本池；
* 可归类的请求交给 BaselineRegistry：先用“并入前”基线做对照判定，
  判定落定后才并入样本（先判后并，异常请求不会稀释自己的基线）；
* 每个 trace 只定版一次；定版之后晚到的片段仍进树展示，但不再改基线。
"""
from __future__ import annotations

import time
from typing import Optional

from .baselines import BaselineRegistry
from .config import Settings
from .dependencies import aggregate_edges
from .entries import (
    CLASSIFY_PENDING,
    CLASSIFY_UNCLASSIFIABLE,
    classify_trace,
)
from .events import EventBus
from .models import SpanRecord
from .storage import SpanStore
from .tree import TraceAssembler

WINDOWS = {"1h": 3600 * 1000.0, "1d": 24 * 3600 * 1000.0}


class TracingService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.store = SpanStore(settings.database_path)
        self.assembler = TraceAssembler(settings.max_pending_wait_ms)
        self.bus = EventBus(settings.graph_push_debounce_seconds)
        self.baselines = BaselineRegistry(
            min_samples=settings.baseline_min_samples,
            slow_multiplier=settings.latency_slow_multiplier,
            regular_ratio=settings.structure_regular_ratio,
        )
        # 重启后从持久化片段重建拼接状态与入口基线
        self.assembler.load_from_storage(self.store.all_spans())
        self._replay_baselines()

    # ---------------------------------------------------------------- 上报

    def ingest(self, spans: list[SpanRecord]) -> dict:
        """入库一批已校验片段，返回每条的接收结果。"""
        now_ms = time.time() * 1000.0
        results: list[dict] = []
        touched_traces: set[str] = set()
        for span in spans:
            stored = self.store.insert_span(span, now_ms)
            outcome = self.assembler.add_span(span, now_ms=now_ms)
            status = "added" if (stored and outcome == "added") else "duplicate"
            results.append(
                {"trace_id": span.trace_id, "span_id": span.span_id, "status": status}
            )
            if status == "added":
                touched_traces.add(span.trace_id)
        # 顺手推进一次超时检查，让等不到父片段的子片段及时落入占位节点
        flushed = self.assembler.flush_expired(now_ms)
        touched_traces.update(flushed)
        # 已归位的树当场定版入样（含本次刚完整的、以及刚超时归位的）
        self._finalize_if_ready(touched_traces, now_ms)
        if touched_traces:
            self.bus.publish_spans(sorted(touched_traces), len(spans))
        return {
            "accepted": sum(1 for r in results if r["status"] == "added"),
            "duplicates": sum(1 for r in results if r["status"] == "duplicate"),
            "results": results,
        }

    def _finalize_if_ready(self, trace_ids: set[str], now_ms: float) -> None:
        """把已归位且尚未终局的 trace 定版：先判定，后并样本。

        之前标记为“未定（还在等父片段）”的 trace，在晚到片段把树补全后，
        会随本次涉及的 trace 一起在这里完成定版。
        """
        for trace_id in trace_ids:
            if self.baselines.is_known_trace(trace_id):
                continue
            edges = self.assembler.extract_edges(trace_id)
            outcome = classify_trace(trace_id, self.assembler, edges)
            if outcome == CLASSIFY_PENDING:
                # 还在等父片段：树不完整，登记为未定，父到了再重看
                self.baselines.mark_incomplete(trace_id)
                continue
            if outcome == CLASSIFY_UNCLASSIFIABLE:
                # 无清晰根片段 / 多根：不归类、不污染任何入口样本
                self.baselines.mark_unclassified(trace_id)
                continue
            self.baselines.process_sample(outcome, now_ms)

    def _replay_baselines(self) -> None:
        """重启回放：按最早开始时间（再按入库时间）逐个定版，次序确定、可重建。"""
        for trace_id in self.store.trace_replay_order():
            if self.baselines.is_known_trace(trace_id):
                continue
            bounds = self.assembler.trace_span_bounds(trace_id)
            seen_ms = bounds[0] if bounds else 0.0
            edges = self.assembler.extract_edges(trace_id)
            outcome = classify_trace(trace_id, self.assembler, edges)
            if outcome == CLASSIFY_PENDING:
                self.baselines.mark_incomplete(trace_id)
                continue
            if outcome == CLASSIFY_UNCLASSIFIABLE:
                self.baselines.mark_unclassified(trace_id)
                continue
            self.baselines.process_sample(outcome, seen_ms)

    # ---------------------------------------------------------------- 查询

    def get_tree(self, trace_id: str) -> Optional[dict]:
        return self.assembler.build_tree(trace_id)

    def list_traces(
        self, trace_id: Optional[str], service: Optional[str], limit: int
    ) -> list[dict]:
        return self.store.list_traces(trace_id=trace_id, service=service, limit=limit)

    def get_graph(self, window: str) -> dict:
        """按时间窗口聚合依赖图。窗口外的片段完全不参与，新旧窗口不串。"""
        window_ms = WINDOWS[window]
        since = time.time() * 1000.0 - window_ms
        recent = self.store.spans_starting_since(since)
        # 只统计本次窗口内的 trace 的边；逐 trace 从拼接器取真实父子边
        edges: list[tuple[str, str, float]] = []
        seen_traces: set[str] = set()
        for span in recent:
            if span.trace_id in seen_traces:
                continue
            seen_traces.add(span.trace_id)
            edges.extend(self.assembler.extract_edges(span.trace_id))
        graph = aggregate_edges(edges)
        graph["window"] = window
        graph["since"] = since
        return graph

    # ---------------------------------------------------- 入口基线查询

    def list_entries(self) -> dict:
        return {"entries": self.baselines.list_entries()}

    def get_entry_detail(self, service: str, operation: str) -> Optional[dict]:
        return self.baselines.get_entry_detail(service, operation)

    def get_comparison(self, trace_id: str) -> Optional[dict]:
        return self.baselines.get_result(trace_id)

    # ---------------------------------------------------------------- 维护

    def tick(self) -> None:
        """后台周期任务：推进超时占位 + 定版刚超时的树 + 冲刷防抖推送。"""
        now_ms = time.time() * 1000.0
        flushed = self.assembler.flush_expired(now_ms)
        if flushed:
            self._finalize_if_ready(set(flushed), now_ms)
            self.bus.publish_spans(sorted(flushed), 0)
        self.bus.flush_graph()
        self.bus.flush_baseline()

    def close(self) -> None:
        self.store.close()
