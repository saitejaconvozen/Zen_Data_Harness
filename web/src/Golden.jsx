import React, { useEffect, useMemo, useState } from "react";
import { api } from "./api.js";

const num = (v) => Number(v ?? 0).toLocaleString();

function Metrics({ metrics }) {
  if (!metrics?.length) return null;
  return (
    <div className="metrics">
      {metrics.map((m, i) => (
        <div className="metric" key={i}>
          <span className="path">
            {m.axis} <em>›</em> {m.subaxis} <em>›</em> {m.variant}
          </span>
          <span className="verdicts">
            <span className="pill fail">source {m.source_verdict || "—"}</span>
            <span className="pill pass">golden {m.golden_verdict || "—"}</span>
          </span>
        </div>
      ))}
    </div>
  );
}

function AgentTurn({ turn }) {
  return (
    <article className={`turn agent${turn.changed ? " changed" : ""}`}>
      <div className="head">
        <span className="who">agent</span>
        <span className={`pill ${turn.changed ? "warn" : "pass"}`}>{turn.action}</span>
        {turn.source_quality && <span className="pill idle">{turn.source_quality}</span>}
        {turn.excluded_from_golden && <span className="pill fail">excluded</span>}
        {turn.tool_calls?.length ? (
          <span className="pill accent">
            {turn.tool_calls.length} tool call{turn.tool_calls.length > 1 ? "s" : ""}
          </span>
        ) : null}
        <code className="tid">{turn.turn_id}</code>
      </div>
      {turn.changed ? (
        <div className="compare">
          <div className="pane">
            <h4>What the agent said</h4>
            <p className="utterance">{turn.source_text}</p>
          </div>
          <div className="pane golden">
            <h4>Corrected</h4>
            <p className="utterance">{turn.golden_text}</p>
          </div>
        </div>
      ) : (
        <p className="utterance">{turn.golden_text}</p>
      )}
      {turn.correction_reason && <p className="reason">{turn.correction_reason}</p>}
      <Metrics metrics={turn.metrics} />
    </article>
  );
}

function CallerTurn({ turn }) {
  return (
    <article className="turn caller">
      <div className="head">
        <span className="who">caller</span>
        <span className="pill pass">byte-identical</span>
        <code className="tid">{turn.turn_id}</code>
      </div>
      <p className="utterance">{turn.text}</p>
    </article>
  );
}

function Exchange({ exchange, direction }) {
  // Render in the order the call actually ran: inbound opens with the caller,
  // outbound with the agent. Pairing without direction is off by one for half
  // the corpus.
  const order = direction === "INBOUND" ? ["user", "assistant"] : ["assistant", "user"];
  return (
    <section className="exchange">
      <span className="ex-index">{exchange.index + 1}</span>
      <div className="ex-body">
        {order.flatMap((role) =>
          (exchange[role] || []).map((t, i) =>
            role === "user"
              ? <CallerTurn key={`${role}-${i}`} turn={t} />
              : <AgentTurn key={`${role}-${i}`} turn={t} />
          )
        )}
      </div>
    </section>
  );
}

export default function Golden() {
  const [data, setData] = useState(null);
  const [selected, setSelected] = useState(null);
  const [query, setQuery] = useState("");
  const [only, setOnly] = useState("all");
  const [error, setError] = useState(null);

  useEffect(() => {
    api.golden().then(({ data, error }) => { setData(data); setError(error); });
  }, []);

  const rows = useMemo(() => {
    const all = data?.conversations || [];
    const q = query.trim().toLowerCase();
    return all.filter((c) => {
      if (only === "corrected" && !c.counts.corrected_turns) return false;
      if (only === "inbound" && c.call_direction !== "INBOUND") return false;
      if (only === "outbound" && c.call_direction !== "OUTBOUND") return false;
      if (!q) return true;
      return [c.short_id, c.domain, c.primary_language, ...(c.axes_touched || [])]
        .filter(Boolean).join(" ").toLowerCase().includes(q);
    });
  }, [data, query, only]);

  const current = useMemo(
    () => rows.find((c) => c.short_id === selected) || rows[0] || null,
    [rows, selected]
  );

  if (error) return <><h1>Golden conversations</h1><p className="err">{error}</p></>;
  if (!data) return <><h1>Golden conversations</h1><p className="empty">Loading…</p></>;

  const corrected = (data.conversations || []).reduce(
    (n, c) => n + c.counts.corrected_turns, 0);
  const turns = (data.conversations || []).reduce(
    (n, c) => n + c.counts.assistant_turns, 0);

  return (
    <>
      <h1>Golden conversations</h1>
      <p className="sub">
        {num(data.count)} ready to dispatch · {num(turns)} agent turns ·{" "}
        {num(corrected)} corrected ({turns ? ((corrected / turns) * 100).toFixed(1) : 0}%)
        · every caller turn byte-identical to source
      </p>

      <div className="split">
        <section className="panel">
          <input className="search" placeholder="Search id, domain, language, axis…"
                 value={query} onChange={(e) => setQuery(e.target.value)} />
          <div className="chips" style={{ marginTop: 10 }}>
            {[["all", "All"], ["corrected", "Corrected only"],
              ["inbound", "Inbound"], ["outbound", "Outbound"]].map(([k, label]) => (
              <button key={k} className="chip" aria-pressed={only === k}
                      onClick={() => setOnly(k)}>{label}</button>
            ))}
          </div>
          <div className="list" style={{ marginTop: 12 }}>
            {rows.slice(0, 400).map((c) => (
              <button key={c.short_id} className="row"
                      aria-current={current?.short_id === c.short_id}
                      onClick={() => setSelected(c.short_id)}>
                <span className="l1">
                  <span className="id">{c.short_id}</span>
                  <span className={`pill ${c.call_direction === "INBOUND" ? "idle" : "accent"}`}>
                    {c.call_direction.toLowerCase()}
                  </span>
                </span>
                <span className="l2">
                  {c.domain || "unclassified"} · {c.primary_language || "—"} ·{" "}
                  {c.counts.exchanges} exchanges
                  {c.counts.corrected_turns
                    ? ` · ${c.counts.corrected_turns} corrected` : ""}
                </span>
              </button>
            ))}
            {!rows.length && <p className="empty">Nothing matches.</p>}
          </div>
        </section>

        <section className="panel">
          {!current && <p className="empty">Select a conversation.</p>}
          {current && (
            <>
              <div className="detail-head">
                <div>
                  <h2>{current.short_id}</h2>
                  <p className="reason">
                    {current.domain} · {current.primary_language} ·{" "}
                    {current.turn_order}
                  </p>
                </div>
                <span className="pill pass">{current.terminal_status?.replace(/_/g, " ").toLowerCase()}</span>
              </div>

              {current.metric_coverage?.length > 0 && (
                <div className="coverage">
                  <h3>Metric coverage</h3>
                  {current.metric_coverage.map((ax) => (
                    <div className="cov-axis" key={ax.axis}>
                      <span className="cov-name">{ax.axis}</span>
                      <span className="cov-n">{ax.count}</span>
                      <div className="cov-subs">
                        {ax.subaxes.map((s) => (
                          <span className="cov-sub" key={s.subaxis}>
                            {s.subaxis} <em>×{s.count}</em>
                          </span>
                        ))}
                      </div>
                    </div>
                  ))}
                </div>
              )}

              <div className="exchanges">
                {current.exchanges.map((ex) => (
                  <Exchange key={ex.index} exchange={ex}
                            direction={current.call_direction} />
                ))}
              </div>
            </>
          )}
        </section>
      </div>
    </>
  );
}
