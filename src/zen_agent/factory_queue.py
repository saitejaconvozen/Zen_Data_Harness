from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Iterable
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class ClaimedWork:
    id: str
    job_key: str
    stage: str
    payload: dict[str, Any]
    attempt: int
    lease_token: str
    lease_expires_at: float


class LocalFactoryQueue:
    """Durable single-host queue for development and forward tests.

    Production deployments must use the same claim/lease contract on PostgreSQL or
    another transactional queue. SQLite is deliberately not presented as a 5K
    multi-host backend.
    """

    def __init__(self, path: Path):
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS factory_work (
              id TEXT PRIMARY KEY,
              run_id TEXT NOT NULL,
              job_key TEXT NOT NULL,
              stage TEXT NOT NULL,
              priority INTEGER NOT NULL DEFAULT 0,
              status TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              attempt INTEGER NOT NULL DEFAULT 0,
              max_attempts INTEGER NOT NULL,
              available_at REAL NOT NULL,
              lease_owner TEXT,
              lease_token TEXT,
              lease_expires_at REAL,
              output_ref TEXT,
              error TEXT,
              created_at REAL NOT NULL,
              updated_at REAL NOT NULL,
              UNIQUE(run_id, job_key, stage)
            );
            CREATE INDEX IF NOT EXISTS idx_factory_claim
              ON factory_work(run_id, stage, status, available_at, priority, created_at);
            """
        )

    def close(self) -> None:
        self.db.close()

    def enqueue(
        self,
        run_id: str,
        job_key: str,
        stage: str,
        payload: dict[str, Any],
        *,
        max_attempts: int = 3,
        priority: int = 0,
        available_at: float | None = None,
    ) -> bool:
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        now = time.time()
        cursor = self.db.execute(
            """INSERT OR IGNORE INTO factory_work
            (id, run_id, job_key, stage, priority, status, payload_json, attempt,
             max_attempts, available_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'READY', ?, 0, ?, ?, ?, ?)""",
            (
                uuid4().hex,
                run_id,
                job_key,
                stage,
                priority,
                json.dumps(payload, sort_keys=True),
                max_attempts,
                now if available_at is None else available_at,
                now,
                now,
            ),
        )
        return cursor.rowcount == 1

    def enqueue_many(self, rows: Iterable[dict[str, Any]]) -> int:
        return sum(self.enqueue(**row) for row in rows)

    def claim(
        self,
        run_id: str,
        worker_id: str,
        stages: tuple[str, ...],
        *,
        lease_seconds: int = 900,
    ) -> ClaimedWork | None:
        if not stages:
            raise ValueError("worker must declare at least one stage")
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        now = time.time()
        placeholders = ",".join("?" for _ in stages)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute(
                """UPDATE factory_work
                SET status=CASE WHEN attempt < max_attempts THEN 'READY' ELSE 'DEAD' END,
                    lease_owner=NULL, lease_token=NULL, lease_expires_at=NULL,
                    error=CASE WHEN attempt < max_attempts THEN error ELSE 'lease expired after final attempt' END,
                    updated_at=?
                WHERE run_id=? AND status='LEASED' AND lease_expires_at <= ?""",
                (now, run_id, now),
            )
            row = self.db.execute(
                f"""SELECT * FROM factory_work
                WHERE run_id=? AND stage IN ({placeholders}) AND status='READY'
                  AND available_at <= ? AND attempt < max_attempts
                ORDER BY priority DESC, created_at ASC LIMIT 1""",
                (run_id, *stages, now),
            ).fetchone()
            if row is None:
                self.db.execute("COMMIT")
                return None
            token = uuid4().hex
            expires = now + lease_seconds
            cursor = self.db.execute(
                """UPDATE factory_work SET status='LEASED', attempt=attempt+1,
                   lease_owner=?, lease_token=?, lease_expires_at=?, updated_at=?
                   WHERE id=? AND status='READY'""",
                (worker_id, token, expires, now, row["id"]),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("factory claim lost its compare-and-set")
            claimed = self.db.execute("SELECT * FROM factory_work WHERE id=?", (row["id"],)).fetchone()
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        return ClaimedWork(
            id=claimed["id"],
            job_key=claimed["job_key"],
            stage=claimed["stage"],
            payload=json.loads(claimed["payload_json"]),
            attempt=claimed["attempt"],
            lease_token=claimed["lease_token"],
            lease_expires_at=claimed["lease_expires_at"],
        )

    def heartbeat(self, work_id: str, lease_token: str, *, lease_seconds: int = 900) -> bool:
        now = time.time()
        cursor = self.db.execute(
            """UPDATE factory_work SET lease_expires_at=?, updated_at=?
            WHERE id=? AND status='LEASED' AND lease_token=? AND lease_expires_at>?""",
            (now + lease_seconds, now, work_id, lease_token, now),
        )
        return cursor.rowcount == 1

    def complete(self, work_id: str, lease_token: str, output_ref: str) -> None:
        self._finish(work_id, lease_token, "SUCCEEDED", output_ref=output_ref)

    def fail(self, work_id: str, lease_token: str, error: str, *, retry_delay: float = 0) -> str:
        row = self.db.execute(
            "SELECT attempt, max_attempts FROM factory_work WHERE id=? AND status='LEASED' AND lease_token=?",
            (work_id, lease_token),
        ).fetchone()
        if row is None:
            raise ValueError("stale or unknown factory lease")
        status = "READY" if row["attempt"] < row["max_attempts"] else "DEAD"
        now = time.time()
        cursor = self.db.execute(
            """UPDATE factory_work SET status=?, error=?, available_at=?, lease_owner=NULL,
               lease_token=NULL, lease_expires_at=NULL, updated_at=?
               WHERE id=? AND status='LEASED' AND lease_token=?""",
            (status, error[-4000:], now + retry_delay, now, work_id, lease_token),
        )
        if cursor.rowcount != 1:
            raise ValueError("stale factory lease")
        return status

    def _finish(self, work_id: str, lease_token: str, status: str, *, output_ref: str) -> None:
        cursor = self.db.execute(
            """UPDATE factory_work SET status=?, output_ref=?, lease_owner=NULL,
               lease_token=NULL, lease_expires_at=NULL, updated_at=?
               WHERE id=? AND status='LEASED' AND lease_token=?""",
            (status, output_ref, time.time(), work_id, lease_token),
        )
        if cursor.rowcount != 1:
            raise ValueError("stale or unknown factory lease")

    def cancel_ready(self, run_id: str, job_key: str, stage: str, reason: str) -> bool:
        """Retire an unclaimed work item while preserving its audit record."""
        cursor = self.db.execute(
            """UPDATE factory_work SET status='DEAD', error=?, updated_at=?
            WHERE run_id=? AND job_key=? AND stage=? AND status='READY'""",
            (reason[-4000:], time.time(), run_id, job_key, stage),
        )
        return cursor.rowcount == 1

    def terminal_status_counts(self, run_id: str) -> dict[str, int]:
        """Committed terminal statuses for a run, e.g. VERIFIED_CANDIDATE -> n."""

        rows = self.db.execute(
            """SELECT json_extract(payload_json,'$.inputs.terminal_status') AS status,
                      COUNT(*) AS n
               FROM factory_work
               WHERE run_id=? AND stage='terminal' AND status='SUCCEEDED'
               GROUP BY status""",
            (run_id,),
        ).fetchall()
        return {row["status"]: row["n"] for row in rows if row["status"]}

    def counts_by_stage(self, run_id: str) -> dict[str, dict[str, int]]:
        rows = self.db.execute(
            "SELECT stage,status,COUNT(*) AS count FROM factory_work WHERE run_id=? GROUP BY stage,status",
            (run_id,),
        ).fetchall()
        result: dict[str, dict[str, int]] = {}
        for row in rows:
            result.setdefault(row["stage"], {})[row["status"]] = row["count"]
        return result

    def counts(self, run_id: str) -> dict[str, int]:
        rows = self.db.execute(
            "SELECT status, COUNT(*) AS count FROM factory_work WHERE run_id=? GROUP BY status",
            (run_id,),
        ).fetchall()
        return {row["status"]: row["count"] for row in rows}

    def item(self, run_id: str, job_key: str, stage: str) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT * FROM factory_work WHERE run_id=? AND job_key=? AND stage=?",
            (run_id, job_key, stage),
        ).fetchone()
        if row is None:
            raise KeyError((run_id, job_key, stage))
        value = dict(row)
        value["payload"] = json.loads(value.pop("payload_json"))
        return value

    def items_for_run(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM factory_work WHERE run_id=? ORDER BY created_at, id",
            (run_id,),
        ).fetchall()
        values = []
        for row in rows:
            value = dict(row)
            value["payload"] = json.loads(value.pop("payload_json"))
            values.append(value)
        return values
