from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Any
from uuid import uuid4

from .models import ArtifactRecord, Plan, RunStatus, TaskStatus, utc_now


class EventStore:
    def __init__(self, path: Path):
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self._initialize()

    def close(self) -> None:
        self.connection.close()

    def _initialize(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY,
                objective TEXT NOT NULL,
                workflow TEXT NOT NULL,
                status TEXT NOT NULL,
                inputs_json TEXT NOT NULL,
                explanation TEXT NOT NULL,
                config_fingerprint TEXT NOT NULL,
                terminal_reason TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES runs(id),
                task_key TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                name TEXT NOT NULL,
                tool TEXT NOT NULL,
                inputs_json TEXT NOT NULL,
                depends_on_json TEXT NOT NULL,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL,
                required INTEGER NOT NULL,
                output_sha256 TEXT,
                error TEXT,
                UNIQUE(run_id, task_key)
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL REFERENCES runs(id),
                task_id TEXT,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS artifacts (
                sha256 TEXT NOT NULL,
                run_id TEXT NOT NULL REFERENCES runs(id),
                task_id TEXT,
                relative_path TEXT NOT NULL,
                bytes INTEGER NOT NULL,
                media_type TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(run_id, sha256)
            );
            CREATE INDEX IF NOT EXISTS idx_tasks_run ON tasks(run_id, sequence);
            CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, id);
            """
        )
        self.connection.commit()

    def create_run(self, plan: Plan, config_fingerprint: str) -> str:
        run_id = uuid4().hex
        now = utc_now()
        with self.connection:
            self.connection.execute(
                "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)",
                (
                    run_id,
                    plan.objective,
                    plan.workflow,
                    RunStatus.PLANNED.value,
                    json.dumps(plan.inputs, sort_keys=True),
                    plan.explanation,
                    config_fingerprint,
                    now,
                    now,
                ),
            )
            for sequence, task in enumerate(plan.tasks):
                task_id = uuid4().hex
                self.connection.execute(
                    """INSERT INTO tasks
                    (id, run_id, task_key, sequence, name, tool, inputs_json,
                     depends_on_json, status, attempts, max_attempts, required)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)""",
                    (
                        task_id,
                        run_id,
                        task.key,
                        sequence,
                        task.name,
                        task.tool,
                        json.dumps(task.inputs, sort_keys=True),
                        json.dumps(list(task.depends_on)),
                        TaskStatus.PENDING.value,
                        task.max_attempts,
                        int(task.required),
                    ),
                )
            self._append_event(run_id, None, "run.created", plan.to_dict())
        return run_id

    def _append_event(
        self, run_id: str, task_id: str | None, event_type: str, payload: dict[str, Any]
    ) -> None:
        self.connection.execute(
            "INSERT INTO events(run_id, task_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (run_id, task_id, event_type, json.dumps(payload, sort_keys=True), utc_now()),
        )

    def event(
        self, run_id: str, event_type: str, payload: dict[str, Any], task_id: str | None = None
    ) -> None:
        with self.connection:
            self._append_event(run_id, task_id, event_type, payload)

    def get_run(self, run_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown run: {run_id}")
        value = dict(row)
        value["inputs"] = json.loads(value.pop("inputs_json"))
        return value

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]

    def set_run_status(
        self, run_id: str, status: RunStatus, reason: str | None = None
    ) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE runs SET status = ?, terminal_reason = ?, updated_at = ? WHERE id = ?",
                (status.value, reason, utc_now(), run_id),
            )
            self._append_event(
                run_id, None, "run.status_changed", {"status": status.value, "reason": reason}
            )

    def list_tasks(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM tasks WHERE run_id = ? ORDER BY sequence", (run_id,)
        ).fetchall()
        values: list[dict[str, Any]] = []
        for row in rows:
            value = dict(row)
            value["inputs"] = json.loads(value.pop("inputs_json"))
            value["depends_on"] = json.loads(value.pop("depends_on_json"))
            value["required"] = bool(value["required"])
            values.append(value)
        return values

    def start_task(self, task_id: str) -> dict[str, Any]:
        with self.connection:
            row = self.connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            if row["status"] != TaskStatus.PENDING.value:
                raise ValueError(f"task is not pending: {row['status']}")
            attempts = row["attempts"] + 1
            self.connection.execute(
                "UPDATE tasks SET status = ?, attempts = ?, error = NULL WHERE id = ?",
                (TaskStatus.RUNNING.value, attempts, task_id),
            )
            self._append_event(
                row["run_id"], task_id, "task.started", {"attempt": attempts, "tool": row["tool"]}
            )
        return next(item for item in self.list_tasks(row["run_id"]) if item["id"] == task_id)

    def set_task_status(
        self,
        task_id: str,
        status: TaskStatus,
        *,
        error: str | None = None,
        output_sha256: str | None = None,
    ) -> None:
        with self.connection:
            row = self.connection.execute("SELECT run_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            self.connection.execute(
                "UPDATE tasks SET status = ?, error = ?, output_sha256 = COALESCE(?, output_sha256) WHERE id = ?",
                (status.value, error, output_sha256, task_id),
            )
            self._append_event(
                row["run_id"], task_id, "task.status_changed",
                {"status": status.value, "error": error, "output_sha256": output_sha256},
            )

    def register_artifact(
        self, run_id: str, task_id: str | None, record: ArtifactRecord
    ) -> None:
        with self.connection:
            self.connection.execute(
                """INSERT OR IGNORE INTO artifacts
                (sha256, run_id, task_id, relative_path, bytes, media_type, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.sha256,
                    run_id,
                    task_id,
                    record.relative_path,
                    record.bytes,
                    record.media_type,
                    utc_now(),
                ),
            )
            self._append_event(
                run_id, task_id, "artifact.committed", record.to_dict()
            )

    def list_artifacts(self, run_id: str) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM artifacts WHERE run_id = ? ORDER BY created_at", (run_id,)
            ).fetchall()
        ]

    def trace(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM events WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()
        values = []
        for row in rows:
            value = dict(row)
            value["payload"] = json.loads(value.pop("payload_json"))
            values.append(value)
        return values

    def prepare_resume(self, run_id: str) -> None:
        run = self.get_run(run_id)
        if run["status"] == RunStatus.SUCCEEDED.value:
            raise ValueError("a succeeded run does not need resume")
        with self.connection:
            self.connection.execute(
                """UPDATE tasks SET status = ?, error = NULL
                WHERE run_id = ? AND status IN (?, ?) AND attempts < max_attempts""",
                (
                    TaskStatus.PENDING.value,
                    run_id,
                    TaskStatus.RUNNING.value,
                    TaskStatus.FAILED.value,
                ),
            )
            self.connection.execute(
                "UPDATE tasks SET status = ?, error = NULL WHERE run_id = ? AND status = ?",
                (TaskStatus.PENDING.value, run_id, TaskStatus.BLOCKED.value),
            )
            self.connection.execute(
                "UPDATE runs SET status = ?, terminal_reason = NULL, updated_at = ? WHERE id = ?",
                (RunStatus.PLANNED.value, utc_now(), run_id),
            )
            self._append_event(run_id, None, "run.resumed", {})
