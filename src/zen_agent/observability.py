"""Record what every model call did and how long it took.

The queue records *whether* work finished. It says nothing about how many model
calls each role took, how long they ran, how often a model had to be retried, or
which stage is consuming the work. That gap has bitten twice:

* 63% of one run's model calls went to repair loops, and nobody knew until it
  was counted by hand.
* A crash-looping driver restarted every 13 seconds for an hour while the
  supervisor reported it as healthy, because "restarted" and "made progress"
  were never distinguished.

So this module records one row per model call — role, model, latency, tokens,
retries, outcome — in its own database. Its own, deliberately: the queue
is on the hot write path with dozens of workers contending, and observability
must never be able to slow down or block the thing it observes.

Nothing here is allowed to raise into a caller. A metrics failure that kills a
completed model call would make the instrument more expensive than the problem.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import sqlite3
import time
from typing import Any


@dataclass(slots=True)
class CallRecord:
    """One model call, from the harness's point of view."""

    run_id: str
    role: str
    provider: str
    model: str
    stage: str = ""
    packet_id: str = ""
    latency_ms: int = 0
    attempts: int = 1
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    reasoning_effort: str = ""
    outcome: str = "SUCCEEDED"
    error_class: str = ""
    started_at: float = field(default_factory=time.time)


class MetricsStore:
    """Append-only record of model calls, in its own database."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=15000")
        # `synchronous=NORMAL`: losing the last few metric rows in a hard crash
        # is acceptable; slowing every model call to fsync them is not.
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS model_calls (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              run_id TEXT NOT NULL,
              stage TEXT NOT NULL DEFAULT '',
              role TEXT NOT NULL,
              provider TEXT NOT NULL,
              model TEXT NOT NULL,
              packet_id TEXT NOT NULL DEFAULT '',
              latency_ms INTEGER NOT NULL DEFAULT 0,
              attempts INTEGER NOT NULL DEFAULT 1,
              input_tokens INTEGER NOT NULL DEFAULT 0,
              output_tokens INTEGER NOT NULL DEFAULT 0,
              reasoning_tokens INTEGER NOT NULL DEFAULT 0,
              reasoning_effort TEXT NOT NULL DEFAULT '',
              outcome TEXT NOT NULL DEFAULT 'SUCCEEDED',
              error_class TEXT NOT NULL DEFAULT '',
              started_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_calls_run ON model_calls(run_id, started_at);
            CREATE INDEX IF NOT EXISTS idx_calls_role ON model_calls(run_id, role);
            """
        )

    def close(self) -> None:
        try:
            self.db.close()
        except sqlite3.Error:
            pass

    def record(self, call: CallRecord) -> None:
        try:
            self.db.execute(
                """INSERT INTO model_calls
                (run_id, stage, role, provider, model, packet_id, latency_ms,
                 attempts, input_tokens, output_tokens, reasoning_tokens,
                 reasoning_effort, outcome, error_class, started_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    call.run_id, call.stage, call.role, call.provider, call.model,
                    call.packet_id, call.latency_ms, call.attempts, call.input_tokens,
                    call.output_tokens, call.reasoning_tokens,
                    call.reasoning_effort, call.outcome, call.error_class,
                    call.started_at,
                ),
            )
        except sqlite3.Error:
            # Never let instrumentation fail the work it is measuring.
            pass

    # ---- reporting -------------------------------------------------------

    def by_role(self, run_id: str | None = None) -> list[dict[str, Any]]:
        """Where the calls and the time actually went, per role."""
        where, params = ("WHERE run_id=?", (run_id,)) if run_id else ("", ())
        rows = self.db.execute(
            f"""SELECT role, model,
                       COUNT(*) AS calls,
                       SUM(outcome != 'SUCCEEDED') AS failures,
                       SUM(attempts) - COUNT(*) AS retries,
                       ROUND(AVG(latency_ms)) AS mean_ms,
                       MAX(latency_ms) AS max_ms,
                       SUM(input_tokens) AS input_tokens,
                       SUM(output_tokens) AS output_tokens
                FROM model_calls {where}
                GROUP BY role, model ORDER BY calls DESC""",
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def totals(self, run_id: str | None = None) -> dict[str, Any]:
        where, params = ("WHERE run_id=?", (run_id,)) if run_id else ("", ())
        row = self.db.execute(
            f"""SELECT COUNT(*) AS calls,
                       SUM(outcome != 'SUCCEEDED') AS failures,
                       SUM(input_tokens + output_tokens) AS tokens,
                       MIN(started_at) AS first_at, MAX(started_at) AS last_at
                FROM model_calls {where}""",
            params,
        ).fetchone()
        return dict(row) if row else {}

    def throughput(self, run_id: str, window_seconds: int = 300) -> dict[str, Any]:
        """Recent call rate — the number that distinguishes 'running' from 'stuck'."""
        since = time.time() - window_seconds
        row = self.db.execute(
            """SELECT COUNT(*) AS calls, SUM(outcome != 'SUCCEEDED') AS failures
               FROM model_calls WHERE run_id=? AND started_at > ?""",
            (run_id, since),
        ).fetchone()
        result = dict(row) if row else {"calls": 0}
        calls = result.get("calls") or 0
        result["calls_per_minute"] = round(calls / (window_seconds / 60), 1)
        result["window_seconds"] = window_seconds
        return result

    def failure_reasons(self, run_id: str | None = None, limit: int = 10) -> list[dict]:
        where, params = ("AND run_id=?", (run_id,)) if run_id else ("", ())
        rows = self.db.execute(
            f"""SELECT error_class, role, COUNT(*) AS n FROM model_calls
                WHERE outcome != 'SUCCEEDED' {where}
                GROUP BY error_class, role ORDER BY n DESC LIMIT ?""",
            (*params, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def calls_per_conversation(self, run_id: str) -> dict[str, Any]:
        """Model calls divided by conversations that reached a decision.

        The number that answers "how much work does one conversation take?",
        which the queue cannot answer because it counts items, not calls.
        """
        row = self.db.execute(
            """SELECT COUNT(*) AS calls, COUNT(DISTINCT packet_id) AS packets
               FROM model_calls WHERE run_id=? AND packet_id != ''""",
            (run_id,),
        ).fetchone()
        calls = (row["calls"] or 0) if row else 0
        packets = (row["packets"] or 0) if row else 0
        return {
            "calls": calls,
            "conversations": packets,
            "calls_per_conversation": round(calls / packets, 2) if packets else None,
            "projected_10k_calls": int(calls / packets * 10_000) if packets else None,
        }
