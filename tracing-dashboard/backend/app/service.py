"""门面服务：把存储、拼接器、聚合器、基线协调器、事件总线串起来。

HTTP 路由只跟 TracingService 打交道，不直接碰内部模块。
"""
from __future__ import annotations

import time
from typing import Optional

from .baselines import BaselineTracker, decode_edges
from .config import Settings
from .dependencies import aggregate_edges
from .entries import DEFAULT_OPERATION
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
        self.baselines = BaselineTracker(self.store, settings)
        # 重启后从持久化数据重建拼接状态
        self.assembler.load_from_storage(self.store.all_spans())
        # 重启后把「已拼好但还没做过归类」的历史请求补齐对照/入样
        self._settle_all()

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
        changed_entries = self._settle(touched_traces, now_ms)
        if touched_traces:
            self.bus.publish_spans(sorted(touched_traces), len(spans))
        if changed_entries:
            self.bus.publish_baselines(changed_entries)
        return {
            "accepted": sum(1 for r in results if r["status"] == "added"),
            "duplicates": sum(1 for r in results if r["status"] == "duplicate"),
            "results": results,
        }

    # ------------------------------------------------------ 对照/入样次序

    def _settle(self, trace_ids: set[str], now_ms: float) -> list[str]:
        """对已稳定的树做归类对照并入样，返回发生变化的入口名列表。"""
        changed: set[str] = set()
        for trace_id in trace_ids:
            tree = self.assembler.build_tree(trace_id, now_ms=now_ms)
            if tree is None:
                continue
            # 仍有挂起片段的请求先不动：等父片段到齐或超时落占位后再判定
            if not tree["complete"]:
                continue
            result = self.baselines.settle_tree(tree, now_ms)
            if result is not None:
                changed.add(result.entry.display_name)
        return sorted(changed)

    def _settle_all(self) -> None:
        """启动恢复：处理所有还没做过归类的已拼好请求。"""
        now_ms = time.time() * 1000.0
        for trace_id in self.assembler.trace_ids():
            tree = self.assembler.build_tree(trace_id, now_ms=now_ms)
            if tree is None or not tree["complete"]:
                continue
            self.baselines.settle_tree(tree, now_ms)

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

    # ------------------------------------------------------------ 入口对照

    def list_entries(self) -> dict:
        """列出所有入口：样本量、基线分位、成型状态、最近异常请求。"""
        anomalies = self.store.recent_anomalies_by_entries()
        entries: list[dict] = []
        for key in self.baselines.entries():
            count = self.baselines.sample_count(key)
            formed = self.baselines.is_formed(key)
            quantiles = self.baselines.quantiles(key)
            rows = anomalies.get((key.root_service, key.operation), [])
            entries.append(
                {
                    "root_service": key.root_service,
                    "operation": key.operation,
                    "name": key.display_name,
                    "is_default_operation": key.operation == DEFAULT_OPERATION,
                    "sample_count": count,
                    "baseline_formed": formed,
                    "min_samples": self.settings.baseline_min_samples,
                    "quantiles": {
                        name: _round(value)
                        for name, value in quantiles.items()
                    },
                    "recent_anomalies": [
                        self._comparison_row_summary(row) for row in rows
                    ],
                }
            )
        return {"entries": entries}

    def get_comparison(self, trace_id: str) -> Optional[dict]:
        result = self.baselines.get_result(trace_id)
        if result is None:
            return None
        payload = result.to_dict()
        # 结构基线完整明细（含非常规边与各自频率），供详情页与基线并排展示
        structure = self.baselines.structure_snapshot_of(result.entry)
        payload["baseline"]["structure"] = (
            structure.to_dict() if structure is not None else None
        )
        return payload

    def _comparison_row_summary(self, row: dict) -> dict:
        return {
            "trace_id": row["trace_id"],
            "status": row["status"],
            "duration": row["duration"],
            "threshold": row["threshold"],
            "bucket": row["bucket"],
            "added_edges": decode_edges(row["added_edges"]),
            "missing_edges": decode_edges(row["missing_edges"]),
            "compared_at": row["compared_at"],
        }

    # ---------------------------------------------------------------- 维护

    def tick(self) -> None:
        """后台周期任务：推进超时占位 + 冲刷防抖的图更新 + 补对照入样。"""
        now_ms = time.time() * 1000.0
        flushed = self.assembler.flush_expired(now_ms)
        changed_entries = self._settle(set(flushed), now_ms)
        if flushed:
            self.bus.publish_spans(sorted(flushed), 0)
        if changed_entries:
            self.bus.publish_baselines(changed_entries)
        self.bus.flush_graph()

    def close(self) -> None:
        self.store.close()


def _round(value: Optional[float]) -> Optional[float]:
    return round(value, 3) if value is not None else None
