"""延迟基线：入口端到端耗时分位数的增量维护。

用 P² 算法（Jain/Chlamtac, 1985）的多分位数变体同时跟踪 p50/p95/p99：
固定 2q+3 个标记点（q=3 时为 9 个），每次观测只做常数次标记位置/高度
调整，内存与更新代价都不随样本量增长，不需要每查一次就重算全部历史。

标记点凑齐前（样本数 < 标记数）直接保留排序样本给出精确分位数；
凑齐后切换为 P² 近似。样本"够不够格做判定"由上层注册表按最低样本条数
把关，本模块只负责数值本身。
"""
from __future__ import annotations

import bisect
import math
from typing import Sequence

# 同时维护的分位点
QUANTILES: tuple[float, ...] = (0.50, 0.95, 0.99)


class P2Quantiles:
    """多分位数 P² 估计器。

    标记下标 0..M-1：两头是最小/最大值，中间每个目标分位数对应一个
    分位标记，相邻分位标记之间各插一个中点标记。标记 i 的期望位置
    n'_i = 1 + (N-1) * prob[i]，每来一个样本按 prob[i] 增长。
    """

    def __init__(self, quantiles: Sequence[float] = QUANTILES) -> None:
        qs = list(quantiles)
        self._q = qs
        m = 2 * len(qs) + 3
        self._m = m
        # 各标记对应的概率位置
        prob = [0.0] * m
        for j in range(1, len(qs) + 1):
            prev_p = qs[j - 2] if j >= 2 else 0.0
            prob[2 * j - 1] = (prev_p + qs[j - 1]) / 2.0
            prob[2 * j] = qs[j - 1]
        prob[m - 2] = (qs[-1] + 1.0) / 2.0
        prob[m - 1] = 1.0
        self._prob = prob

        # 标记高度 / 实际位置 / 期望位置；凑齐 m 个样本后初始化
        self._heights: list[float] = []
        self._positions: list[int] = []
        self._desired: list[float] = []
        self._initial: list[float] = []
        self.count = 0

    # -------------------------------------------------------------- 观测

    def observe(self, value: float) -> None:
        self.count += 1
        if not self._heights:
            # 标记尚未初始化：先攒满 m 个样本
            self._initial.append(value)
            if len(self._initial) == self._m:
                self._heights = sorted(self._initial)
                self._positions = list(range(1, self._m + 1))
                self._desired = [float(i) for i in range(1, self._m + 1)]
                self._initial = []
            return
        self._observe_marker(value)

    def _observe_marker(self, value: float) -> None:
        heights = self._heights
        m = self._m

        # 1) 找到 value 落入的标记区间 k，越界直接拉伸端点
        if value < heights[0]:
            heights[0] = value
            k = 0
        elif value >= heights[-1]:
            heights[-1] = value
            k = m - 2
        else:
            k = bisect.bisect_right(heights, value) - 1

        # 2) 期望位置整体按概率推进；区间右侧标记的实际位置 +1
        for i in range(m):
            self._desired[i] += self._prob[i]
        for i in range(k + 1, m):
            self._positions[i] += 1

        # 3) 逐个调整内部标记的高度
        for i in range(1, m - 1):
            n = self._positions
            offset = self._desired[i] - n[i]
            if (offset >= 1 and n[i + 1] - n[i] > 1) or (
                offset <= -1 and n[i - 1] - n[i] < -1
            ):
                d = 1 if offset >= 0 else -1
                candidate = self._parabolic(i, d)
                if candidate is None or not (
                    heights[i - 1] < candidate < heights[i + 1]
                ):
                    # 抛物线预测越界或退化：退化为线性预测
                    candidate = heights[i] + d * (
                        heights[i + d] - heights[i]
                    ) / (n[i + d] - n[i])
                heights[i] = candidate
                n[i] += d

    def _parabolic(self, i: int, d: int) -> float | None:
        heights = self._heights
        n = self._positions
        span = n[i + 1] - n[i - 1]
        if span == 0:
            return None
        return heights[i] + d / span * (
            (n[i] - n[i - 1] + d)
            * (heights[i + 1] - heights[i])
            / (n[i + 1] - n[i])
            + (n[i + 1] - n[i] - d)
            * (heights[i] - heights[i - 1])
            / (n[i] - n[i - 1])
        )

    # -------------------------------------------------------------- 读取

    def value_at(self, quantile: float) -> float | None:
        """取某个已登记分位数的当前估计值；无样本返回 None。"""
        if self.count == 0:
            return None
        if not self._heights:
            return _exact_quantile(sorted(self._initial), quantile)
        # 分位标记下标 = 2*j（j 从 1 起）
        j = self._q.index(quantile)
        return self._heights[2 * (j + 1)]


def _exact_quantile(sorted_values: list[float], p: float) -> float:
    """标记凑齐前的精确分位数（nearest-rank，向上取整排名）。"""
    n = len(sorted_values)
    if n == 1:
        return sorted_values[0]
    rank = max(1, math.ceil(p * n)) - 1
    return sorted_values[min(rank, n - 1)]


class LatencyBaseline:
    """一个入口的端到端耗时分布：只暴露 p50/p95/p99 与样本数。"""

    def __init__(self) -> None:
        self._p2 = P2Quantiles(QUANTILES)

    def observe(self, duration_ms: float) -> None:
        self._p2.observe(max(float(duration_ms), 0.0))

    @property
    def sample_count(self) -> int:
        return self._p2.count

    def quantiles(self) -> dict[str, float | None]:
        return {
            "p50": self._p2.value_at(0.50),
            "p95": self._p2.value_at(0.95),
            "p99": self._p2.value_at(0.99),
        }
