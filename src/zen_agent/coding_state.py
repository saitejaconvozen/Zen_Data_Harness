from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any, Iterator
from uuid import uuid4


SESSION_STATUSES = frozenset(
    {
        "PLANNED",
        "RUNNING",
        "WAITING_FOR_HUMAN",
        "VERIFYING",
        "PAUSED",
        "SUCCEEDED",
        "FAILED",
        "CANCELLED",
    }
)
TOOL_CALL_STATUSES = frozenset({"PENDING", "RUNNING", "SUCCEEDED", "FAILED", "DENIED"})


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class CodingStateStore:
    """Durable state and append-only audit events for autonomous coding sessions."""

    def __init__(self, path: Path):
        self.path = path.expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=30000")
        self._initialize()

    def close(self) -> None:
        with self._lock:
            self.db.close()

    def __enter__(self) -> CodingStateStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock, self.db:
            yield

    def _initialize(self) -> None:
        with self._lock:
            self.db.executescript(
                """
                CREATE TABLE IF NOT EXISTS coding_sessions (
                    id TEXT PRIMARY KEY,
                    objective TEXT NOT NULL,
                    workspace TEXT NOT NULL,
                    status TEXT NOT NULL,
                    model TEXT NOT NULL,
                    agent_name TEXT NOT NULL,
                    parent_session_id TEXT REFERENCES coding_sessions(id),
                    metadata_json TEXT NOT NULL,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    terminal_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS coding_turns (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES coding_sessions(id),
                    sequence INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    agent_name TEXT NOT NULL,
                    content_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(session_id, sequence)
                );
                CREATE TABLE IF NOT EXISTS coding_tool_calls (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES coding_sessions(id),
                    turn_id TEXT REFERENCES coding_turns(id),
                    tool_name TEXT NOT NULL,
                    arguments_json TEXT NOT NULL,
                    result_json TEXT,
                    status TEXT NOT NULL,
                    error TEXT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT
                );
                CREATE TABLE IF NOT EXISTS coding_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL REFERENCES coding_sessions(id),
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS coding_feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL REFERENCES coding_sessions(id),
                    kind TEXT NOT NULL,
                    message TEXT NOT NULL,
                    author TEXT NOT NULL,
                    handled INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    handled_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_coding_sessions_updated
                    ON coding_sessions(updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_coding_events_session
                    ON coding_events(session_id, id);
                CREATE INDEX IF NOT EXISTS idx_coding_feedback_pending
                    ON coding_feedback(session_id, handled, id);
                CREATE TRIGGER IF NOT EXISTS coding_events_no_update
                    BEFORE UPDATE ON coding_events
                    BEGIN SELECT RAISE(ABORT, 'coding events are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS coding_events_no_delete
                    BEFORE DELETE ON coding_events
                    BEGIN SELECT RAISE(ABORT, 'coding events are append-only'); END;
                """
            )
            self.db.commit()

    def create_session(
        self,
        objective: str,
        workspace: str | Path,
        *,
        model: str = "gpt-5.6-sol",
        agent_name: str = "coordinator",
        parent_session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> str:
        objective = objective.strip()
        if not objective:
            raise ValueError("objective must not be empty")
        if parent_session_id is not None:
            self.get_session(parent_session_id)
        identifier = session_id or uuid4().hex
        now = _utc_now()
        workspace_value = str(Path(workspace).expanduser().resolve())
        with self._transaction():
            self.db.execute(
                """INSERT INTO coding_sessions
                (id, objective, workspace, status, model, agent_name, parent_session_id,
                 metadata_json, cancel_requested, terminal_reason, created_at, updated_at)
                VALUES (?, ?, ?, 'PLANNED', ?, ?, ?, ?, 0, NULL, ?, ?)""",
                (
                    identifier,
                    objective,
                    workspace_value,
                    model,
                    agent_name,
                    parent_session_id,
                    _json(metadata or {}),
                    now,
                    now,
                ),
            )
            self._append_event(
                identifier,
                "session.created",
                {
                    "objective": objective,
                    "workspace": workspace_value,
                    "model": model,
                    "agent_name": agent_name,
                    "parent_session_id": parent_session_id,
                },
            )
        return identifier

    def get_session(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            row = self.db.execute(
                "SELECT * FROM coding_sessions WHERE id=?", (session_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown coding session: {session_id}")
        return self._session_value(row)

    def list_sessions(
        self, *, limit: int = 50, status: str | None = None
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        parameters: list[Any] = []
        sql = "SELECT * FROM coding_sessions"
        if status is not None:
            status = self._validate_session_status(status)
            sql += " WHERE status=?"
            parameters.append(status)
        sql += " ORDER BY updated_at DESC, id DESC LIMIT ?"
        parameters.append(limit)
        with self._lock:
            rows = self.db.execute(sql, parameters).fetchall()
        return [self._session_value(row) for row in rows]

    def update_session_status(
        self, session_id: str, status: str, *, reason: str | None = None
    ) -> dict[str, Any]:
        status = self._validate_session_status(status)
        now = _utc_now()
        with self._transaction():
            row = self.db.execute(
                "SELECT status FROM coding_sessions WHERE id=?", (session_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown coding session: {session_id}")
            self.db.execute(
                """UPDATE coding_sessions
                   SET status=?, terminal_reason=?, updated_at=? WHERE id=?""",
                (status, reason, now, session_id),
            )
            self._append_event(
                session_id,
                "session.status_changed",
                {"from": row["status"], "to": status, "reason": reason},
            )
        return self.get_session(session_id)

    def add_turn(
        self,
        session_id: str,
        role: str,
        content: Any,
        *,
        agent_name: str = "coordinator",
    ) -> dict[str, Any]:
        if not role.strip():
            raise ValueError("role must not be empty")
        turn_id = uuid4().hex
        now = _utc_now()
        with self._transaction():
            self._require_session(session_id)
            row = self.db.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS next FROM coding_turns WHERE session_id=?",
                (session_id,),
            ).fetchone()
            sequence = int(row["next"])
            self.db.execute(
                """INSERT INTO coding_turns
                (id,session_id,sequence,role,agent_name,content_json,created_at)
                VALUES (?,?,?,?,?,?,?)""",
                (turn_id, session_id, sequence, role, agent_name, _json(content), now),
            )
            self._append_event(
                session_id,
                "turn.added",
                {"turn_id": turn_id, "sequence": sequence, "role": role, "agent_name": agent_name},
            )
        return self.get_turn(turn_id)

    def get_turn(self, turn_id: str) -> dict[str, Any]:
        with self._lock:
            row = self.db.execute("SELECT * FROM coding_turns WHERE id=?", (turn_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown coding turn: {turn_id}")
        value = dict(row)
        value["content"] = json.loads(value.pop("content_json"))
        return value

    def list_turns(self, session_id: str) -> list[dict[str, Any]]:
        self.get_session(session_id)
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM coding_turns WHERE session_id=? ORDER BY sequence", (session_id,)
            ).fetchall()
        values = []
        for row in rows:
            value = dict(row)
            value["content"] = json.loads(value.pop("content_json"))
            values.append(value)
        return values

    def start_tool_call(
        self,
        session_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        turn_id: str | None = None,
        call_id: str | None = None,
    ) -> str:
        if not tool_name.strip():
            raise ValueError("tool_name must not be empty")
        identifier = call_id or uuid4().hex
        now = _utc_now()
        with self._transaction():
            self._require_session(session_id)
            if turn_id is not None:
                row = self.db.execute(
                    "SELECT session_id FROM coding_turns WHERE id=?", (turn_id,)
                ).fetchone()
                if row is None or row["session_id"] != session_id:
                    raise ValueError("turn_id does not belong to this session")
            self.db.execute(
                """INSERT INTO coding_tool_calls
                (id,session_id,turn_id,tool_name,arguments_json,result_json,status,error,started_at,finished_at)
                VALUES (?,?,?,?,?,NULL,'RUNNING',NULL,?,NULL)""",
                (identifier, session_id, turn_id, tool_name, _json(arguments), now),
            )
            self._append_event(
                session_id,
                "tool.started",
                {"call_id": identifier, "turn_id": turn_id, "tool_name": tool_name},
            )
        return identifier

    def finish_tool_call(
        self,
        call_id: str,
        status: str,
        *,
        result: Any = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        status = status.upper()
        if status not in TOOL_CALL_STATUSES - {"PENDING", "RUNNING"}:
            raise ValueError("finished tool call status must be SUCCEEDED, FAILED, or DENIED")
        with self._transaction():
            row = self.db.execute(
                "SELECT session_id,status FROM coding_tool_calls WHERE id=?", (call_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown tool call: {call_id}")
            if row["status"] != "RUNNING":
                raise ValueError(f"tool call is already finished: {row['status']}")
            self.db.execute(
                """UPDATE coding_tool_calls
                SET result_json=?,status=?,error=?,finished_at=? WHERE id=?""",
                (_json(result), status, error, _utc_now(), call_id),
            )
            self._append_event(
                row["session_id"],
                "tool.finished",
                {"call_id": call_id, "status": status, "error": error},
            )
        return self.get_tool_call(call_id)

    def get_tool_call(self, call_id: str) -> dict[str, Any]:
        with self._lock:
            row = self.db.execute(
                "SELECT * FROM coding_tool_calls WHERE id=?", (call_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown tool call: {call_id}")
        return self._tool_call_value(row)

    def list_tool_calls(self, session_id: str) -> list[dict[str, Any]]:
        self.get_session(session_id)
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM coding_tool_calls WHERE session_id=? ORDER BY started_at,id",
                (session_id,),
            ).fetchall()
        return [self._tool_call_value(row) for row in rows]

    def append_event(
        self, session_id: str, event_type: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if not event_type.strip():
            raise ValueError("event_type must not be empty")
        with self._transaction():
            self._require_session(session_id)
            event_id = self._append_event(session_id, event_type, payload or {})
        return self.get_event(event_id)

    def get_event(self, event_id: int) -> dict[str, Any]:
        with self._lock:
            row = self.db.execute("SELECT * FROM coding_events WHERE id=?", (event_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown coding event: {event_id}")
        return self._event_value(row)

    def list_events(
        self, session_id: str, *, after: int = 0, limit: int = 500
    ) -> list[dict[str, Any]]:
        self.get_session(session_id)
        after = max(0, int(after))
        limit = max(1, min(int(limit), 2000))
        with self._lock:
            rows = self.db.execute(
                """SELECT * FROM coding_events
                WHERE session_id=? AND id>? ORDER BY id LIMIT ?""",
                (session_id, after, limit),
            ).fetchall()
        return [self._event_value(row) for row in rows]

    def add_feedback(
        self,
        session_id: str,
        message: str,
        *,
        author: str = "human",
        kind: str = "feedback",
    ) -> dict[str, Any]:
        message = message.strip()
        if not message:
            raise ValueError("feedback message must not be empty")
        kind = kind.lower()
        if kind not in {"feedback", "steering"}:
            raise ValueError("feedback kind must be feedback or steering")
        now = _utc_now()
        with self._transaction():
            self._require_session(session_id)
            cursor = self.db.execute(
                """INSERT INTO coding_feedback
                (session_id,kind,message,author,handled,created_at,handled_at)
                VALUES (?,?,?,?,0,?,NULL)""",
                (session_id, kind, message, author.strip() or "human", now),
            )
            feedback_id = int(cursor.lastrowid)
            self._append_event(
                session_id,
                f"human.{kind}_received",
                {"feedback_id": feedback_id, "author": author.strip() or "human"},
            )
        return self.get_feedback(feedback_id)

    def add_steering(
        self, session_id: str, message: str, *, author: str = "human"
    ) -> dict[str, Any]:
        return self.add_feedback(session_id, message, author=author, kind="steering")

    def get_feedback(self, feedback_id: int) -> dict[str, Any]:
        with self._lock:
            row = self.db.execute(
                "SELECT * FROM coding_feedback WHERE id=?", (feedback_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown coding feedback: {feedback_id}")
        return self._feedback_value(row)

    def list_feedback(
        self, session_id: str, *, pending_only: bool = False
    ) -> list[dict[str, Any]]:
        self.get_session(session_id)
        sql = "SELECT * FROM coding_feedback WHERE session_id=?"
        if pending_only:
            sql += " AND handled=0"
        sql += " ORDER BY id"
        with self._lock:
            rows = self.db.execute(sql, (session_id,)).fetchall()
        return [self._feedback_value(row) for row in rows]

    def mark_feedback_handled(self, feedback_id: int) -> dict[str, Any]:
        now = _utc_now()
        with self._transaction():
            row = self.db.execute(
                "SELECT session_id,handled FROM coding_feedback WHERE id=?", (feedback_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown coding feedback: {feedback_id}")
            if not row["handled"]:
                self.db.execute(
                    "UPDATE coding_feedback SET handled=1,handled_at=? WHERE id=?",
                    (now, feedback_id),
                )
                self._append_event(
                    row["session_id"], "human.feedback_handled", {"feedback_id": feedback_id}
                )
        return self.get_feedback(feedback_id)

    def request_cancel(self, session_id: str, *, reason: str | None = None) -> dict[str, Any]:
        now = _utc_now()
        with self._transaction():
            self._require_session(session_id)
            self.db.execute(
                "UPDATE coding_sessions SET cancel_requested=1,updated_at=? WHERE id=?",
                (now, session_id),
            )
            self._append_event(session_id, "session.cancel_requested", {"reason": reason})
        return self.get_session(session_id)

    def clear_cancel_request(self, session_id: str) -> dict[str, Any]:
        with self._transaction():
            self._require_session(session_id)
            self.db.execute(
                "UPDATE coding_sessions SET cancel_requested=0,updated_at=? WHERE id=?",
                (_utc_now(), session_id),
            )
            self._append_event(session_id, "session.cancel_cleared", {})
        return self.get_session(session_id)

    def _append_event(self, session_id: str, event_type: str, payload: dict[str, Any]) -> int:
        cursor = self.db.execute(
            "INSERT INTO coding_events(session_id,event_type,payload_json,created_at) VALUES (?,?,?,?)",
            (session_id, event_type, _json(payload), _utc_now()),
        )
        return int(cursor.lastrowid)

    def _require_session(self, session_id: str) -> None:
        if self.db.execute("SELECT 1 FROM coding_sessions WHERE id=?", (session_id,)).fetchone() is None:
            raise KeyError(f"unknown coding session: {session_id}")

    @staticmethod
    def _validate_session_status(status: str) -> str:
        value = status.upper()
        if value not in SESSION_STATUSES:
            raise ValueError(f"invalid coding session status: {status}")
        return value

    @staticmethod
    def _session_value(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["metadata"] = json.loads(value.pop("metadata_json"))
        value["cancel_requested"] = bool(value["cancel_requested"])
        return value

    @staticmethod
    def _event_value(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["payload"] = json.loads(value.pop("payload_json"))
        return value

    @staticmethod
    def _tool_call_value(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["arguments"] = json.loads(value.pop("arguments_json"))
        raw_result = value.pop("result_json")
        value["result"] = json.loads(raw_result) if raw_result is not None else None
        return value

    @staticmethod
    def _feedback_value(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["handled"] = bool(value["handled"])
        return value
