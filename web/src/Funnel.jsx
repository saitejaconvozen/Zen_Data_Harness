import React, { useEffect, useState } from "react";
import { api } from "./api.js";

const num = (v) => Number(v ?? 0).toLocaleString();

export default function Funnel() {
  const [d, setD] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    const load = () => api.funnel().then(({ data, error }) => { setD(data); setError(error); });
    load();
    const t = setInterval(load, 30000);
    return () => clearInterval(t);
  }, []);

  if (error) return <><h1>Quality funnel</h1><p className="err">{error}</p></>;
  if (!d) return <><h1>Quality funnel</h1><p className="empty">Loading…</p></>;

  const e = d.export || {};
  const bound = d.stages?.[0]?.count || 1;
  const survival = (d.survival_rate || 0) * 100;

  return (
    <>
      <h1>Quality funnel</h1>
      <p className="sub">
        Where conversations are lost between MongoDB and the training set.
        Every number counted, none estimated.
      </p>

      <div className="tiles">
        <div className="tile"><div className="label">Survival rate</div>
          <div className="value">{survival.toFixed(1)}%</div>
          <div className="foot">bound → exportable</div></div>
        <div className="tile"><div className="label">Fetch for 10k</div>
          <div className="value">{d.fetch_needed_for_10k ? num(d.fetch_needed_for_10k) : "—"}</div>
          <div className="foot">at the current rate</div></div>
        <div className="tile"><div className="label">SFT targets</div>
          <div className="value">{num(e.sft_targets)}</div>
          <div className="foot">{num(e.masked_no_speech)} masked, no speech</div></div>
        <div className="tile"><div className="label">Tool turns</div>
          <div className="value">{num(e.tool_turns)}</div>
          <div className="foot">
            {e.silent_tool_turns
              ? <span className="pill fail">{num(e.silent_tool_turns)} silent</span>
              : "all speak to the caller"}
          </div></div>
      </div>

      <section className="panel">
        <h2>Attrition by stage</h2>
        <table>
          <thead><tr>
            <th>Stage</th><th>Survives</th>
            <th className="num">Count</th><th className="num">% of bound</th>
            <th className="num">Lost here</th><th>What the gate asks</th>
          </tr></thead>
          <tbody>
            {(d.stages || []).map((s) => {
              const pct = s.share_of_bound || 0;
              // The widest loss is the gate worth arguing about.
              const heavy = s.lost_here > bound * 0.15;
              return (
                <tr key={s.stage}>
                  <td className="mono">{s.stage}</td>
                  <td style={{ minWidth: 150 }}>
                    <div className="bar">
                      <span style={{ width: `${pct}%`, background: "var(--pass)" }} />
                      <span style={{ width: `${100 - pct}%`,
                                     background: heavy ? "var(--fail)" : "var(--plane)" }} />
                    </div>
                  </td>
                  <td className="num">{num(s.count)}</td>
                  <td className="num">{pct.toFixed(1)}%</td>
                  <td className="num">
                    {s.lost_here
                      ? <span className={`pill ${heavy ? "fail" : "warn"}`}>{num(s.lost_here)}</span>
                      : "0"}
                  </td>
                  <td style={{ color: "var(--muted)", fontSize: 12 }}>{s.description}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </section>

      <section className="panel">
        <h2>Terminal outcomes</h2>
        <div className="tiles">
          {Object.entries(d.terminal_breakdown || {}).map(([k, v]) => (
            <div className="tile" key={k}>
              <div className="label">{k.replace(/_/g, " ").toLowerCase()}</div>
              <div className="value">{num(v)}</div>
            </div>
          ))}
        </div>
      </section>
    </>
  );
}
