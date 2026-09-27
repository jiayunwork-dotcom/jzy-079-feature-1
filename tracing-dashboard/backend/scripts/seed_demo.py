"""生成演示数据：入口基线样本 + 延迟劣化 / 结构漂移异常、乱序上报、孤儿片段。

用法：python scripts/seed_demo.py [API_BASE_URL]
默认 POST 到 http://localhost:8000（容器内），宿主机用 http://localhost:8080。
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
now = time.time() * 1000.0
minute = 60_000.0


def post(batch: list[dict]) -> None:
    req = urllib.request.Request(
        f"{BASE}/api/spans",
        data=json.dumps({"spans": batch}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        print(resp.status, resp.read().decode()[:160])


def order_trace(
    trace_id: str,
    base_ms: float,
    duration_ms: float = 800.0,
    *,
    extra_edge: bool = False,
    drop_order: bool = False,
    jitter: float = 0.0,
) -> None:
    """gateway 入口 place_order 操作的常规结构：gateway -> inventory / payment。"""
    spans = [
        {"trace_id": trace_id, "span_id": "gw", "parent_span_id": None,
         "service": "gateway", "operation": "place_order",
         "start_time": base_ms, "end_time": base_ms + duration_ms,
         "status_code": 200},
        {"trace_id": trace_id, "span_id": "inv", "parent_span_id": "gw",
         "service": "inventory",
         "start_time": base_ms + 50, "end_time": base_ms + 300 + jitter,
         "status_code": 200},
    ]
    if not drop_order:
        spans.append(
            {"trace_id": trace_id, "span_id": "pay", "parent_span_id": "gw",
             "service": "payment",
             "start_time": base_ms + 200, "end_time": base_ms + 700 + jitter,
             "status_code": 200}
        )
    if extra_edge:
        # 意外的新增调用：gateway -> riskcheck，基线常规路径里没有
        spans.append(
            {"trace_id": trace_id, "span_id": "risk", "parent_span_id": "gw",
             "service": "riskcheck",
             "start_time": base_ms + 250, "end_time": base_ms + 650,
             "status_code": 200}
        )
    post(spans)


# 1) 常规样本：同一入口 gateway/place_order 反复出现，耗时在 800ms 附近小幅波动
for i in range(18):
    order_trace(
        f"demo-order-{i:03d}",
        now - (40 - i) * minute,
        duration_ms=780 + (i % 5) * 25,
        jitter=(i % 3) * 10,
    )

# 2) 延迟劣化：结构不变，端到端 5s（远超 p95 × 1.5）
order_trace("demo-order-slow", now - 20_000, duration_ms=5000)

# 3) 结构漂移-新增边：多出一条 gateway -> riskcheck
order_trace("demo-order-extra", now - 12_000, duration_ms=820, extra_edge=True)

# 4) 结构漂移-消失边：常规的 gateway -> payment 这次没了
order_trace("demo-order-missing", now - 5_000, duration_ms=600, drop_order=True)

# 5) 另一个入口 gateway/refund 的少量样本（基线尚未成型演示）
for i in range(3):
    post([
        {"trace_id": f"demo-refund-{i:03d}", "span_id": "gw",
         "parent_span_id": None, "service": "gateway", "operation": "refund",
         "start_time": now - (4 - i) * minute, "end_time": now - (4 - i) * minute + 500,
         "status_code": 200},
        {"trace_id": f"demo-refund-{i:03d}", "span_id": "bill",
         "parent_span_id": "gw", "service": "billing",
         "start_time": now - (4 - i) * minute + 30,
         "end_time": now - (4 - i) * minute + 420,
         "status_code": 200},
    ])

# 6) 乱序上报演示（独立入口，避免混入 place_order 样本）
t6 = "demo-outoforder"
post([
    {"trace_id": t6, "span_id": "check", "parent_span_id": "order",
     "service": "inventory", "start_time": now - 90_000, "end_time": now - 89_700,
     "status_code": 200},
    {"trace_id": t6, "span_id": "gw", "parent_span_id": None,
     "service": "gateway", "operation": "out_of_order_demo",
     "start_time": now - 90_100, "end_time": now - 89_200, "status_code": 200},
    {"trace_id": t6, "span_id": "order", "parent_span_id": "gw",
     "service": "order", "start_time": now - 90_050, "end_time": now - 89_250,
     "status_code": 200},
])

# 7) 带环依赖（独立入口）
t7 = "demo-cycle"
post([
    {"trace_id": t7, "span_id": "r", "parent_span_id": None,
     "service": "gateway", "operation": "cycle_demo",
     "start_time": now - 70_000, "end_time": now - 69_000, "status_code": 200},
    {"trace_id": t7, "span_id": "u1", "parent_span_id": "r",
     "service": "user", "start_time": now - 69_900, "end_time": now - 69_200,
     "status_code": 200},
    {"trace_id": t7, "span_id": "p1", "parent_span_id": "u1",
     "service": "profile", "start_time": now - 69_800, "end_time": now - 69_400,
     "status_code": 200},
    {"trace_id": t7, "span_id": "u2", "parent_span_id": "p1",
     "service": "user", "start_time": now - 69_700, "end_time": now - 69_500,
     "status_code": 200},
])

# 8) 父片段永远不到的孤儿片段：无清晰根片段，不参与入口归类
post([
    {"trace_id": "demo-orphan", "span_id": "o1", "parent_span_id": "never-arrives",
     "service": "notify", "start_time": now - 10_000, "end_time": now - 9_500,
     "status_code": 200},
])

print(f"\n演示数据已上报到 {BASE}，打开前端「入口基线对照」查看异常请求。")
