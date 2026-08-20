import React, { useEffect, useMemo, useState } from "react";
import { api } from "./api.js";

const STATUS_CLASS = {
  VERIFIED_CANDIDATE: "pass", PARTIAL_CANDIDATE: "warn",
  QUARANTINED: "fail", NOT_SELECTED: "idle",
};

function Turn({ turn }) {
  if (turn.role === "user") {
    return (
      <article className="turn user">
        <div className="head">
          <span className="who">caller</span>
          <span className="pill pass">source preserved</span>
        </div>
        <p className="utterance">{turn.text}</p>
      </article>
    );
  }
  const replaced = turn.action === "REPLACE";
  return (
    <article className="turn">
      <div className="head">
        <span className="who">agent</span>
        <span className={`pill ${replaced ? "warn" : "pass"}`}>{turn.action}</span>
        {turn.source_quality && <span className="pill idle">{turn.source_quality}</span>}
        {turn.excluded_from_golden && <span className="pill fail">excluded</span>}
      </div>
      {replaced ? (
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
        <p className="utterance">{turn.golden_text || turn.source_text}</p>
      )}
      {turn.correction_reason && <p className="reason">{turn.correction_reason}</p>}
    </article>
  );
}

export default function Traces() {
  const [index, setIndex] = useState([]);
  const [selected, setSelected] = useState(null);
  const [detail, setDetail] = useState(null);
  const [query, setQuery] = useState("");
  const [error, setError] = useState(null);

  useEffect(() => {
    api.conversations().then(({ data, error }) => {
      setError(error);
      setIndex(data?.conversations || []);
    });
  }, []);

  useEffect(() => {
    if (!selected) return;
    setDetail(null);
    api.conversation(selected).then(({ data }) => setDetail(data));
  }, [selected]);

  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    const rows = q
      ? index.filter((c) =>
          [c.source_id, c.domain, c.language, c.status, c.reason]
            .filter(Boolean).join(" ").toLowerCase().includes(q))
      : index;
    return rows.slice(0, 400);
  }, [index, query]);

  return (
    <>
      <h1>Call traces</h1>
      <p className="sub">
        {index.length.toLocaleString()} conversations · every caller turn byte-identical to source
      </p>
      {error && <p className="err">{error}</p>}
      <div className="split">
        <section className="panel">
          <input className="search" placeholder="Search id, domain, language, status…"
                 value={query} onChange={(e) => setQuery(e.target.value)} />
          <div className="list" style={{ marginTop: 12 }}>
            {shown.map((c) => (
              <button key={c.source_id} className="row"
                      aria-current={c.source_id === selected}
                      onClick={() => setSelected(c.source_id)}>
                <span className="l1">
                  <span className="mono">#{c.number} {c.source_id}</span>
                  <span className={`pill ${STATUS_CLASS[c.status] || "idle"}`}>
                    {(c.status || "").replace(/_/g, " ").toLowerCase()}
                  </span>
                </span>
                <span className="l2">
                  {c.domain} · {c.language} · {c.assistant_turns ?? "?"} agent turns
                  {c.replaced ? ` · ${c.replaced} corrected` : ""}
                </span>
              </button>
            ))}
            {!shown.length && <p className="empty">Nothing matches.</p>}
          </div>
        </section>

        <section className="panel">
          {!selected && <p className="empty">Select a conversation.</p>}
          {selected && !detail && <p className="empty">Loading…</p>}
          {detail && (
            <>
              <h2>{detail.source_id} · {detail.terminal?.status?.replace(/_/g, " ").toLowerCase()}</h2>
              {detail.terminal?.reason && <p className="reason">{detail.terminal.reason}</p>}
              <div style={{ marginTop: 14 }}>
                {(detail.turns || []).map((t) => <Turn key={t.turn_id} turn={t} />)}
              </div>
            </>
          )}
        </section>
      </div>
    </>
  );
}
