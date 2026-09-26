import React, { useEffect, useMemo, useState } from 'react';
import { listEntries } from '../api.js';
import ComparisonDetail from './ComparisonDetail.jsx';

const STATUS_TEXT = {
  degraded: '延迟劣化',
  drifted: '结构漂移',
  degraded_drifted: '延迟劣化 + 结构漂移',
};

const BUCKET_TEXT = {
  normal: '正常范围',
  between_p95_p99: 'p95 ~ p99',
  above_p99: '高于 p99',
  unknown: '—',
};

function edgeLabel(e) {
  return `${e.caller} → ${e.callee}`;
}

// 入口浏览：各入口样本量、延迟基线分位、最近异常请求；点开异常看对照详情
export default function EntryBrowser({ refreshTick }) {
  const [entries, setEntries] = useState([]);
  const [error, setError] = useState(null);
  const [expanded, setExpanded] = useState(null);
  const [selectedTrace, setSelectedTrace] = useState(null);

  useEffect(() => {
    let cancelled = false;
    listEntries()
      .then((data) => {
        if (!cancelled) {
          setEntries(data.entries);
          setError(null);
        }
      })
      .catch((err) => {
        if (!cancelled) setError(String(err.message || err));
      });
    return () => {
      cancelled = true;
    };
  }, [refreshTick]);

  const entryMap = useMemo(
    () => new Map(entries.map((e) => [e.name, e])),
    [entries]
  );

  return (
    <div className="entries-page">
      <section className="entry-list">
        <h2>入口基线</h2>
        {error && <div className="error-banner">{error}</div>}
        <table>
          <thead>
            <tr>
              <th>入口（根服务 : 操作）</th>
              <th>样本量</th>
              <th>p50</th>
              <th>p95</th>
              <th>p99</th>
              <th>最近异常</th>
            </tr>
          </thead>
          <tbody>
            {entries.map((entry) => {
              const key = entry.name;
              const isOpen = expanded === key;
              return (
                <React.Fragment key={key}>
                  <tr
                    className="entry-row"
                    onClick={() => setExpanded(isOpen ? null : key)}
                  >
                    <td className="mono">
                      {entry.is_default_operation
                        ? entry.root_service
                        : `${entry.root_service} : ${entry.operation}`}
                    </td>
                    <td>
                      {entry.sample_count}
                      {!entry.baseline_formed && (
                        <span className="badge unformed">
                          基线未成型（需 {entry.min_samples}）
                        </span>
                      )}
                    </td>
                    <td>{fmtQ(entry.quantiles.p50, entry.baseline_formed)}</td>
                    <td>{fmtQ(entry.quantiles.p95, entry.baseline_formed)}</td>
                    <td>{fmtQ(entry.quantiles.p99, entry.baseline_formed)}</td>
                    <td>
                      {entry.recent_anomalies.length > 0 ? (
                        <span className="anomaly-count">
                          {entry.recent_anomalies.length} 条
                          <span className="expand-hint">
                            {isOpen ? ' ▲' : ' ▼'}
                          </span>
                        </span>
                      ) : (
                        <span className="muted">—</span>
                      )}
                    </td>
                  </tr>
                  {isOpen && (
                    <tr className="anomaly-row-panel">
                      <td colSpan={6}>
                        {entry.recent_anomalies.length === 0 ? (
                          <div className="muted">近期没有被判为异常的请求</div>
                        ) : (
                          <table className="anomaly-table">
                            <thead>
                              <tr>
                                <th>追踪编号</th>
                                <th>判定</th>
                                <th>分位档</th>
                                <th>本次耗时 / 阈值</th>
                                <th>新增边</th>
                                <th>消失边</th>
                                <th></th>
                              </tr>
                            </thead>
                            <tbody>
                              {entry.recent_anomalies.map((a) => (
                                <tr key={a.trace_id}>
                                  <td className="mono">{a.trace_id}</td>
                                  <td className="has-error">
                                    {STATUS_TEXT[a.status] || a.status}
                                  </td>
                                  <td>{BUCKET_TEXT[a.bucket] || a.bucket}</td>
                                  <td>
                                    {a.duration.toFixed(0)} ms
                                    {a.threshold != null &&
                                      ` / ${a.threshold.toFixed(0)} ms`}
                                  </td>
                                  <td className="edge-added">
                                    {a.added_edges.length
                                      ? a.added_edges.map(edgeLabel).join('，')
                                      : '—'}
                                  </td>
                                  <td className="edge-missing">
                                    {a.missing_edges.length
                                      ? a.missing_edges.map(edgeLabel).join('，')
                                      : '—'}
                                  </td>
                                  <td>
                                    <button
                                      onClick={() =>
                                        setSelectedTrace(a.trace_id)
                                      }
                                    >
                                      对照详情
                                    </button>
                                  </td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        )}
                      </td>
                    </tr>
                  )}
                </React.Fragment>
              );
            })}
            {entries.length === 0 && (
              <tr>
                <td colSpan={6} className="empty">
                  暂无入口数据（无清晰根片段的请求不会在此出现）
                </td>
              </tr>
            )}
          </tbody>
        </table>
        {selectedTrace && (
          <ComparisonDetail
            traceId={selectedTrace}
            onClose={() => setSelectedTrace(null)}
          />
        )}
      </section>
    </div>
  );
}

function fmtQ(value, formed) {
  if (!formed || value == null) return <span className="muted">—</span>;
  return `${value.toFixed(0)} ms`;
}
