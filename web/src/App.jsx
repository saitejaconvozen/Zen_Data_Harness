import React, { useEffect, useMemo, useState } from "react";
import { api } from "./api.js";
import Overview from "./Overview.jsx";
import Traces from "./Traces.jsx";
import Economics from "./Economics.jsx";

const VIEWS = [
  ["overview", "Overview"],
  ["traces", "Call traces"],
  ["economics", "Cost & latency"],
];

export default function App() {
  const [view, setView] = useState("overview");
  const [status, setStatus] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      const { data, error } = await api.status();
      if (cancelled) return;
      if (data) setStatus(data);
      setError(error);
    };
    load();
    // A batch runs for hours; polling keeps the page honest without a socket.
    const timer = setInterval(load, 15000);
    return () => { cancelled = true; clearInterval(timer); };
  }, []);

  const runId = status?.run_id;

  return (
    <div className="shell">
      <aside className="side">
        <div className="brand">
          Zen Data Engine
          <small>{runId ? `run ${runId.slice(0, 8)}` : "connecting…"}</small>
        </div>
        <nav className="nav">
          {VIEWS.map(([key, label]) => (
            <button key={key} aria-current={view === key} onClick={() => setView(key)}>
              {label}
            </button>
          ))}
        </nav>
        {error && <p className="err" style={{ marginTop: 18 }}>{error}</p>}
      </aside>
      <main className="main">
        {view === "overview" && <Overview status={status} />}
        {view === "traces" && <Traces />}
        {view === "economics" && <Economics runId={runId} />}
      </main>
    </div>
  );
}
