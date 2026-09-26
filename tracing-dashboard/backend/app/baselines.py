"""入口基线协调器：样本池、两类基线、对照结果的统一维护。

职责边界：
* ``entries``     只管「这棵树属于哪个入口、长什么样」；
* ``latency``     只管端到端耗时分布的增量分位；
* ``structure``   只管调用边出现频率与常规边；
* ``comparison``  只在给定快照上做对照判定；
* 本模块负责把四者串起来，并守住关键次序与生命周期：

      1. 对一棵新树取「并入前」只读基线快照；
      2. 用快照做对照判定；
      3. 先把对照结果落库；
      4. 再把本次请求并入样本池、推进基线版本。

    因此任何请求的判定依据都不可能包含它自己。基线版本号即并入前的
    历史样本数，每次并入 +1。

重启后从持久化的样本重建基线（对照结果不重算，原样可读回）。
"""
from __future__ import annotations

import json
from typing import Optional

from .comparison import BaselineSnapshot, ComparisonResult, compare
from .config import Settings
from .entries import Edge, EntryKey, RequestFeatures, classify
from .latency import LatencyBaseline
from .storage import SpanStore
from .structure import StructureBaseline, StructureSnapshot

# 无清晰根片段的排除原因
REASON_NO_ROOT = "no_clear_root_span"


def _edges_json(edges: list[Edge]) -> str:
    return json.dumps([{"caller": a, "callee": b} for a, b in edges],
                      ensure_ascii=False)


def _quantiles_json(quantiles: dict[str, Optional[float]]) -> str:
    return json.dumps(quantiles, ensure_ascii=False)


def decode_edges(raw: str) -> list[Edge]:
    return [(item["caller"], item["callee"]) for item in json.loads(raw)]


