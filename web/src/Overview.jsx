import React from "react";

const num = (v) => (v ?? 0).toLocaleString();

function Tile({ label, value, foot }) {
  return (
    <div className="tile">
      <div className="label">{label}</div>
      <div className="value">{value}</div>
      {foot && <div className="foot">{foot}</div>}
    </div>
  );
}

const COLORS = {
  SUCCEEDED: "var(--pass)", READY: "var(--idle)",
  LEASED: "var(--accent)", DEAD: "var(--fail)",
};

export default function Overview({ status }) {
  if (!status) return <p className="empty">Waiting for the harness…</p>;
  const stages = status.stages || {};
  const counts = status.counts || {};

  return (
    <>
      <h1>Overview</h1>
      <p className="sub">
        {status.process ? "Workers active" : "No workers running"} ·{" "}
        {num(status.model_calls)} concurrent model calls
      </p>

      <div className="tiles">
        <Tile label="Sourced" value={num(status.selected)} foot="conversations acquired" />
        <Tile label="Terminal" value={num(status.terminal_total)}
              foot={`${num(status.remaining)} remaining`} />
        <Tile label="Candidates" value={num(status.candidates)}
              foot={`${status.yield_pct ?? 0}% of terminal`} />
        <Tile label="Throughput" value={num(status.throughput_per_hour)} foot="terminal / hour" />
      </div>

      <section className="panel">
        <h2>Pipeline</h2>
        <table>
          <thead>
            <tr>
              <th>Stage</th><th>Progress</th>
              <th className="num">Done</th><th className="num">Active</th>
              <th className="num">Queued</th><th className="num">Dead</th>
            </tr>
          </thead>
          <tbody>
            {Object.entries(stages).map(([name, v]) => {
              const total = (v.succeeded || 0) + (v.active || 0) + (v.queued || 0) + (v.dead || 0);
              const seg = (n, key) =>
                total ? <span key={key} style={{ width: `${(n / total) * 100}%`,
                          background: COLORS[key] }} /> : null;
              return (
                <tr key={name}>
                  <td className="mono">{name.replace(/_/g, " ")}</td>
                  <td style={{ minWidth: 170 }}>
                    <div className="bar">
                      {seg(v.succeeded, "SUCCEEDED")}{seg(v.active, "LEASED")}
                      {seg(v.queued, "READY")}{seg(v.dead, "DEAD")}
                    </div>
                  </td>
                  <td className="num">{num(v.succeeded)}</td>
                  <td className="num">{num(v.active)}</td>
                  <td className="num">{num(v.queued)}</td>
                  <td className="num">
                    {v.dead ? <span className="pill fail">{v.dead}</span> : "0"}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </section>

      <section className="panel">
        <h2>Outcomes</h2>
        <div className="tiles">
          {Object.entries(counts).filter(([, v]) => typeof v === "number").map(([k, v]) => (
            <Tile key={k} label={k.replace(/_/g, " ").toLowerCase()} value={num(v)} />
          ))}
        </div>
      </section>
    </>
  );
}
