from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import time
from typing import Any
from uuid import uuid4

from .factory import FactoryManifest


class FactoryControlState:
    def __init__(self, path: Path):
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS factory_runs (
              id TEXT PRIMARY KEY, status TEXT NOT NULL, manifest_json TEXT NOT NULL,
              reason TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS factory_cycles (
              id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES factory_runs(id),
              cycle_number INTEGER NOT NULL, status TEXT NOT NULL,
              observation_sha256 TEXT NOT NULL, proposal_sha256 TEXT,
              critique_sha256 TEXT, compiled_sha256 TEXT, error TEXT,
              created_at REAL NOT NULL, updated_at REAL NOT NULL,
              UNIQUE(run_id, cycle_number)
            );
            CREATE TABLE IF NOT EXISTS factory_control_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
              cycle_number INTEGER, event_type TEXT NOT NULL,
              payload_json TEXT NOT NULL, created_at REAL NOT NULL
            );
            """
        )
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def create_run(self, manifest: FactoryManifest) -> str:
        manifest.validate()
        run_id = uuid4().hex
        now = time.time()
        with self.db:
            self.db.execute(
                "INSERT INTO factory_runs VALUES (?, 'PLANNED', ?, NULL, ?, ?)",
                (run_id, json.dumps(manifest.to_dict(), sort_keys=True), now, now),
            )
            self._event(run_id, None, "factory.created", {"target_accepted": manifest.target_accepted})
        return run_id

    def run(self, run_id: str) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM factory_runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        value = dict(row)
        value["manifest"] = json.loads(value.pop("manifest_json"))
        return value

    def set_run_status(self, run_id: str, status: str, reason: str | None = None) -> None:
        allowed = {"PLANNED", "RUNNING", "PAUSED", "NEEDS_HUMAN", "SUCCEEDED", "FAILED", "CANCELLED"}
        if status not in allowed:
            raise ValueError("invalid factory run status")
        with self.db:
            self.db.execute(
                "UPDATE factory_runs SET status=?,reason=?,updated_at=? WHERE id=?",
                (status, reason, time.time(), run_id),
            )
            self._event(run_id, None, "factory.status", {"status": status, "reason": reason})

    def start_cycle(self, run_id: str, cycle: int, observation_sha256: str) -> None:
        now = time.time()
        with self.db:
            self.db.execute(
                """INSERT INTO factory_cycles
                (id,run_id,cycle_number,status,observation_sha256,created_at,updated_at)
                VALUES (?, ?, ?, 'PLANNING', ?, ?, ?)""",
                (uuid4().hex, run_id, cycle, observation_sha256, now, now),
            )
            self.db.execute(
                "UPDATE factory_runs SET status='RUNNING', reason=NULL, updated_at=? WHERE id=?",
                (now, run_id),
            )
            self._event(run_id, cycle, "cycle.started", {"observation_sha256": observation_sha256})

    def finish_cycle(
        self,
        run_id: str,
        cycle: int,
        *,
        status: str,
        proposal_sha256: str,
        critique_sha256: str,
        compiled_sha256: str,
        action: str,
    ) -> None:
        now = time.time()
        run_status = {"COMPLETE": "SUCCEEDED", "PAUSE": "PAUSED"}.get(action, "RUNNING")
        with self.db:
            self.db.execute(
                """UPDATE factory_cycles SET status=?, proposal_sha256=?, critique_sha256=?,
                   compiled_sha256=?, updated_at=? WHERE run_id=? AND cycle_number=?""",
                (status, proposal_sha256, critique_sha256, compiled_sha256, now, run_id, cycle),
            )
            self.db.execute(
                "UPDATE factory_runs SET status=?, updated_at=? WHERE id=?",
                (run_status, now, run_id),
            )
            self._event(run_id, cycle, "cycle.compiled", {"action": action, "compiled_sha256": compiled_sha256})

    def fail_cycle(self, run_id: str, cycle: int, error: str) -> None:
        now = time.time()
        with self.db:
            self.db.execute(
                "UPDATE factory_cycles SET status='FAILED', error=?, updated_at=? WHERE run_id=? AND cycle_number=?",
                (error[-4000:], now, run_id, cycle),
            )
            self.db.execute(
                "UPDATE factory_runs SET status='NEEDS_HUMAN', reason=?, updated_at=? WHERE id=?",
                (error[-4000:], now, run_id),
            )
            self._event(run_id, cycle, "cycle.failed", {"error": error[-4000:]})

    def next_cycle(self, run_id: str) -> int:
        row = self.db.execute(
            "SELECT COALESCE(MAX(cycle_number), -1) + 1 AS next FROM factory_cycles WHERE run_id=?",
            (run_id,),
        ).fetchone()
        return int(row["next"])

    def cycles(self, run_id: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM factory_cycles WHERE run_id=? ORDER BY cycle_number", (run_id,)
        ).fetchall()]

    def _event(self, run_id: str, cycle: int | None, event_type: str, payload: dict[str, Any]) -> None:
        self.db.execute(
            "INSERT INTO factory_control_events(run_id,cycle_number,event_type,payload_json,created_at) VALUES (?,?,?,?,?)",
            (run_id, cycle, event_type, json.dumps(payload, sort_keys=True), time.time()),
        )