class BaselineTracker:
    def __init__(self, store: SpanStore, settings: Settings) -> None:
        self._store = store
        self._min_samples = settings.baseline_min_samples
        self._slow_factor = settings.latency_slow_factor
        self._edge_frequency = settings.structure_edge_frequency
        self._latency: dict[EntryKey, LatencyBaseline] = {}
        self._structure: dict[EntryKey, StructureBaseline] = {}
        # 最新一次对照结果缓存在内存，同时已落库
        self._results: dict[str, ComparisonResult] = {}
        # 已处理（入样或排除）的 trace；重启后从持久化恢复，绝不二次入样
        self._settled: set[str] = set()
        self._excluded: set[str] = set()
        self.rebuild_from_storage()

    # ------------------------------------------------------------- 重建

    def rebuild_from_storage(self) -> None:
        """重启恢复：从持久化样本重放基线，对照结果按需从库里读。"""
        self._latency.clear()
        self._structure.clear()
        self._results.clear()
        self._settled = self._store.settled_trace_ids()
        self._excluded = self._store.excluded_trace_ids()
        for sample in self._store.all_samples():
            entry = EntryKey(sample["root_service"], sample["operation"])
            self._latency.setdefault(entry, LatencyBaseline()).add(
                sample["duration"]
            )
            edges = frozenset(self._store.sample_edges(sample["trace_id"]))
            self._structure.setdefault(
                entry, StructureBaseline(self._edge_frequency)
            ).add(edges)

    # ------------------------------------------------------------- 查询

    def is_settled(self, trace_id: str) -> bool:
        return trace_id in self._settled

    def is_excluded(self, trace_id: str) -> bool:
        return trace_id in self._excluded

    def entries(self) -> list[EntryKey]:
        return sorted(self._latency, key=lambda e: (e.root_service, e.operation))

    def sample_count(self, entry: EntryKey) -> int:
        baseline = self._latency.get(entry)
        return baseline.count if baseline is not None else 0

    def is_formed(self, entry: EntryKey) -> bool:
        return self.sample_count(entry) >= self._min_samples

    def quantiles(self, entry: EntryKey) -> dict[str, Optional[float]]:
        """入口当前延迟分位（用于列表展示）。"""
        baseline = self._latency.get(entry)
        if baseline is None:
            return {"p50": None, "p95": None, "p99": None}
        return {
            "p50": baseline.quantile("p50"),
            "p95": baseline.quantile("p95"),
            "p99": baseline.quantile("p99"),
        }

    def structure_snapshot_of(
        self, entry: EntryKey
    ) -> Optional["StructureSnapshot"]:
        baseline = self._structure.get(entry)
        return baseline.snapshot() if baseline is not None else None

    def edge_frequency_threshold(self) -> float:
        return self._edge_frequency

    def _snapshot(self, entry: EntryKey) -> BaselineSnapshot:
        """取入口当前（= 下一个请求并入前）的只读基线快照。"""
        latency = self._latency.get(entry)
        structure = self._structure.get(entry)
        count = latency.count if latency is not None else 0
        formed = count >= self._min_samples
        return BaselineSnapshot(
            entry=entry,
            version=count,
            formed=formed,
            min_samples=self._min_samples,
            p50=latency.quantile("p50") if latency is not None else None,
            p95=latency.quantile("p95") if latency is not None else None,
            p99=latency.quantile("p99") if latency is not None else None,
            slow_factor=self._slow_factor,
            structure=structure.snapshot() if structure is not None else None,
        )

    def get_result(self, trace_id: str) -> Optional[ComparisonResult]:
        cached = self._results.get(trace_id)
        if cached is not None:
            return cached
        # 重启后内存里没有：落库的对照结果原样取回
        row = self._store.get_comparison(trace_id)
        return None if row is None else _result_from_row(row)

    # ----------------------------------------------------- 归类/对照/并入

    def settle_tree(self, tree: dict, now_ms: float) -> Optional[ComparisonResult]:
        """对一棵已稳定的树执行：归类 -> 先判定 -> 落结果 -> 后并样本。

        已经处理过的 trace（含被排除的）直接返回 None，绝不二次入样。
        """
        trace_id = tree["trace_id"]
        if self.is_settled(trace_id):
            return None

        features: Optional[RequestFeatures] = classify(tree)
        if features is None:
            # 无清晰根片段：只记排除，不碰任何入口的基线
            self._settled.add(trace_id)
            self._excluded.add(trace_id)
            self._store.insert_excluded(trace_id, REASON_NO_ROOT, now_ms)
            return None

        # 1) 并入前快照（关键：此时基线不含本次请求）
        snapshot = self._snapshot(features.entry)
        # 2) 对照判定，用的就是并入前版本
        result = compare(features, snapshot)
        # 3) 先落对照结果
        self._persist_result(result, now_ms)
        # 4) 最后才把本次请求并入样本池、推进基线
        self._latency.setdefault(
            features.entry, LatencyBaseline()
        ).add(features.duration)
        self._structure.setdefault(
            features.entry, StructureBaseline(self._edge_frequency)
        ).add(features.edges)
        self._store.insert_sample(
            {
                "trace_id": features.trace_id,
                "root_service": features.entry.root_service,
                "operation": features.entry.operation,
                "root_span_id": features.root_span_id,
                "duration": features.duration,
                "start_time": features.start_time,
            },
            now_ms,
        )
        self._results[trace_id] = result
        self._settled.add(trace_id)
        return result

    def _persist_result(self, result: ComparisonResult, now_ms: float) -> None:
        latency = result.latency
        self._store.insert_comparison(
            {
                "trace_id": result.trace_id,
                "root_service": result.entry.root_service,
                "operation": result.entry.operation,
                "baseline_version": result.baseline_version,
                "baseline_formed": result.baseline_formed,
                "min_samples": result.min_samples,
                "status": result.status,
                "is_anomaly": result.is_anomaly,
                "duration": latency.duration,
                "threshold": latency.threshold,
                "bucket": latency.bucket,
                "added_edges": _edges_json(result.structure.added_edges),
                "missing_edges": _edges_json(result.structure.missing_edges),
                "baseline_quantiles": _quantiles_json(
                    result.baseline_quantiles
                ),
                "regular_edges": _edges_json(result.baseline_regular_edges),
                "compared_at": now_ms,
            }
        )


def _result_from_row(row: dict) -> ComparisonResult:
    """把落库的对照行还原成 ComparisonResult（重启后查看历史用）。"""
    from .comparison import (
        BUCKET_UNKNOWN,
        STATUS_UNFORMED,
        LatencyVerdict,
        StructureVerdict,
    )

    entry = EntryKey(row["root_service"], row["operation"])
    formed = bool(row["baseline_formed"])
    added = decode_edges(row["added_edges"])
    missing = decode_edges(row["missing_edges"])
    quantiles = json.loads(row["baseline_quantiles"])
    regular = decode_edges(row["regular_edges"])
    structure = StructureVerdict(added_edges=added, missing_edges=missing)
    latency = LatencyVerdict(
        status=row["status"] if formed else STATUS_UNFORMED,
        bucket=row["bucket"] if formed else BUCKET_UNKNOWN,
        duration=row["duration"],
        threshold=row["threshold"],
        ratio=(row["duration"] / row["threshold"])
        if row["threshold"] not in (None, 0)
        else None,
        degraded=row["status"] in ("degraded", "degraded_drifted"),
    )
    return ComparisonResult(
        trace_id=row["trace_id"],
        entry=entry,
        baseline_version=row["baseline_version"],
        baseline_formed=formed,
        min_samples=row["min_samples"],
        status=row["status"],
        latency=latency,
        structure=structure,
        baseline_quantiles=quantiles,
        baseline_regular_edges=regular,
        structure_edge_frequency=0.0,
    )
