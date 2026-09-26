"""入口对照相关 HTTP API 测试：入口列表、对照详情、未成型、404。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def make_span(span_id, parent=None, trace="t1", service="gateway",
              start=100.0, end=200.0, status=200, operation=None):
    return {
        "trace_id": trace,
        "span_id": span_id,
        "parent_span_id": parent,
        "service": service,
        "operation": operation,
        "start_time": start,
        "end_time": end,
        "status_code": status,
    }


def order_trace(client: TestClient, trace: str, base: float, duration: float = 100.0,
                operation="createOrder", extra=False, drop_inv=False):
    spans = [
        make_span("root", trace=trace, service="gateway",
                  start=base, end=base + duration, operation=operation),
        make_span("order", parent="root", trace=trace, service="order",
                  start=base + 5, end=base + duration - 5),
        make_span("mysql", parent="order", trace=trace, service="mysql",
                  start=base + 10, end=base + 40),
    ]
    if not drop_inv:
        spans.append(make_span("inventory", parent="order", trace=trace,
                               service="inventory", start=base + 12, end=base + 30))
    if extra:
        spans.append(make_span("riskcheck", parent="order", trace=trace,
                               service="riskcheck", start=base + 14, end=base + 28))
    client.post("/api/spans", json={"spans": spans})


@pytest.fixture
def client():
    app = create_app(
        Settings(
            database_path=":memory:",
            max_pending_wait_seconds=30,
            baseline_min_samples=5,
        )
    )
    with TestClient(app) as c:
        yield c


class TestEntriesAPI:
    def test_empty_entries(self, client: TestClient) -> None:
        assert client.get("/api/entries").json() == {"entries": []}

    def test_entries_list_with_baseline(self, client: TestClient) -> None:
        for i in range(6):
            order_trace(client, f"n{i}", base=10_000 + i * 1000)
        body = client.get("/api/entries").json()
        assert len(body["entries"]) == 1
        row = body["entries"][0]
        assert row["root_service"] == "gateway"
        assert row["operation"] == "createOrder"
        assert row["sample_count"] == 6
        assert row["baseline_formed"] is True
        assert row["quantiles"]["p50"] == 100.0

    def test_recent_anomalies_listed(self, client: TestClient) -> None:
        for i in range(6):
            order_trace(client, f"n{i}", base=10_000 + i * 1000)
        order_trace(client, "slow", base=90_000, duration=400)
        order_trace(client, "drift", base=91_000, extra=True)

        row = client.get("/api/entries").json()["entries"][0]
        anomaly_ids = {a["trace_id"] for a in row["recent_anomalies"]}
        assert {"slow", "drift"} <= anomaly_ids

    def test_unformed_entry_reported(self, client: TestClient) -> None:
        order_trace(client, "only", base=10_000)
        row = client.get("/api/entries").json()["entries"][0]
        assert row["baseline_formed"] is False
        assert row["min_samples"] == 5


class TestComparisonAPI:
    def test_comparison_detail(self, client: TestClient) -> None:
        for i in range(6):
            order_trace(client, f"n{i}", base=10_000 + i * 1000)
        order_trace(client, "slow", base=90_000, duration=400)
        result = client.get("/api/comparisons/slow").json()
        assert result["entry"]["root_service"] == "gateway"
        assert result["baseline_version"] == 6
        assert result["latency"]["degraded"] is True
        # 详情里带基线结构明细
        assert result["baseline"]["structure"] is not None
        regular = {(e["caller"], e["callee"])
                   for e in result["baseline"]["regular_edges"]}
        assert ("order", "mysql") in regular

    def test_comparison_unknown_trace_404(self, client: TestClient) -> None:
        resp = client.get("/api/comparisons/nope")
        assert resp.status_code == 404

    def test_operation_field_accepted_and_validated(self, client: TestClient) -> None:
        resp = client.post(
            "/api/spans",
            json={"spans": [make_span("r1", operation="do-thing")]},
        )
        assert resp.json()["accepted"] == 1
        tree = client.get("/api/traces/t1").json()
        assert tree["roots"][0]["operation"] == "do-thing"

        # 空白 operation 属于结构化拒绝
        bad = client.post(
            "/api/spans",
            json={"spans": [make_span("r2", trace="t2", operation="   ")]},
        ).json()
        assert bad["rejected"] == 1
        assert "operation" in bad["errors"][0]["fields"]
