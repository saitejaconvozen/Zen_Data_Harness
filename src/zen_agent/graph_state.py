from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Any
from uuid import uuid4

from .graph import GraphPlan
from .models import utc_now


class GraphState:
    def __init__(self, path: Path):
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS graph_runs (
              id TEXT PRIMARY KEY, graph_name TEXT NOT NULL, objective TEXT NOT NULL,
              plan_json TEXT NOT NULL, status TEXT NOT NULL, reason TEXT,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS graph_executions (
              id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES graph_runs(id),
              lane_key TEXT NOT NULL, node_key TEXT NOT NULL, round_number INTEGER NOT NULL,
              status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
              max_attempts INTEGER NOT NULL, input_json TEXT NOT NULL,
              route TEXT, output_sha256 TEXT, error TEXT,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              UNIQUE(run_id, lane_key, node_key, round_number)
            );
            CREATE TABLE IF NOT EXISTS graph_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              run_id TEXT NOT NULL REFERENCES graph_runs(id), execution_id TEXT,
              event_type TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_graph_execution_ready
              ON graph_executions(run_id, status, created_at);
            """
        )
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def create_run(self, plan: GraphPlan) -> str:
        run_id = uuid4().hex
        now = utc_now()
        with self.db:
            self.db.execute(
                "INSERT INTO graph_runs VALUES (?, ?, ?, ?, 'PLANNED', NULL, ?, ?)",
                (run_id, plan.graph, plan.objective, json.dumps(plan.to_dict(), sort_keys=True), now, now),
            )
            for lane in plan.lanes:
                node = plan.node(plan.start_node)
                self._insert_execution(run_id, lane.key, node.key, 0, node.max_attempts, {})
            self._event(run_id, None, "graph.created", plan.to_dict())
        return run_id

    def _insert_execution(self, run_id: str, lane: str, node: str, round_number: int, max_attempts: int, inputs: dict) -> bool:
        now = utc_now()
        cursor = self.db.execute(
            """INSERT OR IGNORE INTO graph_executions
            (id, run_id, lane_key, node_key, round_number, status, attempts,
             max_attempts, input_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'PENDING', 0, ?, ?, ?, ?)""",
            (uuid4().hex, run_id, lane, node, round_number, max_attempts, json.dumps(inputs, sort_keys=True), now, now),
        )
        return cursor.rowcount == 1

    def schedule(self, run_id: str, lane: str, node: str, round_number: int, max_attempts: int, inputs: dict) -> bool:
        with self.db:
            created = self._insert_execution(run_id, lane, node, round_number, max_attempts, inputs)
            if created:
                self._event(run_id, None, "node.scheduled", {"lane": lane, "node": node, "round": round_number})
            return created

    def _event(self, run_id: str, execution_id: str | None, event_type: str, payload: dict) -> None:
        self.db.execute(
            "INSERT INTO graph_events(run_id, execution_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (run_id, execution_id, event_type, json.dumps(payload, sort_keys=True), utc_now()),
        )

    def event(self, run_id: str, event_type: str, payload: dict, execution_id: str | None = None) -> None:
        with self.db:
            self._event(run_id, execution_id, event_type, payload)

    def run(self, run_id: str) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM graph_runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        value = dict(row)
        value["plan"] = json.loads(value.pop("plan_json"))
        return value

    def executions(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM graph_executions WHERE run_id=? ORDER BY created_at, lane_key, round_number",
            (run_id,),
        ).fetchall()
        values = []
        for row in rows:
            value = dict(row)
            value["inputs"] = json.loads(value.pop("input_json"))
            values.append(value)
        return values

    def start(self, execution_id: str, inputs: dict) -> dict[str, Any]:
        with self.db:
            row = self.db.execute("SELECT * FROM graph_executions WHERE id=?", (execution_id,)).fetchone()
            if row is None or row["status"] != "PENDING":
                raise ValueError("graph execution is not pending")
            attempts = row["attempts"] + 1
            self.db.execute(
                "UPDATE graph_executions SET status='RUNNING', attempts=?, input_json=?, error=NULL, updated_at=? WHERE id=?",
                (attempts, json.dumps(inputs, sort_keys=True), utc_now(), execution_id),
            )
            self._event(row["run_id"], execution_id, "node.started", {"attempt": attempts})
        return next(item for item in self.executions(row["run_id"]) if item["id"] == execution_id)

    def finish(self, execution_id: str, *, route: str, output_sha256: str) -> None:
        with self.db:
            row = self.db.execute("SELECT run_id FROM graph_executions WHERE id=?", (execution_id,)).fetchone()
            self.db.execute(
                "UPDATE graph_executions SET status='SUCCEEDED', route=?, output_sha256=?, updated_at=? WHERE id=?",
                (route, output_sha256, utc_now(), execution_id),
            )
            self._event(row["run_id"], execution_id, "node.succeeded", {"route": route, "output_sha256": output_sha256})

    def fail(self, execution_id: str, error: str) -> None:
        with self.db:
            row = self.db.execute("SELECT run_id, attempts, max_attempts FROM graph_executions WHERE id=?", (execution_id,)).fetchone()
            status = "PENDING" if row["attempts"] < row["max_attempts"] else "FAILED"
            self.db.execute(
                "UPDATE graph_executions SET status=?, error=?, updated_at=? WHERE id=?",
                (status, error, utc_now(), execution_id),
            )
            self._event(row["run_id"], execution_id, "node.failed", {"status": status, "error": error})

    def set_run(self, run_id: str, status: str, reason: str | None = None) -> None:
        with self.db:
            self.db.execute(
                "UPDATE graph_runs SET status=?, reason=?, updated_at=? WHERE id=?",
                (status, reason, utc_now(), run_id),
            )
            self._event(run_id, None, "graph.status", {"status": status, "reason": reason})

    def resume(self, run_id: str) -> None:
        with self.db:
            self.db.execute(
                """UPDATE graph_executions SET status='PENDING', error=NULL, updated_at=?
                WHERE run_id=? AND status IN ('RUNNING','FAILED') AND attempts < max_attempts""",
                (utc_now(), run_id),
            )
            self.db.execute("UPDATE graph_runs SET status='PLANNED', reason=NULL, updated_at=? WHERE id=?", (utc_now(), run_id))
            self._event(run_id, None, "graph.resumed", {})

    def trace(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT * FROM graph_events WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
        values = []
        for row in rows:
            value = dict(row)
            value["payload"] = json.loads(value.pop("payload_json"))
            values.append(value)
        return values
