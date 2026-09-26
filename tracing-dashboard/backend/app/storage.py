"""SQLite 持久化：只存片段本身。

依赖图是查询时按时间窗口实时聚合的，不做落库——窗口切换（1h/1d）时
直接重新聚合，从根上杜绝新旧窗口的边混在一起。
"""
from __future__ import annotations

import os
import sqlite3
import threading
from typing import Optional

from .models import SpanRecord

_SCHEMA = """
CREATE TABLE IF NOT EXISTS spans (
    trace_id       TEXT NOT NULL,
    span_id        TEXT NOT NULL,
    parent_span_id TEXT,
    service        TEXT NOT NULL,
    operation      TEXT,
    start_time     REAL NOT NULL,
    end_time       REAL NOT NULL,
    status_code    INTEGER NOT NULL,
    received_at    REAL NOT NULL,
    PRIMARY KEY (trace_id, span_id)
);
CREATE INDEX IF NOT EXISTS idx_spans_trace ON spans (trace_id);
CREATE INDEX IF NOT EXISTS idx_spans_start ON spans (start_time);

-- 已归入入口样本池的请求（每个 trace 至多一行）
CREATE TABLE IF NOT EXISTS trace_samples (
    trace_id        TEXT PRIMARY KEY,
    root_service    TEXT NOT NULL,
    operation       TEXT NOT NULL,
    root_span_id    TEXT NOT NULL,
    duration        REAL NOT NULL,
    start_time      REAL NOT NULL,
    sampled_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_samples_entry
    ON trace_samples (root_service, operation);

-- 无清晰根片段、被排除在归类之外的请求
CREATE TABLE IF NOT EXISTS trace_excluded (
    trace_id    TEXT PRIMARY KEY,
    reason      TEXT NOT NULL,
    excluded_at REAL NOT NULL
);

-- 每次请求并入样本前产出的对照结果
CREATE TABLE IF NOT EXISTS comparisons (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id           TEXT NOT NULL UNIQUE,
    root_service       TEXT NOT NULL,
    operation          TEXT NOT NULL,
    baseline_version   INTEGER NOT NULL,
    baseline_formed    INTEGER NOT NULL,
    min_samples        INTEGER NOT NULL,
    status             TEXT NOT NULL,
    is_anomaly         INTEGER NOT NULL,
    duration           REAL NOT NULL,
    threshold          REAL,
    bucket             TEXT NOT NULL,
    added_edges        TEXT NOT NULL,
    missing_edges      TEXT NOT NULL,
    baseline_quantiles TEXT NOT NULL,
    regular_edges      TEXT NOT NULL,
    compared_at        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_comparisons_anomaly
    ON comparisons (is_anomaly, compared_at);
CREATE INDEX IF NOT EXISTS idx_comparisons_entry
    ON comparisons (root_service, operation, compared_at);
"""

# 老库升级：建表时存在但没有 operation 列的 spans 表补列
_MIGRATIONS = (
    "ALTER TABLE spans ADD COLUMN operation TEXT",
)


