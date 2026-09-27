"""入口基线注册表：样本归集之上的增量维护 + 对照编排。

每个入口维护两样“平时的样子”：
* :class:`LatencyBaseline` —— 端到端耗时分布（p50/p95/p99，P² 增量更新）；
* :class:`StructureBaseline` —— 调用边出现频率与常规边集合。

不可动摇的次序（见 :meth:`EntryBaselines.process_sample`）：

1. 先取**并入前**的基线快照（版本号 = 当前样本数、分位数、常规边）；
2. 用这份快照对本次请求做对照判定（comparison.evaluate）；
3. 判定落定后，才把本次样本喂给两条基线，版本号 +1。

这样任何异常请求都不可能在判定时被它自己稀释——它对照的永远是
“没有它的那个平时”。重启回放时按 trace 开始时间逐个重放，次序一致，
基线版本与对照结果都能确定性重建，所以基线本身不另行落库，只持久化片段。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from threading import RLock

from . import comparison
from .entries import EntryKey, TraceSample
from .latency_baseline import LatencyBaseline
from .structure_baseline import StructureBaseline


@dataclass
class EntryBaselines:
    """单个入口的两份基线。最近异常列表由注册表按入口统一保管。"""

    key: EntryKey
    min_samples: int
    slow_multiplier: float
    regular_ratio: float
    latency: LatencyBaseline = field(default_factory=LatencyBaseline)
    structure: StructureBaseline = field(init=False)
    last_seen_ms: float = 0.0

    def __post_init__(self) -> None:
        self.structure = StructureBaseline(self.regular_ratio)

    @property
    def version(self) -> int:
        """基线版本 = 已并入的样本条数；也是下一次请求对照的版本号。"""
        return self.latency.sample_count

    # ------------------------------------------------------- 先判后并

    def process_sample(self, sample: TraceSample, seen_ms: float) -> dict:
        """对照一次请求并把它并入基线。返回对照结果。"""
        # 1) 并入前快照：版本、分位数、常规边，全部取自“没有它”的基线
        quantiles_before = self.latency.quantiles()
        regular_before = self.structure.regular_edges()

        # 2) 用并入前基线判定（纯函数，不触碰基线）
        result = comparison.evaluate(
            sample,
            baseline_version=self.version,
            sample_count=self.version,
            min_samples=self.min_samples,
            quantiles=quantiles_before,
            regular_edges=regular_before,
            slow_multiplier=self.slow_multiplier,
            regular_ratio=self.regular_ratio,
        )

        # 3) 判定落定后才并入：异常请求不可能在判定阶段稀释自己
        self.latency.observe(sample.duration_ms)
        self.structure.observe(sample.edges)
        self.last_seen_ms = seen_ms
        return result


class BaselineRegistry:
    """所有入口的基线、对照结果、不可归类记录。线程安全。"""

    def __init__(
        self,
        *,
        min_samples: int,
        slow_multiplier: float,
        regular_ratio: float,
        recent_limit: int = 50,
    ) -> None:
        self._min_samples = min_samples
        self._slow_multiplier = slow_multiplier
        self._regular_ratio = regular_ratio
        self._recent_limit = recent_limit
        self._entries: dict[EntryKey, EntryBaselines] = {}
        # trace_id -> 对照结果（含“尚未成型”的结果）
        self._results: dict[str, dict] = {}
        # 每个入口最近被判定为异常（延迟劣化 / 结构漂移）的对照结果
        self._recent: dict[EntryKey, deque] = {}
        # 已完成对照并入的 trace：每个请求只判定并入一次
        self._processed: set[str] = set()
        # 已判为不可归类的 trace（无清晰根 / 多根）：不进样本池、不再改口
        self._unclassified: set[str] = set()
        # 结构未定的 trace：仍有片段在等待父片段。标记后晚到片段会触发重看
        self._incomplete: set[str] = set()
        self._lock = RLock()

    @property
    def min_samples(self) -> int:
        return self._min_samples

    # ------------------------------------------------------------ 处理

    def process_sample(self, sample: TraceSample, seen_ms: float) -> dict:
        with self._lock:
            entry = self._entries.get(sample.entry)
            if entry is None:
                entry = EntryBaselines(
                    key=sample.entry,
                    min_samples=self._min_samples,
                    slow_multiplier=self._slow_multiplier,
                    regular_ratio=self._regular_ratio,
                )
                self._entries[sample.entry] = entry
            result = entry.process_sample(sample, seen_ms)
            self._processed.add(sample.trace_id)
            self._incomplete.discard(sample.trace_id)
            self._results[sample.trace_id] = result
            if result["is_anomaly"]:
                bucket = self._recent.setdefault(
                    sample.entry, deque(maxlen=self._recent_limit)
                )
                bucket.appendleft(result)
            return result

    def mark_unclassified(self, trace_id: str) -> None:
        """登记无清晰根 / 多根的 trace：永不入样、永不改基线。"""
        with self._lock:
            self._incomplete.discard(trace_id)
            self._unclassified.add(trace_id)

    def mark_incomplete(self, trace_id: str) -> None:
        """登记结构未定的 trace：父片段可能晚到，暂不定版。"""
        with self._lock:
            self._incomplete.add(trace_id)

    def is_known_trace(self, trace_id: str) -> bool:
        """已终局（入样或判不可归类）的 trace 不再重复定版；未定的仍可重看。"""
        with self._lock:
            return trace_id in self._processed or trace_id in self._unclassified

    def needs_revisit(self, trace_id: str) -> bool:
        with self._lock:
            return trace_id in self._incomplete

    # ------------------------------------------------------------ 查询

    def get_result(self, trace_id: str) -> dict | None:
        with self._lock:
            return self._results.get(trace_id)

    def list_entries(self) -> list[dict]:
        with self._lock:
            rows = []
            for entry in self._entries.values():
                q = entry.latency.quantiles()
                rows.append(
                    {
                        "service": entry.key.service,
                        "operation": entry.key.operation,
                        "sample_count": entry.version,
                        "baseline_ready": entry.version >= self._min_samples,
                        "min_samples": self._min_samples,
                        "p50_ms": _round(q["p50"]),
                        "p95_ms": _round(q["p95"]),
                        "p99_ms": _round(q["p99"]),
                        "regular_edge_count": len(entry.structure.regular_edges()),
                        "last_seen_ms": entry.last_seen_ms,
                        "recent_anomalies": [
                            _recent_item(r)
                            for r in self._recent.get(entry.key, ())
                        ],
                    }
                )
            rows.sort(key=lambda r: (-r["last_seen_ms"], r["service"], r["operation"]))
            return rows

    def get_entry_detail(self, service: str, operation: str) -> dict | None:
        from .entries import normalize_operation

        key = EntryKey(service, normalize_operation(operation))
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            q = entry.latency.quantiles()
            return {
                "service": entry.key.service,
                "operation": entry.key.operation,
                "sample_count": entry.version,
                "baseline_ready": entry.version >= self._min_samples,
                "min_samples": self._min_samples,
                "slow_multiplier": self._slow_multiplier,
                "latency": {
                    "p50_ms": _round(q["p50"]),
                    "p95_ms": _round(q["p95"]),
                    "p99_ms": _round(q["p99"]),
                },
                "structure": entry.structure.snapshot(),
                "recent_anomalies": [
                    _recent_item(r) for r in self._recent.get(key, ())
                ],
            }

    def is_unclassified(self, trace_id: str) -> bool:
        with self._lock:
            return trace_id in self._unclassified


def _round(value: float | None) -> float | None:
    return round(value, 3) if value is not None else None


def _recent_item(result: dict) -> dict:
    return {
        "trace_id": result["trace_id"],
        "status": result["status"],
        "anomaly_types": result["anomaly_types"],
        "duration_ms": result["duration_ms"],
        "baseline_version": result["baseline_version"],
        "latency_band": result["latency"]["band"] if result["latency"] else None,
        "added_edge_count": (
            len(result["structure"]["added_edges"]) if result["structure"] else 0
        ),
        "missing_edge_count": (
            len(result["structure"]["missing_edges"]) if result["structure"] else 0
        ),
    }
