"""对照判定：把「这次请求」与「并入它之前的基线」摆在一起比。

严格次序（由 BaselineTracker 保证，本模块只接受已经冻结的快照）：

    快照（不含本次） -> compare(本次, 快照) -> 落对照结果 -> 并入本次样本

判定器拿到的 BaselineSnapshot 是只读的，结构上没有途径把本次请求
先塞进基线再自比，从根上避免异常被自己稀释。

两项对照：
* 延迟：本次端到端耗时落在基线的哪个分位区间；超过 p95 * 慢倍数判劣化；
* 结构：本次边集合相对快照里的常规边做差——
  新增 = 本次有、常规路径里没有；消失 = 常规路径有、本次没有。

样本不足最低门槛时状态为 ``unformed``（基线尚未成型），不给任何正式判定。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .entries import Edge, EntryKey, RequestFeatures
from .latency import QUANTILES
from .structure import StructureSnapshot

# 对照结果的一版格式
COMPARISON_VERSION = 1

# 基线状态
STATUS_UNFORMED = "unformed"   # 样本不足，基线尚未成型
STATUS_NORMAL = "normal"       # 基线成型，本次未发现偏差
STATUS_DEGRADED = "degraded"   # 延迟劣化
STATUS_DRIFTED = "drifted"     # 结构漂移（新增/消失边）
# 延迟劣化与结构漂移同时出现
STATUS_DEGRADED_DRIFTED = "degraded_drifted"

# 本次耗时相对基线分位的落点
BUCKET_UNKNOWN = "unknown"     # 基线未成型，无法定位
BUCKET_NORMAL = "normal"       # <= p95
BUCKET_BETWEEN_P95_P99 = "between_p95_p99"
BUCKET_ABOVE_P99 = "above_p99"


@dataclass(frozen=True)
class BaselineSnapshot:
    """一次对照所依据的、不含被判定请求本身的基线版本。"""

    entry: EntryKey
    # 该入口并入本次请求之前的样本数；同时作为基线版本号（每并一条 +1）
    version: int
    formed: bool
    min_samples: int
    p50: Optional[float]
    p95: Optional[float]
    p99: Optional[float]
    slow_factor: float
    structure: Optional[StructureSnapshot]


@dataclass
class LatencyVerdict:
    status: str
    bucket: str
    duration: float
    threshold: Optional[float]      # p95 * slow_factor
    ratio: Optional[float]          # duration / threshold
    degraded: bool


@dataclass
class StructureVerdict:
    added_edges: list[Edge] = field(default_factory=list)
    missing_edges: list[Edge] = field(default_factory=list)

    @property
    def drifted(self) -> bool:
        return bool(self.added_edges or self.missing_edges)


@dataclass
class ComparisonResult:
    trace_id: str
    entry: EntryKey
    # 判定所依据的基线版本（= 并入前样本数）
    baseline_version: int
    baseline_formed: bool
    min_samples: int
    status: str
    latency: LatencyVerdict
    structure: StructureVerdict
    # 判定时基线的分位/常规边快照，供前端并排展示、也便于事后复核
    baseline_quantiles: dict[str, Optional[float]]
    baseline_regular_edges: list[Edge]
    structure_edge_frequency: float

    @property
    def is_anomaly(self) -> bool:
        return self.status in (
            STATUS_DEGRADED,
            STATUS_DRIFTED,
            STATUS_DEGRADED_DRIFTED,
        )

    def to_dict(self) -> dict:
        def edge(edge: Edge) -> dict:
            return {"caller": edge[0], "callee": edge[1]}

        return {
            "trace_id": self.trace_id,
            "entry": self.entry.to_dict(),
            "entry_name": self.entry.display_name,
            "version": COMPARISON_VERSION,
            "baseline_version": self.baseline_version,
            "baseline_formed": self.baseline_formed,
            "min_samples": self.min_samples,
            "status": self.status,
            "is_anomaly": self.is_anomaly,
            "latency": {
                "status": self.latency.status,
                "bucket": self.latency.bucket,
                "duration": self.latency.duration,
                "threshold": self.latency.threshold,
                "ratio": self.latency.ratio,
                "degraded": self.latency.degraded,
            },
            "structure": {
                "added_edges": [edge(e) for e in self.structure.added_edges],
                "missing_edges": [edge(e) for e in self.structure.missing_edges],
                "drifted": self.structure.drifted,
            },
            "baseline": {
                "quantiles": {
                    name: _round(value)
                    for name, value in self.baseline_quantiles.items()
                },
                "regular_edges": [edge(e) for e in self.baseline_regular_edges],
                "structure_edge_frequency": self.structure_edge_frequency,
            },
        }


def _round(value: Optional[float]) -> Optional[float]:
    return round(value, 3) if value is not None else None


def _latency_bucket(duration: float, snapshot: BaselineSnapshot) -> str:
    p95 = snapshot.p95
    p99 = snapshot.p99
    if p95 is None:
        return BUCKET_UNKNOWN
    if duration <= p95:
        return BUCKET_NORMAL
    if p99 is not None and duration > p99:
        return BUCKET_ABOVE_P99
    return BUCKET_BETWEEN_P95_P99


def compare_latency(
    duration: float, snapshot: BaselineSnapshot
) -> LatencyVerdict:
    """延迟对照：定位分位区间，并按 p95 * slow_factor 判定是否劣化。"""
    if not snapshot.formed or snapshot.p95 is None:
        return LatencyVerdict(
            status=STATUS_UNFORMED,
            bucket=BUCKET_UNKNOWN,
            duration=duration,
            threshold=None,
            ratio=None,
            degraded=False,
        )
    threshold = snapshot.p95 * snapshot.slow_factor
    degraded = duration > threshold
    return LatencyVerdict(
        status=STATUS_DEGRADED if degraded else STATUS_NORMAL,
        bucket=_latency_bucket(duration, snapshot),
        duration=duration,
        threshold=threshold,
        ratio=duration / threshold if threshold > 0 else None,
        degraded=degraded,
    )


def compare_structure(
    edges: frozenset[Edge], snapshot: BaselineSnapshot
) -> StructureVerdict:
    """结构对照：相对基线常规路径找新增边与消失边。"""
    if not snapshot.formed or snapshot.structure is None:
        return StructureVerdict()
    regular = snapshot.structure.regular_edges
    added = sorted(edges - regular)
    missing = sorted(regular - edges)
    return StructureVerdict(added_edges=added, missing_edges=missing)


def compare(features: RequestFeatures, snapshot: BaselineSnapshot) -> ComparisonResult:
    """对一次请求做完整对照。``snapshot`` 必须是并入该请求之前的版本。"""
    if not snapshot.formed:
        latency = compare_latency(features.duration, snapshot)
        return ComparisonResult(
            trace_id=features.trace_id,
            entry=features.entry,
            baseline_version=snapshot.version,
            baseline_formed=False,
            min_samples=snapshot.min_samples,
            status=STATUS_UNFORMED,
            latency=latency,
            structure=StructureVerdict(),
            baseline_quantiles={name: None for name in QUANTILES},
            baseline_regular_edges=[],
            structure_edge_frequency=(
                snapshot.structure.frequency_threshold
                if snapshot.structure is not None
                else 0.0
            ),
        )

    latency = compare_latency(features.duration, snapshot)
    structure = compare_structure(features.edges, snapshot)

    if latency.degraded and structure.drifted:
        status = STATUS_DEGRADED_DRIFTED
    elif latency.degraded:
        status = STATUS_DEGRADED
    elif structure.drifted:
        status = STATUS_DRIFTED
    else:
        status = STATUS_NORMAL

    assert snapshot.structure is not None
    return ComparisonResult(
        trace_id=features.trace_id,
        entry=features.entry,
        baseline_version=snapshot.version,
        baseline_formed=True,
        min_samples=snapshot.min_samples,
        status=status,
        latency=latency,
        structure=structure,
        baseline_quantiles={
            "p50": snapshot.p50,
            "p95": snapshot.p95,
            "p99": snapshot.p99,
        },
        baseline_regular_edges=sorted(snapshot.structure.regular_edges),
        structure_edge_frequency=snapshot.structure.frequency_threshold,
    )
