"""结构基线测试：边频率、常规边门槛、增量维护。"""
from __future__ import annotations

from app.structure import StructureBaseline

AB = ("a", "b")
AC = ("a", "c")
AD = ("a", "d")


class TestStructureBaseline:
    def test_empty_has_no_regular_edges(self) -> None:
        baseline = StructureBaseline(frequency_threshold=0.8)
        assert baseline.sample_count == 0
        assert baseline.regular_edges() == frozenset()

    def test_frequency_threshold_marks_regular(self) -> None:
        """出现比例达到/超过门槛才算常规；只冒过一两次的边不算。"""
        baseline = StructureBaseline(frequency_threshold=0.8)
        # 10 次请求：AB 每次都在；AC 出现 8 次（恰好 0.8）；AD 只出现 1 次
        for i in range(10):
            edges = {AB}
            if i < 8:
                edges.add(AC)
            if i == 0:
                edges.add(AD)
            baseline.add(frozenset(edges))
        assert baseline.sample_count == 10
        regular = baseline.regular_edges()
        assert AB in regular
        assert AC in regular  # 恰好 80%，达到门槛
        assert AD not in regular  # 10% 的偶发边不算常规
        assert baseline.edge_counts_snapshot()[AD] == 1

    def test_duplicate_edge_in_one_trace_counts_once(self) -> None:
        """同一请求内同一条边出现多次（重试/环）只计一次。"""
        baseline = StructureBaseline(frequency_threshold=0.8)
        baseline.add(frozenset({AB}))
        baseline.add(frozenset({AB}))
        assert baseline.edge_counts_snapshot()[AB] == 2
        assert baseline.sample_count == 2

    def test_below_threshold_not_regular(self) -> None:
        baseline = StructureBaseline(frequency_threshold=0.8)
        for i in range(5):
            edges = {AB}
            if i < 3:
                edges.add(AC)  # 3/5 = 0.6
            baseline.add(frozenset(edges))
        assert AB in baseline.regular_edges()
        assert AC not in baseline.regular_edges()

    def test_snapshot_is_frozen_view(self) -> None:
        baseline = StructureBaseline(frequency_threshold=0.5)
        baseline.add(frozenset({AB}))
        snapshot = baseline.snapshot()
        assert snapshot.sample_count == 1
        assert snapshot.regular_edges == frozenset({AB})
        # 快照不可变：继续并入不影响已取出的快照
        baseline.add(frozenset({AC}))
        assert snapshot.sample_count == 1
        assert snapshot.regular_edges == frozenset({AB})

    def test_snapshot_payload(self) -> None:
        baseline = StructureBaseline(frequency_threshold=0.8)
        baseline.add(frozenset({AB, AC}))
        baseline.add(frozenset({AB}))
        payload = baseline.snapshot().to_dict()
        assert payload["sample_count"] == 2
        edges = {(e["caller"], e["callee"]): e for e in payload["edges"]}
        assert edges[AB]["count"] == 2
        assert edges[AB]["frequency"] == 1.0
        assert edges[AB]["regular"] is True
        assert edges[AC]["frequency"] == 0.5
        assert edges[AC]["regular"] is False
