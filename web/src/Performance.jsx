import React, { useEffect, useState } from "react";
import { api } from "./api.js";

const num = (v) => Number(v ?? 0).toLocaleString();

export default function Performance({ runId }) {
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

  if (error) return <><h1>Performance</h1><p className="err">{error}</p></>;
  if (!report) return <><h1>Performance</h1><p className="empty">Loading…</p></>;

  const totals = report.totals || {};
  const workload = report.workload || {};
  const rate = report.throughput || {};
  const roles = report.by_role || [];
  // Share of total calls, so the widest bar is the role doing the most work.
  const peak = Math.max(1, ...roles.map((r) => r.calls || 0));
  const retries = roles.reduce((sum, r) => sum + (r.retries || 0), 0);

  return (
    <>
      <h1>Performance</h1>
      <p className="sub">Measured per model call, not estimated</p>

      <div className="tiles">
        <div className="tile"><div className="label">Model calls</div>
          <div className="value">{num(totals.calls)}</div>
          <div className="foot">{num(totals.failures)} failed · {num(retries)} retried</div></div>
        <div className="tile"><div className="label">Tokens</div>
          <div className="value">{num(totals.tokens)}</div>
          <div className="foot">input + output</div></div>
        <div className="tile"><div className="label">Calls per conversation</div>
          <div className="value">{workload.calls_per_conversation ?? "—"}</div>
          <div className="foot">{num(workload.conversations)} decided</div></div>
        <div className="tile"><div className="label">Projected 10k</div>
          <div className="value">
            {workload.projected_10k_calls != null
              ? num(workload.projected_10k_calls) : "—"}</div>
          <div className="foot">model calls at current rate</div></div>
        <div className="tile"><div className="label">Live rate</div>
          <div className="value">{rate.calls_per_minute ?? 0}</div>
          <div className="foot">calls / min</div></div>
      </div>

      <section className="panel">
        <h2>Where the work goes</h2>
        <table>
          <thead><tr>
            <th>Role</th><th>Model</th><th>Share of calls</th>
            <th className="num">Calls</th><th className="num">Retries</th>
            <th className="num">Mean ms</th><th className="num">Max ms</th>
            <th className="num">Tokens</th>
          </tr></thead>
          <tbody>
            {roles.map((r) => (
              <tr key={`${r.role}-${r.model}`}>
                <td>{r.role}</td>
                <td className="mono">{r.model}</td>
                <td style={{ minWidth: 110 }}>
                  <div className="bar">
                    <span style={{ width: `${((r.calls || 0) / peak) * 100}%`,
                                   background: "var(--accent)" }} />
                  </div>
                </td>
                <td className="num">{num(r.calls)}</td>
                <td className="num">
                  {r.retries ? <span className="pill warn">{r.retries}</span> : "0"}
                </td>
                <td className="num">{num(r.mean_ms)}</td>
                <td className="num">{num(r.max_ms)}</td>
                <td className="num">{num((r.input_tokens || 0) + (r.output_tokens || 0))}</td>
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
