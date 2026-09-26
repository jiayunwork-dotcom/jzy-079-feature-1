"""延迟基线：端到端耗时分布的增量分位数维护。

样本不断上报时基线必须跟着更新，但不能每次查询都重算全部历史。
分位数用 P² 算法（Jain & Chlamtac, "A Simple Algorithm for Fitting
Quantile Functions"）增量估计：

* 每个分位只保留 5 个标志点，单样本更新 O(1)，存储 O(1)，与样本量无关；
* 样本数低于 5 时退化为保留原始观测做精确分位（小样本下 P² 尚未启动）。

P² 给出的是估计分位，样本越多越稳；正式判定受入口最低样本数门槛保护，
门槛之上估计已足够区分「正常」与「远超平时」。
"""
from __future__ import annotations

from typing import Iterable, Optional

# 基线对外固定提供的分档
QUANTILES = {"p50": 0.50, "p95": 0.95, "p99": 0.99}


def exact_quantile(sorted_values: list[float], q: float) -> float:
    """已排序列表上的线性插值分位（与 numpy linear 约定一致）。"""
    n = len(sorted_values)
    if n == 1:
        return sorted_values[0]
    pos = q * (n - 1)
    lo = int(pos)
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return sorted_values[lo] * (1.0 - frac) + sorted_values[hi] * frac


class P2Quantile:
    """单个分位的 P² 增量估计器。"""

    def __init__(self, p: float) -> None:
        self.p = p
        # 前 5 个观测先缓存，攒齐后初始化标志点
        self._buffer: list[float] = []
        self._initialized = False
        # 标志点高度 q'[i]、实际位置 n'[i]、期望位置 dn[i]
        self._heights: list[float] = []
        self._actual: list[int] = []
        self._desired_inc: list[float] = []

    # ------------------------------------------------------------- 更新

    def add(self, value: float) -> None:
        if not self._initialized:
            self._buffer.append(value)
            if len(self._buffer) < 5:
                return
            self._initialize(self._buffer)
            self._buffer = []
            return
        self._add_after_init(value)

    def add_all(self, values: Iterable[float]) -> None:
        for value in values:
            self.add(value)

    def _initialize(self, five: list[float]) -> None:
        heights = sorted(five)
        self._heights = heights
        self._actual = [1, 2, 3, 4, 5]
        p = self.p
        self._desired_inc = [0.0, p / 2.0, p, (1.0 + p) / 2.0, 1.0]
        self._initialized = True

    def _add_after_init(self, value: float) -> None:
        heights = self._heights
        # 1) 找落点单元 k 并让其右侧标志点的实际位置 +1
        if value < heights[0]:
            k = 0
            heights[0] = value
        elif value >= heights[4]:
            k = 3
            heights[4] = value
        else:
            k = 0
            for i in range(4):
                if heights[i] <= value < heights[i + 1]:
                    k = i
                    break
        for i in range(k + 1, 5):
            self._actual[i] += 1

        # 2) 期望位置推进
        desired = [
            1.0 + inc * self.count
            for inc in self._desired_inc
        ]

        # 3) 调整内部标志点 1..3
        for i in range(1, 4):
            d_int = desired[i] - self._actual[i]
            # 只能朝「相邻标志点间距 > 1」的方向挪，保证标志点位置严格递增、
            # 抛物线/线性预测的分母恒不为 0（论文中的方向性守卫）
            forward_gap = self._actual[i + 1] - self._actual[i]
            backward_gap = self._actual[i] - self._actual[i - 1]
            if (d_int >= 1.0 and forward_gap > 1) or (
                d_int <= -1.0 and backward_gap > 1
            ):
                d = 1 if d_int >= 0 else -1
                qs = self._parabolic(i, d)
                # 抛物线预测越界时退化为线性
                if heights[i - 1] < qs < heights[i + 1]:
                    heights[i] = qs
                else:
                    heights[i] = self._linear(i, d)
                self._actual[i] += d

    def _parabolic(self, i: int, d: int) -> float:
        heights = self._heights
        actual = self._actual
        gap = actual[i + 1] - actual[i - 1]
        return heights[i] + d / gap * (
            (actual[i] - actual[i - 1] + d)
            * (heights[i + 1] - heights[i])
            / (actual[i + 1] - actual[i])
            + (actual[i + 1] - actual[i] - d)
            * (heights[i] - heights[i - 1])
            / (actual[i] - actual[i - 1])
        )

    def _linear(self, i: int, d: int) -> float:
        heights = self._heights
        actual = self._actual
        target = i + d
        return heights[i] + d * (
            (heights[target] - heights[i]) / (actual[target] - actual[i])
        )

    # ------------------------------------------------------------- 查询

    @property
    def count(self) -> int:
        if self._initialized:
            return self._actual[4]
        return len(self._buffer)

    def value(self) -> Optional[float]:
        """当前分位估计；尚无观测时返回 None。"""
        if not self._initialized:
            if not self._buffer:
                return None
            return exact_quantile(sorted(self._buffer), self.p)
        return self._heights[2]


class LatencyBaseline:
    """一个入口的端到端耗时分布基线（p50 / p95 / p99）。"""

    def __init__(self) -> None:
        self._quantiles = {name: P2Quantile(q) for name, q in QUANTILES.items()}
        # 前 4 个观测额外留一份，样本 < 5 时给出精确分位
        self._head: list[float] = []
        self._count = 0

    def add(self, duration: float) -> None:
        """并入一次请求的端到端耗时。O(分位数个数)。"""
        for estimator in self._quantiles.values():
            estimator.add(duration)
        if len(self._head) < 5:
            self._head.append(duration)
        self._count += 1

    @property
    def count(self) -> int:
        return self._count

    def quantile(self, name: str) -> Optional[float]:
        if self._count == 0:
            return None
        if self._count < 5:
            return exact_quantile(sorted(self._head[: self._count]), QUANTILES[name])
        return self._quantiles[name].value()

    def quantiles(self) -> dict[str, Optional[float]]:
        return {name: self.quantile(name) for name in QUANTILES}
