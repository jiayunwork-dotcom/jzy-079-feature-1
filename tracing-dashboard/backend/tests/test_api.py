"""HTTP API 测试：上报接口的结构化错误、检索、树查询、依赖图。"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture
def client():
    app = create_app(Settings(database_path=":memory:", max_pending_wait_seconds=30))
    with TestClient(app) as c:
        yield c


@pytest.fixture
def baseline_client():
    app = create_app(
        Settings(
            database_path=":memory:",
            max_pending_wait_seconds=0,
            baseline_min_samples=5,
            latency_slow_multiplier=1.5,
            structure_regular_ratio=0.8,
        )
    )
    with TestClient(app) as c:
        yield c


def make_span(span_id, parent=None, trace="t1", service="svc",
              start=100.0, end=200.0, status=200):
    return {
        "trace_id": trace,
        "span_id": span_id,
        "parent_span_id": parent,
        "service": service,
        "start_time": start,
        "end_time": end,
        "status_code": status,
    }


class TestIngestAPI:
    def test_valid_batch(self, client: TestClient) -> None:
        resp = client.post("/api/spans", json={"spans": [make_span("s1")]})
        assert resp.status_code == 200
        body = resp.json()
        assert body["accepted"] == 1
        assert body["rejected"] == 0
        assert body["errors"] == []

    def test_invalid_spans_get_structured_errors(
        self, client: TestClient
    ) -> None:
        resp = client.post(
            "/api/spans",
            json={
                "spans": [
                    make_span("good"),
                    make_span("bad-time", start=300.0, end=100.0),
                    make_span("", service="svc"),  # 空 span_id
                    {**make_span("no-end"), "end_time": None},
                ]
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["accepted"] == 1
        assert body["rejected"] == 3
        codes = {e["code"] for e in body["errors"]}
        assert "TIME_INVERTED" in codes
        assert "MISSING_FIELD" in codes
        for err in body["errors"]:
            assert err["message"]
            assert err["fields"]
        # 合法的那条正常入树
        tree = client.get("/api/traces/t1").json()
        assert tree["span_count"] == 1

    def test_duplicate_reported(self, client: TestClient) -> None:
        client.post("/api/spans", json={"spans": [make_span("s1")]})
        resp = client.post("/api/spans", json={"spans": [make_span("s1")]})
        assert resp.json()["duplicates"] == 1
        tree = client.get("/api/traces/t1").json()
        assert tree["span_count"] == 1


class TestTraceAPI:
    def test_tree_and_search(self, client: TestClient) -> None:
        now = time.time() * 1000
        client.post(
            "/api/spans",
            json={
                "spans": [
                    make_span("root", trace="t1", service="gateway",
                              start=now, end=now + 500),
                    make_span("child", parent="root", trace="t1", service="auth",
                              start=now + 50, end=now + 200, status=500),
                ]
            },
        )
        tree = client.get("/api/traces/t1").json()
        assert tree["span_count"] == 2
        assert tree["critical_path"] == ["root", "child"]
        child = tree["roots"][0]["children"][0]
        assert child["is_error"] is True

        # 按 trace_id 和服务名检索
        by_id = client.get("/api/traces", params={"trace_id": "t1"}).json()
        assert len(by_id["traces"]) == 1
        by_svc = client.get("/api/traces", params={"service": "auth"}).json()
        assert len(by_svc["traces"]) == 1
        missing = client.get("/api/traces", params={"service": "nope"}).json()
        assert missing["traces"] == []

    def test_unknown_trace_404(self, client: TestClient) -> None:
        assert client.get("/api/traces/ghost").status_code == 404


class TestGraphAPI:
    def test_graph_and_window_validation(self, client: TestClient) -> None:
        now = time.time() * 1000
        client.post(
            "/api/spans",
            json={
                "spans": [
                    make_span("a", trace="t1", service="svc-a",
                              start=now, end=now + 100),
                    make_span("b", parent="a", trace="t1", service="svc-b",
                              start=now + 10, end=now + 50),
                    make_span("c", parent="b", trace="t1", service="svc-a",
                              start=now + 15, end=now + 40),
                ]
            },
        )
        graph = client.get("/api/graph", params={"window": "1h"}).json()
        edges = {(e["source"], e["target"]): e for e in graph["edges"]}
        # svc-a -> svc-b -> svc-a 构成环，两条边都要标出来
        assert edges[("svc-a", "svc-b")]["in_cycle"] is True
        assert edges[("svc-b", "svc-a")]["in_cycle"] is True

        assert client.get("/api/graph", params={"window": "bogus"}).status_code == 400


def _post_with_operation(client, trace, duration=100.0, start=1000.0,
                         op="place_order", extra=None, drop=None):
    root = make_span("root", trace=trace, service="gateway",
                     start=start, end=start + duration)
    root["operation"] = op
    spans = [
        root,
        make_span("inv", parent="root", trace=trace, service="inventory",
                  start=start + 5, end=start + 40),
    ]
    if "pay" not in (drop or []):
        spans.append(
            make_span("pay", parent="root", trace=trace, service="payment",
                      start=start + 10, end=start + 80)
        )
    for svc in extra or []:
        sid = f"x-{svc}"
        spans.append(
            make_span(sid, parent="root", trace=trace, service=svc,
                      start=start + 20, end=start + 60)
        )
    return client.post("/api/spans", json={"spans": spans})


class TestEntryBaselineAPI:
    def test_entries_comparison_flow(self, baseline_client: TestClient) -> None:
        c = baseline_client
        for i in range(5):
            _post_with_operation(c, f"n-{i}", duration=100, start=1000 + i * 1000)

        # 入口列表：基线已成型（第 6 个请求对照时样本数=5）
        rows = c.get("/api/entries").json()["entries"]
        assert len(rows) == 1
        row = rows[0]
        assert (row["service"], row["operation"]) == ("gateway", "place_order")
        assert row["sample_count"] == 5
        assert row["baseline_ready"] is True

        # 详情：常规边两条
        detail = c.get(
            "/api/entries/detail", params={"service": "gateway", "operation": "place_order"}
        ).json()
        pairs = {(e["source"], e["target"]) for e in detail["structure"]["edges"]}
        assert {("gateway", "inventory"), ("gateway", "payment")} <= pairs

        # 延迟劣化请求
        _post_with_operation(c, "slow", duration=1000, start=50_000)
        slow = c.get("/api/comparisons/slow").json()
        assert slow["status"] == "degraded"
        assert slow["baseline_version"] == 5
        assert slow["latency"]["verdict"] == "degraded"

        # 结构新增边
        _post_with_operation(c, "extra", duration=100, start=60_000,
                             extra=["riskcheck"])
        added = c.get("/api/comparisons/extra").json()
        assert ("gateway", "riskcheck") in {
            (e["source"], e["target"]) for e in added["structure"]["added_edges"]
        }

        # 入口列表里的最近异常包含这两条
        row = c.get("/api/entries").json()["entries"][0]
        recent_ids = {a["trace_id"] for a in row["recent_anomalies"]}
        assert {"slow", "extra"} <= recent_ids

    def test_immature_and_unknown_entry(self, baseline_client: TestClient) -> None:
        c = baseline_client
        _post_with_operation(c, "lonely", duration=100, op="rare")
        cmp = c.get("/api/comparisons/lonely").json()
        assert cmp["status"] == "immature"
        assert cmp["is_anomaly"] is False
        # 不存在的入口详情 404；不存在的对照 404
        assert c.get("/api/entries/detail",
                     params={"service": "nope", "operation": "nope"}).status_code == 404
        assert c.get("/api/comparisons/ghost").status_code == 404

    def test_rootless_trace_has_no_comparison(
        self, baseline_client: TestClient
    ) -> None:
        c = baseline_client
        c.post(
            "/api/spans",
            json={
                "spans": [
                    make_span("o1", parent="ghost", trace="orphan", service="notify"),
                ]
            },
        )
        # 等不到父片段时仍挂起，尚不定版；推进超时归位后判为无清晰根片段
        assert c.get("/api/comparisons/orphan").status_code == 404
        baseline_client.app.state.tracing_service.tick()
        assert c.get("/api/comparisons/orphan").status_code == 404
        assert c.get("/api/entries").json()["entries"] == []
