"""结构基线：入口历史上稳定出现过哪些「谁调用谁」的调用边。

对每条边记录它在这个入口的多少次请求里出现过（同一请求内同一条边
只算一次，避免循环/重试把一次出现刷成多次）。出现频率达到可配置门槛
的边才算该入口的「常规路径」；只在个别请求里冒过一两次的边不算常规结构。

单样本更新为每条边一次计数，O(本次请求边数)，与历史样本量无关。
"""
from __future__ import annotations

from dataclasses import dataclass

from .entries import Edge

# 结构基线的一版快照：对照判定拿的就是并入当前请求之前的某一版
STRUCTURE_BASE_VERSION = 1


@dataclass(frozen=True)
class StructureSnapshot:
    """某个时刻的结构基线只读快照。"""

    sample_count: int
    # 边 -> 出现过的请求数
    edge_counts: dict[Edge, int]
    # 当前认定的常规边集合
    regular_edges: frozenset[Edge]
    # 认定常规边所用的频率门槛（0~1）
    frequency_threshold: float

    def edge_frequency(self, edge: Edge) -> float:
        if self.sample_count == 0:
            return 0.0
        return self.edge_counts.get(edge, 0) / self.sample_count

    def to_dict(self) -> dict:
        return {
            "sample_count": self.sample_count,
            "frequency_threshold": self.frequency_threshold,
            "edges": [
                {
                    "caller": caller,
                    "callee": callee,
                    "count": self.edge_counts[(caller, callee)],
                    "frequency": round(
                        self.edge_counts[(caller, callee)]
                        / max(self.sample_count, 1),
                        4,
                    ),
                    "regular": (caller, callee) in self.regular_edges,
                }
                for caller, callee in sorted(self.edge_counts)
            ],
        }


class StructureBaseline:
    """一个入口的调用边频率基线，增量维护。"""

    def __init__(self, frequency_threshold: float = 0.8) -> None:
        self.frequency_threshold = frequency_threshold
        self._edge_counts: dict[Edge, int] = {}
        self._sample_count = 0

    @property
    def sample_count(self) -> int:
        return self._sample_count

    def add(self, edges: frozenset[Edge]) -> None:
        """并入一次请求出现过的边集合（请求内已去重）。"""
        for edge in edges:
            self._edge_counts[edge] = self._edge_counts.get(edge, 0) + 1
        self._sample_count += 1

    def is_regular(self, edge: Edge) -> bool:
        if self._sample_count == 0:
            return False
        return self._edge_counts.get(edge, 0) / self._sample_count >= (
            self.frequency_threshold
        )

    def regular_edges(self) -> frozenset[Edge]:
        return frozenset(
            edge
            for edge, count in self._edge_counts.items()
            if count / self._sample_count >= self.frequency_threshold
        )

    def edge_counts_snapshot(self) -> dict[Edge, int]:
        return dict(self._edge_counts)

    def snapshot(self) -> StructureSnapshot:
        return StructureSnapshot(
            sample_count=self._sample_count,
            edge_counts=dict(self._edge_counts),
            regular_edges=self.regular_edges(),
            frequency_threshold=self.frequency_threshold,
        )
