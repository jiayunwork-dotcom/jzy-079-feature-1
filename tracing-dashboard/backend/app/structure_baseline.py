"""结构基线：入口历史上稳定出现过哪些调用边、各自出现频率。

每条调用边记两样东西：
* ``count``：在该入口的多少个请求样本里出现过（同一棵树里同服务对多条
  片段只算一次——去重已在归集阶段完成）；
* ``count / sample_count``：出现率。出现率达到常规比例门槛（可配置）的边
  才算这个入口的“常规路径”；只在极个别请求里冒过一两次的边不算常规结构。

本模块只做计数与常规判定，不做“本次 vs 基线”的对照——那是 comparison 的职责。
"""
from __future__ import annotations

from collections import defaultdict

from .entries import Edge

# 出现率达到该比例才算常规边
DEFAULT_REGULAR_RATIO = 0.8


class StructureBaseline:
    def __init__(self, regular_ratio: float = DEFAULT_REGULAR_RATIO) -> None:
        self._regular_ratio = regular_ratio
        self._counts: dict[Edge, int] = defaultdict(int)
        self.sample_count = 0

    def observe(self, edges: frozenset[Edge]) -> None:
        self.sample_count += 1
        for edge in edges:
            self._counts[edge] += 1

    def frequency(self, edge: Edge) -> int:
        return self._counts.get(edge, 0)

    def ratio(self, edge: Edge) -> float:
        if self.sample_count == 0:
            return 0.0
        return self._counts.get(edge, 0) / self.sample_count

    def is_regular(self, edge: Edge) -> bool:
        if self.sample_count == 0:
            return False
        return self.ratio(edge) >= self._regular_ratio

    def regular_edges(self) -> set[Edge]:
        return {edge for edge in self._counts if self.is_regular(edge)}

    def snapshot(self) -> dict:
        """给对照判定 / 前端展示用的只读快照。

        每条边带 count、ratio、regular，按 (调用方, 被调方) 排序保证输出稳定。
        对照判定拿的就是这份快照，快照之后的新样本不会影响它。
        """
        total = self.sample_count
        edges = [
            {
                "source": caller,
                "target": callee,
                "count": count,
                "ratio": round(count / total, 4) if total else 0.0,
                "regular": (count / total >= self._regular_ratio) if total else False,
            }
            for (caller, callee), count in sorted(self._counts.items())
        ]
        return {
            "sample_count": total,
            "regular_ratio": self._regular_ratio,
            "edges": edges,
        }
