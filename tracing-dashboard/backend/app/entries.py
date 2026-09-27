"""入口归类与样本归集。

一棵调用树的“入口”由根片段决定：根片段所属服务 + 入口标识 operation
（可理解为这次请求打的是哪个对外操作）。同一入口下的历史请求构成该入口
的样本池，延迟基线与结构基线都建立在样本池之上。

拒绝归类（不进入任何入口的样本池、不污染任何基线）的情形：
* 根本没有根片段——整棵树都挂在“父片段缺失”占位节点下，无法知道入口；
* 根片段不止一个——入口归属有歧义，不能随便挑一个；
* 调用树仍有片段在等待父片段——结构还不完整，等归位后再说。

判定为不归类的 trace 会被登记下来，只判一次；后续晚到片段也不再改口，
保证“基线是由哪些请求构成的”这件事稳定、可回放。
"""
from __future__ import annotations

from dataclasses import dataclass

# operation 缺省或空白时的统一入口标识：不带标识的根片段都归到同一个默认入口
DEFAULT_ENTRY_OPERATION = "default"

# 调用边：调用方服务 -> 被调方服务
Edge = tuple[str, str]

# classify_trace 的非样本结论
CLASSIFY_PENDING = "pending"          # 仍有片段等待父片段，树不完整
CLASSIFY_UNCLASSIFIABLE = "unclassifiable"  # 无清晰根 / 多根，永不归类


def normalize_operation(operation: str | None) -> str:
    if operation is None or not operation.strip():
        return DEFAULT_ENTRY_OPERATION
    return operation.strip()


@dataclass(frozen=True)
class EntryKey:
    """入口身份：根片段服务 + 入口标识。"""

    service: str
    operation: str

    def as_dict(self) -> dict:
        return {"service": self.service, "operation": self.operation}


@dataclass(frozen=True)
class TraceSample:
    """一次可入样的请求：归属入口 + 端到端耗时 + 本次出现的去重调用边。"""

    trace_id: str
    entry: EntryKey
    duration_ms: float
    edges: frozenset[Edge]


def root_duration(root) -> float:
    return max(root.end_time - root.start_time, 0.0)


def classify_trace(trace_id: str, tree, edges: list) -> TraceSample | str:
    """把一棵拼好的调用树归成一个样本；不能入样时返回状态字符串。

    ``edges`` 直接复用拼树器提取的真实父子边（含耗时），占位节点下的
    片段不产生边，与依赖图口径完全一致。这里按“服务对”去重：结构基线
    关心的是这次请求里有没有这条调用路径，同一条边在一棵树里出现多次
    （同服务对的多个片段）只计一次。

    返回值：
    * ``TraceSample`` —— 单一根片段、无挂起片段，可入样；
    * ``CLASSIFY_PENDING`` —— 仍有片段等待父片段（根可能已有，也可能没到）；
    * ``CLASSIFY_UNCLASSIFIABLE`` —— 没有挂起片段却没有唯一根：全树都挂在
      已提交的“父片段缺失”占位节点下（根不会再来），或根片段不止一个
      （入口归属有歧义）。这类请求永不入样。
    """
    roots = tree.root_spans(trace_id)
    if tree.pending_count(trace_id) > 0:
        return CLASSIFY_PENDING
    if len(roots) != 1:
        return CLASSIFY_UNCLASSIFIABLE
    root = roots[0]

    edge_set: frozenset[Edge] = frozenset(
        (caller, callee) for caller, callee, _duration in edges
    )
    return TraceSample(
        trace_id=trace_id,
        entry=EntryKey(service=root.service, operation=normalize_operation(root.operation)),
        # 端到端耗时取根片段自身跨度：根覆盖了这次请求的完整生命周期，
        # 也不会被晚到的孤儿片段的时间区间带偏
        duration_ms=root_duration(root),
        edges=edge_set,
    )
