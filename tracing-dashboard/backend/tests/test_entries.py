"""入口归类测试：根服务 + operation 归类、无根/全占位请求不入样、边提取。"""
from __future__ import annotations

from app.entries import DEFAULT_OPERATION, EntryKey, classify, extract_entry_key, tree_edges


def span_node(span_id, service, children=None, operation=None, parent=None,
              start=0.0, end=100.0):
    return {
        "type": "span",
        "span_id": span_id,
        "parent_span_id": parent,
        "service": service,
        "operation": operation,
        "start_time": start,
        "end_time": end,
        "duration": end - start,
        "status_code": 200,
        "is_error": False,
        "depth": 0,
        "children": children or [],
    }


def placeholder_node(missing, children):
    return {
        "type": "placeholder",
        "span_id": f"__missing_parent__:{missing}",
        "missing_parent_span_id": missing,
        "committed": True,
        "service": "unknown",
        "start_time": 0.0,
        "end_time": 10.0,
        "duration": 10.0,
        "status_code": None,
        "is_error": False,
        "depth": 0,
        "children": children,
    }


def tree(trace_id, roots, duration=100.0):
    return {
        "trace_id": trace_id,
        "roots": roots,
        "duration": duration,
        "trace_start": 0.0,
        "trace_end": duration,
    }


class TestEntryClassification:
    def test_root_service_and_operation_define_entry(self) -> None:
        root = span_node("r", "gateway", operation="createOrder")
        features = classify(tree("t1", [root]))
        assert features is not None
        assert features.entry == EntryKey("gateway", "createOrder")
        assert features.trace_id == "t1"
        assert features.root_span_id == "r"

    def test_missing_operation_uses_default(self) -> None:
        root = span_node("r", "gateway", operation=None)
        assert extract_entry_key(tree("t", [root])) == EntryKey(
            "gateway", DEFAULT_OPERATION
        )

    def test_same_root_service_different_operations_are_different_entries(self) -> None:
        a = classify(tree("a", [span_node("r", "gateway", operation="create")]))
        b = classify(tree("b", [span_node("r", "gateway", operation="cancel")]))
        assert a.entry != b.entry

    def test_operation_on_non_root_is_ignored(self) -> None:
        """归类只看根片段上的 operation，子片段带什么不影响入口。"""
        child = span_node("c", "auth", operation="should-not-matter", parent="r")
        root = span_node("r", "gateway", children=[child], operation="entry-op")
        features = classify(tree("t", [root]))
        assert features.entry.operation == "entry-op"

    def test_multiple_roots_uses_a_real_root(self) -> None:
        root = span_node("r", "gateway", operation="op")
        orphan = placeholder_node("ghost", [span_node("o", "notify", parent="ghost")])
        features = classify(tree("t", [root, orphan]))
        # 有真实根片段就正常归类；占位节点下的片段不影响入口
        assert features.entry == EntryKey("gateway", "op")


class TestRootlessExclusion:
    def test_only_placeholder_roots_not_classified(self) -> None:
        """整棵树挂在「父片段缺失」占位节点下：不能归入任何入口。"""
        orphan = placeholder_node("ghost", [span_node("o", "notify")])
        assert classify(tree("t", [orphan])) is None
        assert extract_entry_key(tree("t", [orphan])) is None

    def test_no_roots_at_all_not_classified(self) -> None:
        assert classify(tree("t", [])) is None


class TestEdgeExtraction:
    def test_edges_follow_real_parent_child_services(self) -> None:
        db = span_node("db", "mysql", parent="order")
        order = span_node("order", "order", children=[db], parent="gw")
        gw = span_node("gw", "gateway", children=[order])
        features = classify(tree("t", [gw]))
        assert ("gateway", "order") in features.edges
        assert ("order", "mysql") in features.edges
        assert len(features.edges) == 2

    def test_duplicate_service_pair_deduped_within_trace(self) -> None:
        """同一服务对在一次请求里多次出现，边集合内只保留一条。"""
        child1 = span_node("c1", "auth", parent="r")
        child2 = span_node("c2", "auth", parent="r")
        root = span_node("r", "gateway", children=[child1, child2])
        assert tree_edges(tree("t", [root])) == frozenset({("gateway", "auth")})

    def test_placeholder_children_produce_no_edge(self) -> None:
        orphan = placeholder_node(
            "ghost", [span_node("o", "notify", parent="ghost")]
        )
        assert tree_edges(tree("t", [orphan])) == frozenset()
