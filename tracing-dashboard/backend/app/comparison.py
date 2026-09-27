"""对照判定：拿一棵新到的调用树和“它被并入样本之前”的基线比。

本模块是纯函数：输入（基线快照 + 本次样本 + 判定参数），输出对照结果，
不持有任何状态、不更新任何基线。先判定后并样本的次序由调用方
（baselines 注册表）保证——注册表先取快照调用这里，再把样本喂给基线。

两项对照：
* 延迟：端到端耗时落在基线的哪一档（p50/p95/p99），是否超过
  p95 × 可配置倍数而判为延迟劣化；
* 结构：本次边集合 vs 基线常规边集合，分出“多出来的调用”（本次有、
  常规路径没有）和“消失的调用”（常规路径有、本次没有）。

样本数不足最低门槛时只给 ``immature``（基线尚未成型），不出任何正式判定。
"""
from __future__ import annotations

from .entries import EntryKey, TraceSample

# 对照状态
STATUS_IMMATURE = "immature"  # 基线尚未成型（样本不足）
STATUS_OK = "ok"              # 已成型，本次未发现偏差
STATUS_DEGRADED = "degraded"  # 延迟劣化（可与结构漂移并存）
STATUS_DRIFTED = "drifted"    # 结构漂移（新增边或消失边，可与劣化并存）

# 延迟档位（“慢在哪一档”）
BAND_BELOW_P50 = "below_p50"
BAND_P50_P95 = "p50_to_p95"
BAND_P95_P99 = "p95_to_p99"
BAND_ABOVE_P99 = "above_p99"

# 延迟判定结论
LATENCY_NORMAL = "normal"
LATENCY_DEGRADED = "degraded"

# 结构偏差类型
DIFF_ADDED = "added"
DIFF_MISSING = "missing"


def classify_latency_band(duration_ms: float, quantiles: dict) -> str:
    p99 = quantiles.get("p99")
    p95 = quantiles.get("p95")
    p50 = quantiles.get("p50")
    if p99 is not None and duration_ms > p99:
        return BAND_ABOVE_P99
    if p95 is not None and duration_ms > p95:
        return BAND_P95_P99
    if p50 is not None and duration_ms > p50:
        return BAND_P50_P95
    return BAND_BELOW_P50


def compare_structure(
    current_edges: frozenset[tuple[str, str]],
    regular_edges: set[tuple[str, str]],
) -> dict:
    """本次边集合与常规边集合的对称差，分成新增 / 消失两组。"""
    added = sorted(current_edges - regular_edges)
    missing = sorted(regular_edges - current_edges)
    return {
        "added": [
            {
                "source": caller,
                "target": callee,
                "difference": DIFF_ADDED,
            }
            for caller, callee in added
        ],
        "missing": [
            {
                "source": caller,
                "target": callee,
                "difference": DIFF_MISSING,
            }
            for caller, callee in missing
        ],
    }


def evaluate(
    sample: TraceSample,
    *,
    baseline_version: int,
    sample_count: int,
    min_samples: int,
    quantiles: dict,
    regular_edges: set[tuple[str, str]],
    slow_multiplier: float,
    regular_ratio: float,
) -> dict:
    """对一次请求产出对照结果（基线必须是该请求并入之前的版本）。

    ``baseline_version`` = 并入前该入口已有的样本数，一并写进结果，
    让“比的是哪一版基线”可追溯。
    """
    entry: EntryKey = sample.entry
    base: dict = {
        "trace_id": sample.trace_id,
        "entry": entry.as_dict(),
        "baseline_version": baseline_version,
        "sample_count_before": sample_count,
        "duration_ms": round(sample.duration_ms, 3),
        "latency": None,
        "structure": None,
        "status": STATUS_IMMATURE,
        "is_anomaly": False,
        "anomaly_types": [],
    }

    if sample_count < min_samples:
        # 样本太少：分位数和常规边都不稳，明确标“基线尚未成型”，
        # 绝不拿两三个样本硬算 p95 去指认异常
        base["reason"] = (
            f"入口样本 {sample_count} 条，未达到最低 {min_samples} 条，基线尚未成型"
        )
        return base

    # --------------------------------------------------------- 延迟对照
    p95 = quantiles.get("p95")
    threshold = p95 * slow_multiplier if p95 is not None else None
    band = classify_latency_band(sample.duration_ms, quantiles)
    degraded = threshold is not None and sample.duration_ms > threshold
    latency_result = {
        "verdict": LATENCY_DEGRADED if degraded else LATENCY_NORMAL,
        "band": band,
        "threshold_ms": round(threshold, 3) if threshold is not None else None,
        "slow_multiplier": slow_multiplier,
        "quantiles": {
            key: (round(value, 3) if value is not None else None)
            for key, value in quantiles.items()
        },
    }
    base["latency"] = latency_result

    # --------------------------------------------------------- 结构对照
    diff = compare_structure(sample.edges, regular_edges)
    drifted = bool(diff["added"] or diff["missing"])
    base["structure"] = {
        "verdict": "drifted" if drifted else "normal",
        "regular_ratio": regular_ratio,
        "regular_edge_count": len(regular_edges),
        "added_edges": diff["added"],
        "missing_edges": diff["missing"],
    }

    # --------------------------------------------------------- 汇总状态
    anomaly_types: list[str] = []
    if degraded:
        anomaly_types.append("latency")
    if drifted:
        anomaly_types.append("structure")
    base["anomaly_types"] = anomaly_types
    base["is_anomaly"] = bool(anomaly_types)
    if degraded and drifted:
        base["status"] = "degraded_and_drifted"
    elif degraded:
        base["status"] = STATUS_DEGRADED
    elif drifted:
        base["status"] = STATUS_DRIFTED
    else:
        base["status"] = STATUS_OK
    return base
