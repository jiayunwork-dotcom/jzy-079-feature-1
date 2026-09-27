import React, { useEffect, useState } from 'react';
import { listEntries } from '../api.js';

// 入口浏览：样本量、延迟基线分位、最近异常请求；点开异常请求看对照详情
export default function EntryList({ onSelect, refreshTick }) {
  const [entries, setEntries] = useState([]);
  const [error, setError] = useState(null);

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

  return (
    <section className="entry-list">
      <div className="section-title">入口基线</div>
      {error && <div className="error-banner">{error}</div>}
      {entries.length === 0 ? (
        <div className="placeholder-panel">还没有可归类的请求样本</div>
      ) : (
        <table>
          <thead>
            <tr>
              <th>入口服务</th>
              <th>操作</th>
              <th>样本量</th>
              <th>p50</th>
              <th>p95</th>
              <th>p99</th>
              <th>常规边</th>
              <th>最近异常</th>
            </tr>
          </thead>
          <tbody>
            {entries.map((e) => {
              const key = `${e.service}/${e.operation}`;
              return (
                <tr key={key}>
                  <td>{e.service}</td>
                  <td className="mono">{e.operation}</td>
                  <td>
                    {e.sample_count}
                    {!e.baseline_ready && (
                      <span className="badge badge-immature">基线未成型</span>
                    )}
                  </td>
                  <td>{fmtMs(e.p50_ms)}</td>
                  <td>{fmtMs(e.p95_ms)}</td>
                  <td>{fmtMs(e.p99_ms)}</td>
                  <td>{e.regular_edge_count}</td>
                  <td>
                    {e.recent_anomalies.length === 0 ? (
                      <span className="muted">—</span>
                    ) : (
                      <div className="anomaly-chips">
                        {e.recent_anomalies.slice(0, 5).map((a) => (
                          <button
                            key={a.trace_id}
                            className="anomaly-chip"
                            title={`查看 ${a.trace_id} 的对照详情`}
                            onClick={() =>
                              onSelect({
                                traceId: a.trace_id,
                                service: e.service,
                                operation: e.operation,
                              })
                            }
                          >
                            <span
                              className={`dot ${
                                a.anomaly_types.includes('latency')
                                  ? 'dot-latency'
                                  : 'dot-structure'
                              }`}
                            />
                            <span className="mono">{shortId(a.trace_id)}</span>
                          </button>
                        ))}
                      </div>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </section>
  );
}

function fmtMs(v) {
  return v == null ? '—' : `${v.toFixed(0)} ms`;
}

function shortId(id) {
  return id.length > 14 ? `${id.slice(0, 12)}…` : id;
}
