import React, { useEffect, useState } from "react";
import { api } from "./api.js";

const num = (v) => Number(v ?? 0).toLocaleString();
const ago = (ts) => {
  const s = Math.max(0, Math.floor(Date.now() / 1000 - (ts || 0)));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  return `${Math.floor(s / 3600)}h`;
};

const STATUS = {
  SUCCEEDED: "pass", READY: "idle", LEASED: "accent", DEAD: "fail",
};

export default function Activity() {
  const [d, setD] = useState(null);
  const [error, setError] = useState(null);
  const [live, setLive] = useState(true);

  useEffect(() => {
    const load = () => api.activity().then(({ data, error }) => { setD(data); setError(error); });
    load();
    if (!live) return;
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, [live]);

  if (error) return <><h1>Activity</h1><p className="err">{error}</p></>;
  if (!d) return <><h1>Activity</h1><p className="empty">Loading…</p></>;

  const rate = d.throughput || {};
  // A run with no calls in five minutes is stalled, whatever the totals say.
  const stalled = (rate.calls || 0) === 0;

  return (
    <>
      <h1>Activity</h1>
      <p className="sub">
        Live model calls and queue state.{" "}
        <button className="chip" aria-pressed={live} onClick={() => setLive(!live)}>
          {live ? "streaming · 5s" : "paused"}
        </button>
      </p>

      <div className="tiles">
        <div className="tile"><div className="label">Rate</div>
          <div className="value" style={{ color: stalled ? "var(--fail)" : undefined }}>
            {rate.calls_per_minute ?? 0}
          </div>
          <div className="foot">calls / min · last {rate.window_seconds ?? 300}s</div></div>
        <div className="tile"><div className="label">Calls in window</div>
          <div className="value">{num(rate.calls)}</div>
          <div className="foot">{num(rate.failures)} failed</div></div>
        <div className="tile"><div className="label">Dead letters</div>
          <div className="value">{num((d.dead || []).length)}</div>
          <div className="foot">{(d.dead || []).length ? "needs a look" : "clean"}</div></div>
      </div>

      {stalled && (
        <section className="panel" style={{ borderColor: "var(--fail)" }}>
          <h2 style={{ color: "var(--fail)" }}>No model calls in the last 5 minutes</h2>
          <p style={{ color: "var(--muted)", fontSize: 13 }}>
            The run is either finished, held at an approval gate, or stuck. Totals
            elsewhere will still look healthy — a stalled pipeline and a finished
            one are indistinguishable from a count.
          </p>
        </section>
      )}

      <div className="split">
        <section className="panel">
          <h2>Recent model calls</h2>
          <div className="list">
            {(d.calls || []).map((c, i) => (
              <div className="row" key={i} style={{ cursor: "default" }}>
                <span className="l1">
                  <span className="id">{c.role}</span>
                  <span className={`pill ${c.outcome === "SUCCEEDED" ? "pass" : "fail"}`}>
                    {c.outcome === "SUCCEEDED" ? `${(c.latency_ms / 1000).toFixed(1)}s` : c.error_class}
                  </span>
                </span>
                <span className="l2">
                  {c.model} · {ago(c.started_at)} ago
                  {c.attempts > 1 ? ` · ${c.attempts} attempts` : ""}
                  {c.packet_id ? ` · ${c.packet_id.slice(3, 15)}` : ""}
                </span>
              </div>
            ))}
            {!(d.calls || []).length && <p className="empty">No calls recorded yet.</p>}
          </div>
        </section>

        <section className="panel">
          <h2>Queue</h2>
          <table>
            <thead><tr><th>Stage</th><th>Status</th><th className="num">Count</th></tr></thead>
            <tbody>
              {(d.stages || []).map((s, i) => (
                <tr key={i}>
                  <td className="mono">{s.stage}</td>
                  <td><span className={`pill ${STATUS[s.status] || "idle"}`}>
                    {s.status.toLowerCase()}</span></td>
                  <td className="num">{num(s.n)}</td>
                </tr>
              ))}
            </tbody>
          </table>

          {(d.dead || []).length > 0 && (
            <>
              <h2 style={{ marginTop: 18 }}>Dead letters</h2>
              <div className="list">
                {d.dead.map((x, i) => (
                  <div className="row" key={i} style={{ cursor: "default" }}>
                    <span className="l1"><span className="id">{x.stage}</span>
                      <span className="pill fail">{ago(x.updated_at)} ago</span></span>
                    <span className="l2">
                      {(x.error || "").split("\n").filter(Boolean).slice(-1)[0]?.slice(0, 120)}
                    </span>
                  </div>
                ))}
              </div>
            </>
          )}
        </section>
      </div>

      <section className="panel">
        <h2>By role</h2>
        <table>
          <thead><tr>
            <th>Role</th><th>Model</th><th className="num">Calls</th>
            <th className="num">Failed</th><th className="num">Retries</th>
            <th className="num">Mean ms</th>
          </tr></thead>
          <tbody>
            {(d.by_role || []).map((r, i) => (
              <tr key={i}>
                <td>{r.role}</td><td className="mono">{r.model}</td>
                <td className="num">{num(r.calls)}</td>
                <td className="num">{num(r.failures)}</td>
                <td className="num">{num(r.retries)}</td>
                <td className="num">{num(r.mean_ms)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </>
  );
}