class SpanStore:
    def __init__(self, path: str) -> None:
        self._path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._run_migrations()

    def _run_migrations(self) -> None:
        columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(spans)").fetchall()
        }
        for statement in _MIGRATIONS:
            # 简单 ALTER 迁移：按语句目标列是否已存在判断要不要执行
            target = statement.split("ADD COLUMN", 1)[1].strip().split()[0]
            if target not in columns:
                self._conn.execute(statement)
        self._conn.commit()

    def insert_span(self, span: SpanRecord, received_at_ms: float) -> bool:
        """插入片段；(trace_id, span_id) 已存在则忽略。返回是否新插入。"""
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT OR IGNORE INTO spans
                  (trace_id, span_id, parent_span_id, service, operation,
                   start_time, end_time, status_code, received_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    span.trace_id,
                    span.span_id,
                    span.parent_span_id,
                    span.service,
                    span.operation,
                    span.start_time,
                    span.end_time,
                    span.status_code,
                    received_at_ms,
                ),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def load_trace(self, trace_id: str) -> list[SpanRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM spans WHERE trace_id = ? ORDER BY start_time, span_id",
                (trace_id,),
            ).fetchall()
        return [SpanRecord.from_row(dict(r)) for r in rows]

    def all_spans(self) -> list[SpanRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM spans ORDER BY start_time, span_id"
            ).fetchall()
        return [SpanRecord.from_row(dict(r)) for r in rows]

    def list_traces(
        self,
        trace_id: Optional[str] = None,
        service: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict]:
        """检索请求列表，返回每个 trace 的摘要行。"""
        sql = "SELECT * FROM spans"
        where: list[str] = []
        params: list = []
        if trace_id:
            where.append("trace_id LIKE ?")
            params.append(f"%{trace_id}%")
        if service:
            where.append("service LIKE ?")
            params.append(f"%{service}%")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY start_time DESC"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()

        grouped: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            grouped.setdefault(row["trace_id"], []).append(row)

        summaries: list[dict] = []
        for tid, trace_rows in grouped.items():
            starts = min(r["start_time"] for r in trace_rows)
            ends = max(r["end_time"] for r in trace_rows)
            services = sorted({r["service"] for r in trace_rows})
            errors = sum(1 for r in trace_rows if r["status_code"] >= 400)
            summaries.append(
                {
                    "trace_id": tid,
                    "span_count": len(trace_rows),
                    "services": services,
                    "start_time": starts,
                    "duration": ends - starts,
                    "error_count": errors,
                }
            )
        summaries.sort(key=lambda s: -s["start_time"])
        return summaries[:limit]

    def spans_starting_since(self, since_ms: float) -> list[SpanRecord]:
        """取开始时间在窗口内的片段，供依赖图聚合。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM spans WHERE start_time >= ? ORDER BY start_time",
                (since_ms,),
            ).fetchall()
        return [SpanRecord.from_row(dict(r)) for r in rows]

    # ----------------------------------------------------- 入口样本/基线

    def insert_sample(self, sample: dict, sampled_at_ms: float) -> bool:
        """记录一个已归入入口的请求。trace 已存在则忽略。"""
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT OR IGNORE INTO trace_samples
                  (trace_id, root_service, operation, root_span_id,
                   duration, start_time, sampled_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sample["trace_id"],
                    sample["root_service"],
                    sample["operation"],
                    sample["root_span_id"],
                    sample["duration"],
                    sample["start_time"],
                    sampled_at_ms,
                ),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def insert_excluded(self, trace_id: str, reason: str, at_ms: float) -> bool:
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT OR IGNORE INTO trace_excluded (trace_id, reason, excluded_at)
                VALUES (?, ?, ?)
                """,
                (trace_id, reason, at_ms),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def settled_trace_ids(self) -> set[str]:
        """已经做过归类处理（入样或排除）的 trace，重启后不重复处理。"""
        with self._lock:
            sampled = {
                row["trace_id"]
                for row in self._conn.execute(
                    "SELECT trace_id FROM trace_samples"
                ).fetchall()
            }
            excluded = {
                row["trace_id"]
                for row in self._conn.execute(
                    "SELECT trace_id FROM trace_excluded"
                ).fetchall()
            }
        return sampled | excluded

    def excluded_trace_ids(self) -> set[str]:
        with self._lock:
            return {
                row["trace_id"]
                for row in self._conn.execute(
                    "SELECT trace_id FROM trace_excluded"
                ).fetchall()
            }

    def all_samples(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT trace_id, root_service, operation, root_span_id,
                       duration, start_time, sampled_at
                FROM trace_samples
                ORDER BY sampled_at, trace_id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def sample_edges(self, trace_id: str) -> list[tuple[str, str]]:
        """从持久化片段重算某条样本的调用边集合（供结构基线重建）。"""
        records = self.load_trace(trace_id)
        by_id = {record.span_id: record for record in records}
        edges: set[tuple[str, str]] = set()
        for record in records:
            parent = by_id.get(record.parent_span_id) if record.parent_span_id else None
            if parent is not None:
                edges.add((parent.service, record.service))
        return sorted(edges)

    def insert_comparison(self, record: dict) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO comparisons
                  (trace_id, root_service, operation, baseline_version,
                   baseline_formed, min_samples, status, is_anomaly, duration,
                   threshold, bucket, added_edges, missing_edges,
                   baseline_quantiles, regular_edges, compared_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["trace_id"],
                    record["root_service"],
                    record["operation"],
                    record["baseline_version"],
                    1 if record["baseline_formed"] else 0,
                    record["min_samples"],
                    record["status"],
                    1 if record["is_anomaly"] else 0,
                    record["duration"],
                    record["threshold"],
                    record["bucket"],
                    record["added_edges"],
                    record["missing_edges"],
                    record["baseline_quantiles"],
                    record["regular_edges"],
                    record["compared_at"],
                ),
            )
            self._conn.commit()

    def get_comparison(self, trace_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM comparisons WHERE trace_id = ?",
                (trace_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def recent_anomalies_by_entries(
        self, limit_per_entry: int = 5
    ) -> dict[tuple[str, str], list[dict]]:
        """各入口最近被判为异常的对照记录（供入口列表展示）。"""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM comparisons
                WHERE is_anomaly = 1
                ORDER BY root_service, operation, compared_at DESC
                """
            ).fetchall()
        grouped: dict[tuple[str, str], list[dict]] = {}
        for row in rows:
            key = (row["root_service"], row["operation"])
            bucket = grouped.setdefault(key, [])
            if len(bucket) < limit_per_entry:
                bucket.append(dict(row))
        return grouped

    def close(self) -> None:
        with self._lock:
            self._conn.close()
