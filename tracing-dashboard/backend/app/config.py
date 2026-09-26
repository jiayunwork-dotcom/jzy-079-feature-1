"""运行期配置，全部可通过环境变量覆盖。"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return int(raw)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return float(raw)


@dataclass(frozen=True)
class Settings:
    # SQLite 文件路径；":memory:" 主要用于测试
    database_path: str = os.environ.get("TRACING_DB", "/data/tracing.db")
    # 子片段等待父片段的最长时间（秒），超时归入「父片段缺失」占位节点
    max_pending_wait_seconds: float = _env_int("TRACING_MAX_PENDING_WAIT_SECONDS", 30)
    # 依赖图重算防抖：一次 SSE 连接里最快多久推送一次 graph_update（秒）
    graph_push_debounce_seconds: float = float(
        os.environ.get("TRACING_GRAPH_PUSH_DEBOUNCE_SECONDS", "1")
    )
    # 入口基线最低样本数：样本不足时对照结果一律标为「基线尚未成型」
    baseline_min_samples: int = _env_int("TRACING_BASELINE_MIN_SAMPLES", 20)
    # 延迟劣化倍数：端到端耗时超过基线 p95 * 该倍数判为延迟劣化
    latency_slow_factor: float = _env_float("TRACING_LATENCY_SLOW_FACTOR", 1.5)
    # 结构常规边频率门槛：边在该入口历史请求中出现比例达到此值才算常规路径
    structure_edge_frequency: float = _env_float(
        "TRACING_STRUCTURE_EDGE_FREQUENCY", 0.8
    )

    @property
    def max_pending_wait_ms(self) -> float:
        return self.max_pending_wait_seconds * 1000.0


settings = Settings()
