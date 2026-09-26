import React, { useEffect, useState } from 'react';
import { getComparison } from '../api.js';

const STATUS_TEXT = {
  unformed: '基线尚未成型',
  normal: '正常',
  degraded: '延迟劣化',
  drifted: '结构漂移',
  degraded_drifted: '延迟劣化 + 结构漂移',
};

const BUCKET_TEXT = {
  normal: '处于 p95 以内的正常范围',
  between_p95_p99: '落在 p95 与 p99 之间',
  above_p99: '高于基线 p99',
  unknown: '基线未成型，无法定位分位',
};

function edgeKey(e) {
  return `${e.caller}→${e.callee}`;
}

// 单次请求与它所属入口基线的对照详情：慢在哪一档、多/少了哪些边
export default function ComparisonDetail({ traceId, onClose }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    getComparison(traceId)
      .then((d) => {
        if (!cancelled) setData(d);
      })
      .catch((err) => {
        if (!cancelled) setError(String(err.message || err));
      });
    return () => {
      cancelled = true;
    };
  }, [traceId]);

  if (error) {
    return (
      <div className="comparison-detail">
        <div className="detail-header">
          <h3>对照详情</h3>
          <button onClick={onClose}>关闭</button>
        </div>
        <div className="error-banner">{error}</div>
      </div>
    );
  }
  if (!data) {
    return (
      <div className="comparison-detail">
        <div className="placeholder-panel">加载中…</div>
      </div>
    );
  }

  const addedSet = new Set(
    (data.structure.added_edges || []).map(edgeKey)
  );
  const missingSet = new Set(
    (data.structure.missing_edges || []).map(edgeKey)
  );
  const latency = data.latency;
  const baselineEdges = data.baseline.structure?.edges || [];

  return (
    <div className="comparison-detail">
      <div className="detail-header">
        <h3>
          对照详情 <span className="mono">{data.trace_id}</span>
        </h3>
        <button onClick={onClose}>关闭</button>
      </div>

      <div className="cmp-meta">
        <span>
          入口：<b className="mono">{data.entry_name}</b>
        </span>
        <span>
          判定依据：基线第 <b>{data.baseline_version}</b> 版（并入本次请求之前）
        </span>
        <span className={`status-tag status-${data.status}`}>
          {STATUS_TEXT[data.status] || data.status}
        </span>
      </div>

      {!data.baseline_formed && (
        <div className="unformed-banner">
          该入口样本数（{data.baseline_version}）尚未达到最低门槛{' '}
          {data.min_samples}，基线尚未成型，本次不给延迟或结构的正式异常判定。
        </div>
      )}

      {data.baseline_formed && (
        <>
          <div className="cmp-section">
            <h4>延迟对照</h4>
            <div className="latency-grid">
              <div>
                <div className="metric-label">本次端到端</div>
                <div className="metric-value">{latency.duration.toFixed(1)} ms</div>
              </div>
              <div>
                <div className="metric-label">劣化阈值（p95 × 倍数）</div>
                <div className="metric-value">
                  {latency.threshold != null
                    ? `${latency.threshold.toFixed(1)} ms`
                    : '—'}
                </div>
              </div>
              <div>
                <div className="metric-label">分位定位</div>
                <div
                  className={`metric-value ${
                    latency.degraded ? 'has-error' : ''
                  }`}
                >
                  {BUCKET_TEXT[latency.bucket] || latency.bucket}
                </div>
              </div>
            </div>
            <div className="quantile-strip">
              {(['p50', 'p95', 'p99']).map((q) => (
                <span key={q} className="quantile-chip">
                  {q} {data.baseline.quantiles[q]?.toFixed(0) ?? '—'} ms
                </span>
              ))}
            </div>
          </div>

          <div className="cmp-section">
            <h4>结构对照（相对常规路径，频率门槛 {data.baseline.structure_edge_frequency}）</h4>
            {data.structure.added_edges.length === 0 &&
              data.structure.missing_edges.length === 0 && (
                <div className="muted">本次调用结构与常规结构一致，无偏差。</div>
              )}
            {data.structure.added_edges.length > 0 && (
              <div className="drift-block">
                <div className="drift-title edge-added">多出的调用（基线常规路径里没有）</div>
                <ul>
                  {data.structure.added_edges.map((e) => (
                    <li key={edgeKey(e)} className="edge-added">
                      {e.caller} → {e.callee}
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {data.structure.missing_edges.length > 0 && (
              <div className="drift-block">
                <div className="drift-title edge-missing">消失的调用（常规路径有、本次没出现）</div>
                <ul>
                  {data.structure.missing_edges.map((e) => (
                    <li key={edgeKey(e)} className="edge-missing">
                      {e.caller} → {e.callee}
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {baselineEdges.length > 0 && (
              <div className="baseline-edges">
                <div className="drift-title muted">入口基线全部边与频率</div>
                <table>
                  <thead>
                    <tr>
                      <th>调用边</th>
                      <th>出现次数</th>
                      <th>频率</th>
                      <th>本次</th>
                    </tr>
                  </thead>
                  <tbody>
                    {baselineEdges.map((e) => {
                      const key = `${e.caller}→${e.callee}`;
                      const isAdded = addedSet.has(key);
                      const isMissing = missingSet.has(key);
                      return (
                        <tr
                          key={key}
                          className={
                            isAdded
                              ? 'row-added'
                              : isMissing
                                ? 'row-missing'
                                : ''
                          }
                        >
                          <td className="mono">{key}</td>
                          <td>{e.count}</td>
                          <td>
                            {(e.frequency * 100).toFixed(0)}%
                            {e.regular && <span className="badge regular">常规</span>}
                          </td>
                          <td>
                            {isAdded ? (
                              <span className="edge-added">新增</span>
                            ) : isMissing ? (
                              <span className="edge-missing">消失</span>
                            ) : (
                              <span className="muted">一致</span>
                            )}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
}
