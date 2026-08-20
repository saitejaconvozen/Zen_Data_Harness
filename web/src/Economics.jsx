import React, { useEffect, useState } from "react";
import { api } from "./api.js";

const money = (v) => `$${Number(v ?? 0).toFixed(4)}`;
const num = (v) => Number(v ?? 0).toLocaleString();

export default function Economics({ runId }) {
  const [report, setReport] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    const load = () => api.metrics(runId).then(({ data, error }) => {
      setReport(data); setError(error);
    });
    load();
    const timer = setInterval(load, 20000);
    return () => clearInterval(timer);
  }, [runId]);

  if (error) return <><h1>Cost &amp; latency</h1><p className="err">{error}</p></>;
  if (!report) return <><h1>Cost &amp; latency</h1><p className="empty">Loading…</p></>;

  const totals = report.totals || {};
  const economics = report.economics || {};
  const rate = report.throughput || {};
  const roles = report.by_role || [];
  const peak = Math.max(1, ...roles.map((r) => r.cost_usd || 0));

  return (
    <>
      <h1>Cost &amp; latency</h1>
      <p className="sub">Measured per model call, not estimated</p>

      <div className="tiles">
        <div className="tile"><div className="label">Model calls</div>
          <div className="value">{num(totals.calls)}</div>
          <div className="foot">{num(totals.failures)} failed</div></div>
        <div className="tile"><div className="label">Spend</div>
          <div className="value">{money(totals.cost_usd)}</div>
          <div className="foot">{num(totals.tokens)} tokens</div></div>
        <div className="tile"><div className="label">Per conversation</div>
          <div className="value">
            {economics.cost_per_conversation != null
              ? money(economics.cost_per_conversation) : "—"}</div>
          <div className="foot">{num(economics.conversations)} decided</div></div>
        <div className="tile"><div className="label">Projected 10k</div>
          <div className="value">
            {economics.projected_10k_usd != null
              ? `$${economics.projected_10k_usd.toFixed(2)}` : "—"}</div>
          <div className="foot">at current rate</div></div>
        <div className="tile"><div className="label">Live rate</div>
          <div className="value">{rate.calls_per_minute ?? 0}</div>
          <div className="foot">calls / min</div></div>
      </div>

      <section className="panel">
        <h2>Where the budget goes</h2>
        <table>
          <thead><tr>
            <th>Role</th><th>Model</th><th>Share</th>
            <th className="num">Calls</th><th className="num">Retries</th>
            <th className="num">Mean ms</th><th className="num">Tokens</th>
            <th className="num">Cost</th>
          </tr></thead>
          <tbody>
            {roles.map((r) => (
              <tr key={`${r.role}-${r.model}`}>
                <td>{r.role}</td>
                <td className="mono">{r.model}</td>
                <td style={{ minWidth: 110 }}>
                  <div className="bar">
                    <span style={{ width: `${((r.cost_usd || 0) / peak) * 100}%`,
                                   background: "var(--accent)" }} />
                  </div>
                </td>
                <td className="num">{num(r.calls)}</td>
                <td className="num">
                  {r.retries ? <span className="pill warn">{r.retries}</span> : "0"}
                </td>
                <td className="num">{num(r.mean_ms)}</td>
                <td className="num">{num((r.input_tokens || 0) + (r.output_tokens || 0))}</td>
                <td className="num">{money(r.cost_usd)}</td>
              </tr>
            ))}
            {!roles.length && <tr><td colSpan={8} className="empty">No calls recorded yet.</td></tr>}
          </tbody>
        </table>
      </section>

      {(report.failures || []).length > 0 && (
        <section className="panel">
          <h2>Failures</h2>
          <table>
            <thead><tr><th>Error</th><th>Role</th><th className="num">Count</th></tr></thead>
            <tbody>
              {report.failures.map((f, i) => (
                <tr key={i}><td className="mono">{f.error_class}</td>
                  <td>{f.role}</td><td className="num">{num(f.n)}</td></tr>
              ))}
            </tbody>
          </table>
        </section>
      )}
    </>
  );
}
