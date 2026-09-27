import React, { useEffect, useState } from 'react';
import { getComparison, getEntryDetail } from '../api.js';

// 对照详情：某次请求“这次 vs 平时”——慢在哪一档、多了/少了哪些边，
// 与所属入口的基线摆在一起展示。纯渲染，不做任何编辑操作。
export default function ComparisonDetail({ selection, onClose, refreshTick }) {
  const { traceId, service, operation } = selection;
  const [comparison, setComparison] = useState(null);
  const [baseline, setBaseline] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    Promise.all([
      getComparison(traceId),
      getEntryDetail(service, operation).catch(() => null),
    ])
      .then(([cmp, detail]) => {
        if (!cancelled) {
          setComparison(cmp);
          setBaseline(detail);
          setError(null);
        }
      })
      .catch((err) => {
        if (!cancelled) setError(String(err.message || err));
      });
    return () => {
      cancelled = true;
    };
  }, [traceId, service, operation, refreshTick]);

  if (error) return <div className="error-banner">{error}</div>;
  if (!comparison) return <div className="placeholder-panel">加载中…</div>;

  const entry = comparison.entry;

  return (
    <section className="comparison-detail">
      <div className="waterfall-header">
        <h2>
          对照结果 <span className="mono">{comparison.trace_id}</span>
        </h2>
        <button className="close-btn" onClick={onClose}>
          ✕
        </button>
      </div>

      <div className="cmp-entry">
        入口 <b>{entry.service}</b> / <span className="mono">{entry.operation}</span>
        <span className="muted">
          （对照基线版本 v{comparison.baseline_version}，即该请求并入前的
          {comparison.sample_count_before} 条历史样本）
        </span>
      </div>

      <StatusBanner comparison={comparison} />

      {comparison.status === 'immature' ? (
        <div className="immature-note">
          {comparison.reason}。样本积累到最低条数后才开始给出正式的延迟与结构判定。
        </div>
      ) : (
        <>
          <LatencyPanel comparison={comparison} baseline={baseline} />
          <StructurePanel comparison={comparison} baseline={baseline} />
        </>
      )}
    </section>
  );
}

function StatusBanner({ comparison }) {
  const map = {
    ok: { cls: 'status-ok', text: '正常：与平时一致' },
    degraded: { cls: 'status-degraded', text: '⚠ 延迟劣化' },
    drifted: { cls: 'status-drifted', text: '⚠ 结构漂移' },
    degraded_and_drifted: {
      cls: 'status-both',
      text: '⚠ 延迟劣化 + 结构漂移',
    },
    immature: { cls: 'status-immature', text: '基线尚未成型' },
  };
  const meta = map[comparison.status] || { cls: '', text: comparison.status };
  return <div className={`status-banner ${meta.cls}`}>{meta.text}</div>;
}

const BAND_LABELS = {
  below_p50: '低于中位数（比一半以上的平时请求都快）',
  p50_to_p95: '中位数 ~ p95 之间（平时范围内）',
  p95_to_p99: 'p95 ~ p99 之间（偏慢）',
  above_p99: '高于 p99（极慢）',
};

function LatencyPanel({ comparison, baseline }) {
  const latency = comparison.latency;
  const bq = baseline?.latency || {};
  return (
    <div className="cmp-panel">
      <h3>延迟对照（端到端）</h3>
      <div className="cmp-latency-row">
        <div className="cmp-current">
          <div className="cmp-big">{comparison.duration_ms.toFixed(1)} ms</div>
          <div className="muted">本次耗时</div>
        </div>
        <div className="cmp-vs">vs</div>
        <div className="cmp-quantiles">
          <table className="mini-table">
            <thead>
              <tr>
                <th />
                <th>对照时基线（不含本次）</th>
                <th>当前基线</th>
              </tr>
            </thead>
            <tbody>
              {['p50', 'p95', 'p99'].map((p) => (
                <tr key={p}>
                  <td className="muted">{p}</td>
                  <td>{fmt(latency.quantiles[p])}</td>
                  <td>{fmt(bq[`${p}_ms`])}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
      <div className="cmp-line">
        所处档位：<b>{BAND_LABELS[latency.band] || latency.band}</b>
      </div>
      <div className="cmp-line">
        劣化阈值：p95 × {latency.slow_multiplier} ={' '}
        {fmt(latency.threshold_ms)}，结论：
        <b className={latency.verdict === 'degraded' ? 'text-danger' : 'text-ok'}>
          {latency.verdict === 'degraded' ? '延迟劣化' : '正常'}
        </b>
      </div>
    </div>
  );
}

function StructurePanel({ comparison, baseline }) {
  const structure = comparison.structure;
  const added = structure.added_edges;
  const missing = structure.missing_edges;
  const baselineEdges = new Map(
    (baseline?.structure?.edges || []).map((e) => [
      `${e.source}->${e.target}`,
      e,
    ])
  );

  return (
    <div className="cmp-panel">
      <h3>结构对照（谁调用谁）</h3>
      <div className="cmp-line muted">
        常规路径门槛：出现率 ≥ {(structure.regular_ratio * 100).toFixed(0)}%；
        对照时常规边 {structure.regular_edge_count} 条
      </div>

      <EdgeGroup
        title="多出来的调用（本次有、常规路径没有）"
        edges={added}
        cls="edge-added"
        baselineEdges={baselineEdges}
      />
      <EdgeGroup
        title="消失的调用（常规路径有、本次没有）"
        edges={missing}
        cls="edge-missing"
        baselineEdges={baselineEdges}
      />
      {added.length === 0 && missing.length === 0 && (
        <div className="text-ok cmp-line">调用结构与平时完全一致，无结构偏差。</div>
      )}
    </div>
  );
}

function EdgeGroup({ title, edges, cls, baselineEdges }) {
  return (
    <div className={`edge-group ${cls}`}>
      <div className="edge-group-title">
        {title}（{edges.length}）
      </div>
      {edges.length === 0 ? (
        <div className="muted cmp-line">无</div>
      ) : (
        <ul>
          {edges.map((e) => {
            const key = `${e.source}->${e.target}`;
            const info = baselineEdges.get(key);
            return (
              <li key={key} className="mono">
                <span className="edge-arrow">
                  {e.source} → {e.target}
                </span>
                {info ? (
                  <span className="muted">
                    {' '}
                    当前基线出现 {info.count} 次（
                    {(info.ratio * 100).toFixed(0)}%）
                  </span>
                ) : (
                  <span className="muted"> 当前基线也从未出现过</span>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

function fmt(v) {
  return v == null ? '—' : `${Number(v).toFixed(1)} ms`;
}
