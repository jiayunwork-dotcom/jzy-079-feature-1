"""入口归类与样本归集。

一个「入口」= 根片段所属服务 + 这次请求的入口标识（根片段上的 operation，
缺省归入默认操作）。同一入口反复产生的调用树构成该入口的样本池。

只有拥有清晰根片段（parent_span_id 为 null 的真实片段）的请求才参与归类：
* 整棵树都挂在「父片段缺失」占位节点下的请求；
* 根本没有任何根片段的请求；
都返回 None，由上层记入排除名单，不进入任何入口的样本池，也不污染基线。

本模块不依赖 FastAPI / 存储 / 拼接器内部状态，输入是已经拼好的树视图
（build_tree 的返回值），可独立单测。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# 根片段未带 operation 时归入的默认入口标识
DEFAULT_OPERATION = "__default__"

# 一条调用边：(调用方服务, 被调方服务)
Edge = tuple[str, str]


@dataclass(frozen=True)
class EntryKey:
    """入口的归类依据：根服务 + 入口标识。"""

    root_service: str
    operation: str

    def to_dict(self) -> dict:
        return {"root_service": self.root_service, "operation": self.operation}

    @property
    def display_name(self) -> str:
        op = "" if self.operation == DEFAULT_OPERATION else f":{self.operation}"
        return f"{self.root_service}{op}"


@dataclass
class RequestFeatures:
    """一次已归类请求参与基线维护所需的全部特征。"""

    trace_id: str
    entry: EntryKey
    # 整条请求端到端耗时（最早片段开始 -> 最晚片段结束）
    duration: float
    # 本次请求里实际出现的调用边（同一棵树内去重）
    edges: frozenset[Edge]
    start_time: float
    root_span_id: str


def extract_entry_key(tree: dict) -> Optional[EntryKey]:
    """从拼好的树视图里取入口。

    树没有任何真实根片段（只有占位节点，或为空）时返回 None——
    这类请求不参与归类。
    """
    root = _root_span_node(tree)
    if root is None:
        return None
    operation = root.get("operation") or DEFAULT_OPERATION
    return EntryKey(root_service=root["service"], operation=operation)


def _root_span_node(tree: dict) -> Optional[dict]:
    for node in tree.get("roots", []):
        if node["type"] == "span" and node.get("parent_span_id") is None:
            return node
    return None


def tree_edges(tree: dict) -> frozenset[Edge]:
    """提取树视图里实际出现的「谁调用谁」边（服务对，树内去重）。

    占位节点下的片段没有真实父服务，不产生边。
    """
    edges: set[Edge] = set()

    def walk(node: dict) -> None:
        if node["type"] == "span":
            for child in node.get("children", []):
                if child["type"] == "span":
                    edges.add((node["service"], child["service"]))
                walk(child)
        else:
            for child in node.get("children", []):
                walk(child)

    for node in tree.get("roots", []):
        walk(node)
    return frozenset(edges)


def classify(tree: dict) -> Optional[RequestFeatures]:
    """把一棵拼好的树归到入口并抽取特征；没有清晰根片段则返回 None。"""
    entry = extract_entry_key(tree)
    if entry is None:
        return None
    root = _root_span_node(tree)
    assert root is not None  # extract_entry_key 已确认存在
    return RequestFeatures(
        trace_id=tree["trace_id"],
        entry=entry,
        duration=float(tree["duration"]),
        edges=tree_edges(tree),
        start_time=float(tree["trace_start"]),
        root_span_id=root["span_id"],
    )
