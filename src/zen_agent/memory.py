from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from typing import Any


_SCOPES = {"project", "episodic"}
_TERMS = re.compile(r"[a-z0-9_]+")


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    id: int
    scope: str
    session_id: str | None
    content: str
    metadata: dict[str, Any]
    actor: str
    created_at: str
    score: int = 0


@dataclass(frozen=True, slots=True)
class MemoryProposal:
    id: int
    content: str
    rationale: str
    actor: str
    status: str
    created_at: str
    reviewed_by: str | None
    reviewed_at: str | None


class MemoryStore:
    """Persistent generated memory plus explicitly approved curated guidance."""

    def __init__(self, db_path: Path, *, curated_path: Path | None = None):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.curated_path = curated_path or db_path.with_name("project-memory.md")
        self.connection = sqlite3.connect(db_path, timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope TEXT NOT NULL CHECK(scope IN ('project', 'episodic')),
                session_id TEXT,
                content TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                actor TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_memories_scope_session
                ON memories(scope, session_id, id DESC);
            CREATE TABLE IF NOT EXISTS memory_proposals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                rationale TEXT NOT NULL,
                actor TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('PENDING', 'APPROVED', 'REJECTED')),
                created_at TEXT NOT NULL,
                reviewed_by TEXT,
                reviewed_at TEXT
            );
            """
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "MemoryStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def append(
        self,
        scope: str,
        content: str,
        *,
        actor: str,
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        if scope not in _SCOPES:
            raise ValueError(f"invalid memory scope: {scope}")
        if scope == "episodic" and not session_id:
            raise ValueError("episodic memory requires a session_id")
        if not content.strip() or not actor.strip():
            raise ValueError("memory content and actor cannot be empty")
        metadata_json = json.dumps(metadata or {}, sort_keys=True, separators=(",", ":"))
        timestamp = _now()
        with self.connection:
            cursor = self.connection.execute(
                """INSERT INTO memories(scope, session_id, content, metadata_json, actor, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (scope, session_id, content, metadata_json, actor, timestamp),
            )
        return int(cursor.lastrowid)

    def append_episode(
        self,
        session_id: str,
        content: str,
        *,
        actor: str,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        return self.append(
            "episodic", content, actor=actor, session_id=session_id, metadata=metadata
        )

    def query(
        self,
        query: str,
        *,
        scope: str | None = None,
        session_id: str | None = None,
        limit: int = 10,
    ) -> list[MemoryRecord]:
        if scope is not None and scope not in _SCOPES:
            raise ValueError(f"invalid memory scope: {scope}")
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        clauses: list[str] = []
        parameters: list[object] = []
        if scope is not None:
            clauses.append("scope = ?")
            parameters.append(scope)
        if session_id is not None:
            clauses.append("session_id = ?")
            parameters.append(session_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.connection.execute(
            "SELECT * FROM memories" + where + " ORDER BY id DESC LIMIT 2000", parameters
        ).fetchall()
        terms = tuple(dict.fromkeys(_TERMS.findall(query.casefold())))
        records = [_memory_record(row, terms) for row in rows]
        if terms:
            records = [record for record in records if record.score]
            records.sort(key=lambda item: (item.score, item.id), reverse=True)
        return records[:limit]

    def read_curated(self) -> str:
        if not self.curated_path.exists():
            return ""
        return self.curated_path.read_text(encoding="utf-8")

    def curated_sha256(self) -> str:
        return hashlib.sha256(self.read_curated().encode("utf-8")).hexdigest()

    def propose_curated(self, content: str, *, actor: str, rationale: str) -> int:
        if not content.strip() or not actor.strip() or not rationale.strip():
            raise ValueError("proposal content, actor, and rationale cannot be empty")
        with self.connection:
            cursor = self.connection.execute(
                """INSERT INTO memory_proposals(
                       content, rationale, actor, status, created_at, reviewed_by, reviewed_at
                   ) VALUES (?, ?, ?, 'PENDING', ?, NULL, NULL)""",
                (content, rationale, actor, _now()),
            )
        return int(cursor.lastrowid)

    def approve_curated(
        self,
        proposal_id: int,
        *,
        reviewer: str,
        expected_sha256: str | None = None,
    ) -> str:
        if not reviewer.strip():
            raise ValueError("reviewer cannot be empty")
        row = self.connection.execute(
            "SELECT * FROM memory_proposals WHERE id = ?", (proposal_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown memory proposal: {proposal_id}")
        if row["status"] != "PENDING":
            raise ValueError(f"memory proposal is already {row['status'].lower()}")
        current_hash = self.curated_sha256()
        if expected_sha256 is not None and expected_sha256 != current_hash:
            raise RuntimeError("curated memory changed since review")
        content = str(row["content"])
        self._write_curated(content)
        with self.connection:
            self.connection.execute(
                """UPDATE memory_proposals
                   SET status = 'APPROVED', reviewed_by = ?, reviewed_at = ?
                   WHERE id = ? AND status = 'PENDING'""",
                (reviewer, _now(), proposal_id),
            )
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def reject_curated(self, proposal_id: int, *, reviewer: str) -> None:
        if not reviewer.strip():
            raise ValueError("reviewer cannot be empty")
        with self.connection:
            cursor = self.connection.execute(
                """UPDATE memory_proposals
                   SET status = 'REJECTED', reviewed_by = ?, reviewed_at = ?
                   WHERE id = ? AND status = 'PENDING'""",
                (reviewer, _now(), proposal_id),
            )
        if cursor.rowcount != 1:
            raise KeyError(f"pending memory proposal not found: {proposal_id}")

    def list_proposals(self, *, status: str | None = None) -> list[MemoryProposal]:
        if status is not None and status not in {"PENDING", "APPROVED", "REJECTED"}:
            raise ValueError(f"invalid proposal status: {status}")
        if status is None:
            rows = self.connection.execute("SELECT * FROM memory_proposals ORDER BY id DESC").fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM memory_proposals WHERE status = ? ORDER BY id DESC", (status,)
            ).fetchall()
        return [
            MemoryProposal(
                id=int(row["id"]),
                content=str(row["content"]),
                rationale=str(row["rationale"]),
                actor=str(row["actor"]),
                status=str(row["status"]),
                created_at=str(row["created_at"]),
                reviewed_by=row["reviewed_by"],
                reviewed_at=row["reviewed_at"],
            )
            for row in rows
        ]

    def _write_curated(self, content: str) -> None:
        self.curated_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            dir=self.curated_path.parent, prefix=f".{self.curated_path.name}.", text=True
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.curated_path)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise


def _memory_record(row: sqlite3.Row, terms: tuple[str, ...]) -> MemoryRecord:
    lowered = str(row["content"]).casefold()
    score = sum(lowered.count(term) for term in terms)
    return MemoryRecord(
        id=int(row["id"]),
        scope=str(row["scope"]),
        session_id=row["session_id"],
        content=str(row["content"]),
        metadata=json.loads(row["metadata_json"]),
        actor=str(row["actor"]),
        created_at=str(row["created_at"]),
        score=score,
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
