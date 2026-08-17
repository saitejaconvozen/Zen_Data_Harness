"""Durable human-review decisions for golden-conversation candidates.

The ledger deliberately separates immutable history from the mutable item summary:
``review_decisions`` and ``review_candidate_revisions`` are append-only, while a
``review_items`` row is only a projection of the latest state.  This makes human
feedback safe to replay into the factory without losing who decided what.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Iterable, Mapping
from uuid import uuid4


ACTIONS = frozenset({"APPROVE", "EDIT", "REJECT", "REQUEST_REPAIR"})
ITEM_STATES = frozenset(
    {
        "REVIEW_PENDING",
        "APPROVED",
        "REJECTED",
        "REPAIR_REQUESTED",
        "EDITED_PENDING_VERIFICATION",
    }
)

_ACTION_STATE = {
    "APPROVE": "APPROVED",
    "REJECT": "REJECTED",
    "REQUEST_REPAIR": "REPAIR_REQUESTED",
    "EDIT": "EDITED_PENDING_VERIFICATION",
}
_ALLOWED_ACTIONS = {
    "REVIEW_PENDING": ACTIONS,
    "APPROVED": frozenset(),
    "REJECTED": frozenset(),
    "REPAIR_REQUESTED": frozenset(),
    "EDITED_PENDING_VERIFICATION": frozenset(),
}


class ReviewFeedbackError(ValueError):
    """Base class for review-ledger contract violations."""


class InvalidReviewTransition(ReviewFeedbackError):
    """Raised when an action is not valid for the current review state."""


class IdempotencyConflict(ReviewFeedbackError):
    """Raised when an idempotency key is reused with different input."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _identity(value: str, field: str = "reviewer_identity") -> str:
    if not isinstance(value, str) or not value or not value.strip():
        raise ReviewFeedbackError(f"{field} must be an explicit non-empty string")
    if len(value) > 512:
        raise ReviewFeedbackError(f"{field} is too long")
    # Do not case-fold, trim, or otherwise normalize identities. The exact value
    # supplied by the authenticated caller is part of the audit record.
    return value


def _strings(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(v, str) or not v for v in value):
        raise ReviewFeedbackError(f"{field} must be a list of non-empty strings")
    return list(value)


def _feedback(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ReviewFeedbackError("feedback must be an object")
    allowed = {
        "summary",
        "reason_codes",
        "evidence_turn_ids",
        "metric_citations",
        "requested_changes",
    }
    unknown = set(value) - allowed
    if unknown:
        raise ReviewFeedbackError(f"unknown feedback fields: {sorted(unknown)}")
    summary = value.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise ReviewFeedbackError("feedback.summary must be a non-empty string")

    citations = value.get("metric_citations", [])
    if not isinstance(citations, list):
        raise ReviewFeedbackError("feedback.metric_citations must be a list")
    normalized_citations = []
    citation_fields = {"axis_id", "subaxis_id", "variant_id", "turn_id", "verdict"}
    for citation in citations:
        if not isinstance(citation, Mapping) or set(citation) - citation_fields:
            raise ReviewFeedbackError("each metric citation must contain only supported fields")
        for field in ("axis_id", "subaxis_id", "variant_id"):
            if not isinstance(citation.get(field), str) or not citation[field]:
                raise ReviewFeedbackError(f"metric citation {field} is required")
        if "turn_id" in citation and (
            not isinstance(citation["turn_id"], str) or not citation["turn_id"]
        ):
            raise ReviewFeedbackError("metric citation turn_id must be a non-empty string")
        if "verdict" in citation and citation["verdict"] not in {"PASS", "FAIL", "NOT_ASSESSED"}:
            raise ReviewFeedbackError("metric citation verdict is invalid")
        normalized_citations.append(dict(citation))

    changes = value.get("requested_changes", [])
    if not isinstance(changes, list):
        raise ReviewFeedbackError("feedback.requested_changes must be a list")
    normalized_changes = []
    for change in changes:
        if not isinstance(change, Mapping) or set(change) - {"turn_id", "instruction"}:
            raise ReviewFeedbackError("each requested change must contain turn_id/instruction only")
        instruction = change.get("instruction")
        if not isinstance(instruction, str) or not instruction.strip():
            raise ReviewFeedbackError("requested change instruction is required")
        turn_id = change.get("turn_id")
        if turn_id is not None and (not isinstance(turn_id, str) or not turn_id):
            raise ReviewFeedbackError("requested change turn_id must be a non-empty string")
        normalized_changes.append(dict(change))

    return {
        "summary": summary,
        "reason_codes": _strings(value.get("reason_codes"), "feedback.reason_codes"),
        "evidence_turn_ids": _strings(
            value.get("evidence_turn_ids"), "feedback.evidence_turn_ids"
        ),
        "metric_citations": normalized_citations,
        "requested_changes": normalized_changes,
    }


def _assistant_edits(
    value: Mapping[str, str] | Iterable[Mapping[str, str]] | None,
    *,
    allowed_turn_ids: set[str],
) -> list[dict[str, str]]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        rows = [{"turn_id": key, "text": text} for key, text in value.items()]
    elif isinstance(value, (str, bytes)):
        raise ReviewFeedbackError("assistant_edits must be a mapping or list")
    else:
        rows = list(value)
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        # A role or a user-turn payload is never accepted by this API.
        if not isinstance(row, Mapping) or set(row) != {"turn_id", "text"}:
            raise ReviewFeedbackError("assistant edits must contain exactly turn_id and text")
        turn_id, text = row["turn_id"], row["text"]
        if not isinstance(turn_id, str) or turn_id not in allowed_turn_ids:
            raise ReviewFeedbackError(f"turn {turn_id!r} is not an assistant turn")
        if turn_id in seen:
            raise ReviewFeedbackError(f"duplicate assistant edit for {turn_id}")
        if not isinstance(text, str) or not text:
            raise ReviewFeedbackError("assistant edit text must be a non-empty string")
        seen.add(turn_id)
        normalized.append({"turn_id": turn_id, "text": text})
    return sorted(normalized, key=lambda row: row["turn_id"])


class ReviewFeedbackStore:
    """Transactional, append-only ledger for human review and repair feedback."""

    def __init__(self, path: Path):
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS review_items (
              id TEXT PRIMARY KEY,
              run_id TEXT NOT NULL,
              conversation_id TEXT NOT NULL,
              source_content_sha256 TEXT NOT NULL,
              assistant_turn_ids_json TEXT NOT NULL,
              state TEXT NOT NULL,
              current_candidate_revision INTEGER NOT NULL,
              current_decision_revision INTEGER NOT NULL DEFAULT 0,
              created_at REAL NOT NULL,
              updated_at REAL NOT NULL,
              UNIQUE(run_id, conversation_id)
            );
            CREATE INDEX IF NOT EXISTS idx_review_items_state
              ON review_items(run_id, state, updated_at);

            CREATE TABLE IF NOT EXISTS review_candidate_revisions (
              id TEXT PRIMARY KEY,
              item_id TEXT NOT NULL REFERENCES review_items(id),
              revision INTEGER NOT NULL,
              candidate_ref TEXT NOT NULL,
              source_content_sha256 TEXT NOT NULL,
              submitted_by TEXT NOT NULL,
              submission_idempotency_key TEXT,
              request_sha256 TEXT NOT NULL,
              created_at REAL NOT NULL,
              UNIQUE(item_id, revision),
              UNIQUE(item_id, submission_idempotency_key)
            );

            CREATE TABLE IF NOT EXISTS review_decisions (
              id TEXT PRIMARY KEY,
              item_id TEXT NOT NULL REFERENCES review_items(id),
              revision INTEGER NOT NULL,
              candidate_revision INTEGER NOT NULL,
              action TEXT NOT NULL,
              reviewer_identity TEXT NOT NULL,
              feedback_json TEXT NOT NULL,
              assistant_edits_json TEXT NOT NULL,
              idempotency_key TEXT NOT NULL,
              request_sha256 TEXT NOT NULL,
              created_at REAL NOT NULL,
              UNIQUE(item_id, revision),
              UNIQUE(item_id, idempotency_key)
            );

            CREATE TABLE IF NOT EXISTS review_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              item_id TEXT NOT NULL REFERENCES review_items(id),
              event_type TEXT NOT NULL,
              actor_identity TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              created_at REAL NOT NULL
            );

            CREATE TRIGGER IF NOT EXISTS immutable_review_decisions_update
              BEFORE UPDATE ON review_decisions BEGIN
                SELECT RAISE(ABORT, 'review decisions are immutable');
              END;
            CREATE TRIGGER IF NOT EXISTS immutable_review_decisions_delete
              BEFORE DELETE ON review_decisions BEGIN
                SELECT RAISE(ABORT, 'review decisions are immutable');
              END;
            CREATE TRIGGER IF NOT EXISTS immutable_candidate_revisions_update
              BEFORE UPDATE ON review_candidate_revisions BEGIN
                SELECT RAISE(ABORT, 'candidate revisions are immutable');
              END;
            CREATE TRIGGER IF NOT EXISTS immutable_candidate_revisions_delete
              BEFORE DELETE ON review_candidate_revisions BEGIN
                SELECT RAISE(ABORT, 'candidate revisions are immutable');
              END;
            CREATE TRIGGER IF NOT EXISTS immutable_review_events_update
              BEFORE UPDATE ON review_events BEGIN
                SELECT RAISE(ABORT, 'review events are immutable');
              END;
            CREATE TRIGGER IF NOT EXISTS immutable_review_events_delete
              BEFORE DELETE ON review_events BEGIN
                SELECT RAISE(ABORT, 'review events are immutable');
              END;
            """
        )
        for database_file in (self.path, self.path.with_name(self.path.name + "-wal"), self.path.with_name(self.path.name + "-shm")):
            if database_file.exists():
                database_file.chmod(0o600)

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "ReviewFeedbackStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def create_item(
        self,
        *,
        run_id: str,
        conversation_id: str,
        source_content_sha256: str,
        candidate_ref: str,
        assistant_turn_ids: Iterable[str],
        submitted_by: str = "zen-harness",
    ) -> dict[str, Any]:
        for name, value in {
            "run_id": run_id,
            "conversation_id": conversation_id,
            "source_content_sha256": source_content_sha256,
            "candidate_ref": candidate_ref,
        }.items():
            if not isinstance(value, str) or not value:
                raise ReviewFeedbackError(f"{name} must be a non-empty string")
        actor = _identity(submitted_by, "submitted_by")
        turns = sorted(set(_strings(list(assistant_turn_ids), "assistant_turn_ids")))
        if not turns:
            raise ReviewFeedbackError("assistant_turn_ids cannot be empty")
        now, item_id = time.time(), uuid4().hex
        request = {
            "candidate_ref": candidate_ref,
            "source_content_sha256": source_content_sha256,
            "submitted_by": actor,
        }
        self.db.execute("BEGIN IMMEDIATE")
        try:
            existing = self.db.execute(
                "SELECT * FROM review_items WHERE run_id=? AND conversation_id=?",
                (run_id, conversation_id),
            ).fetchone()
            if existing is not None:
                value = self._item(existing)
                initial = self.db.execute(
                    "SELECT candidate_ref FROM review_candidate_revisions WHERE item_id=? AND revision=1",
                    (existing["id"],),
                ).fetchone()
                if (
                    existing["source_content_sha256"] != source_content_sha256
                    or json.loads(existing["assistant_turn_ids_json"]) != turns
                    or initial["candidate_ref"] != candidate_ref
                ):
                    raise IdempotencyConflict("review item identity already has different content")
                self.db.execute("COMMIT")
                return value
            self.db.execute(
                """INSERT INTO review_items
                (id,run_id,conversation_id,source_content_sha256,assistant_turn_ids_json,
                 state,current_candidate_revision,current_decision_revision,created_at,updated_at)
                VALUES (?,?,?,?,?,'REVIEW_PENDING',1,0,?,?)""",
                (item_id, run_id, conversation_id, source_content_sha256, _canonical_json(turns), now, now),
            )
            self.db.execute(
                """INSERT INTO review_candidate_revisions
                (id,item_id,revision,candidate_ref,source_content_sha256,submitted_by,
                 submission_idempotency_key,request_sha256,created_at)
                VALUES (?,?,1,?,?,?,?,?,?)""",
                (
                    uuid4().hex,
                    item_id,
                    candidate_ref,
                    source_content_sha256,
                    actor,
                    None,
                    _fingerprint(request),
                    now,
                ),
            )
            self._event(item_id, "review.item_created", actor, {"candidate_revision": 1})
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        return self.get_item(item_id)

    def record_decision(
        self,
        item_id: str,
        *,
        action: str,
        reviewer_identity: str,
        idempotency_key: str,
        feedback: Mapping[str, Any],
        assistant_edits: Mapping[str, str] | Iterable[Mapping[str, str]] | None = None,
    ) -> dict[str, Any]:
        if action not in ACTIONS:
            raise ReviewFeedbackError(f"unsupported review action: {action}")
        reviewer = _identity(reviewer_identity)
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise ReviewFeedbackError("idempotency_key must be a non-empty string")
        normalized_feedback = _feedback(feedback)

        self.db.execute("BEGIN IMMEDIATE")
        try:
            item = self.db.execute("SELECT * FROM review_items WHERE id=?", (item_id,)).fetchone()
            if item is None:
                raise KeyError(item_id)
            edits = _assistant_edits(
                assistant_edits,
                allowed_turn_ids=set(json.loads(item["assistant_turn_ids_json"])),
            )
            if action == "EDIT" and not edits:
                raise ReviewFeedbackError("EDIT requires at least one assistant edit")
            if action != "EDIT" and edits:
                raise ReviewFeedbackError("assistant_edits are accepted only for EDIT")
            request = {
                "action": action,
                "reviewer_identity": reviewer,
                "feedback": normalized_feedback,
                "assistant_edits": edits,
            }
            request_sha = _fingerprint(request)
            existing = self.db.execute(
                "SELECT * FROM review_decisions WHERE item_id=? AND idempotency_key=?",
                (item_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                if existing["request_sha256"] != request_sha:
                    raise IdempotencyConflict("idempotency key was reused with different decision input")
                value = self._decision(existing)
                self.db.execute("COMMIT")
                return value
            if action not in _ALLOWED_ACTIONS[item["state"]]:
                raise InvalidReviewTransition(f"cannot {action} from {item['state']}")

            revision = int(item["current_decision_revision"]) + 1
            decision_id, now = "hfd_" + hashlib.sha256(uuid4().bytes).hexdigest(), time.time()
            self.db.execute(
                """INSERT INTO review_decisions
                (id,item_id,revision,candidate_revision,action,reviewer_identity,feedback_json,
                 assistant_edits_json,idempotency_key,request_sha256,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    decision_id,
                    item_id,
                    revision,
                    item["current_candidate_revision"],
                    action,
                    reviewer,
                    _canonical_json(normalized_feedback),
                    _canonical_json(edits),
                    idempotency_key,
                    request_sha,
                    now,
                ),
            )
            state = _ACTION_STATE[action]
            self.db.execute(
                """UPDATE review_items SET state=?,current_decision_revision=?,updated_at=?
                   WHERE id=?""",
                (state, revision, now, item_id),
            )
            self._event(
                item_id,
                "review.decision_recorded",
                reviewer,
                {
                    "action": action,
                    "decision_id": decision_id,
                    "decision_revision": revision,
                    "candidate_revision": item["current_candidate_revision"],
                    "new_state": state,
                },
            )
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        return self.get_decision(decision_id)

    def submit_candidate_revision(
        self,
        item_id: str,
        *,
        candidate_ref: str,
        source_content_sha256: str,
        submitted_by: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Attach a repaired/verified candidate and reopen it for human review."""
        if not isinstance(candidate_ref, str) or not candidate_ref:
            raise ReviewFeedbackError("candidate_ref must be a non-empty string")
        actor = _identity(submitted_by, "submitted_by")
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise ReviewFeedbackError("idempotency_key must be a non-empty string")
        request = {
            "candidate_ref": candidate_ref,
            "source_content_sha256": source_content_sha256,
            "submitted_by": actor,
        }
        request_sha = _fingerprint(request)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            item = self.db.execute("SELECT * FROM review_items WHERE id=?", (item_id,)).fetchone()
            if item is None:
                raise KeyError(item_id)
            existing = self.db.execute(
                """SELECT * FROM review_candidate_revisions
                   WHERE item_id=? AND submission_idempotency_key=?""",
                (item_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                if existing["request_sha256"] != request_sha:
                    raise IdempotencyConflict("idempotency key was reused with different candidate input")
                self.db.execute("COMMIT")
                return self._candidate(existing)
            if item["state"] not in {"REPAIR_REQUESTED", "EDITED_PENDING_VERIFICATION"}:
                raise InvalidReviewTransition(f"cannot submit candidate from {item['state']}")
            if source_content_sha256 != item["source_content_sha256"]:
                raise ReviewFeedbackError("candidate revision changed the immutable source content hash")
            revision, now = int(item["current_candidate_revision"]) + 1, time.time()
            candidate_id = uuid4().hex
            self.db.execute(
                """INSERT INTO review_candidate_revisions
                (id,item_id,revision,candidate_ref,source_content_sha256,submitted_by,
                 submission_idempotency_key,request_sha256,created_at)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    candidate_id,
                    item_id,
                    revision,
                    candidate_ref,
                    source_content_sha256,
                    actor,
                    idempotency_key,
                    request_sha,
                    now,
                ),
            )
            self.db.execute(
                """UPDATE review_items SET state='REVIEW_PENDING',
                   current_candidate_revision=?,updated_at=? WHERE id=?""",
                (revision, now, item_id),
            )
            self._event(
                item_id,
                "review.candidate_revised",
                actor,
                {"candidate_revision": revision, "candidate_ref": candidate_ref},
            )
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        return self.get_candidate_revision(item_id, revision)

    def get_item(self, item_id: str, *, include_history: bool = True) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM review_items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise KeyError(item_id)
        value = self._item(row)
        if include_history:
            value["candidate_revisions"] = self.list_candidate_revisions(item_id)
            value["decisions"] = self.list_decisions(item_id)
            value["events"] = self.list_events(item_id)
        return value

    def get_item_by_conversation(
        self, run_id: str, conversation_id: str, *, include_history: bool = True
    ) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT id FROM review_items WHERE run_id=? AND conversation_id=?",
            (run_id, conversation_id),
        ).fetchone()
        if row is None:
            raise KeyError((run_id, conversation_id))
        return self.get_item(row["id"], include_history=include_history)

    def list_items(
        self,
        *,
        run_id: str | None = None,
        state: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        if state is not None and state not in ITEM_STATES:
            raise ReviewFeedbackError("invalid review item state")
        if limit < 1 or limit > 10_000 or offset < 0:
            raise ReviewFeedbackError("invalid pagination")
        clauses, values = [], []
        if run_id is not None:
            clauses.append("run_id=?")
            values.append(run_id)
        if state is not None:
            clauses.append("state=?")
            values.append(state)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.db.execute(
            f"SELECT * FROM review_items{where} ORDER BY updated_at DESC,id LIMIT ? OFFSET ?",
            (*values, limit, offset),
        ).fetchall()
        return [self._item(row) for row in rows]

    def get_decision(self, decision_id: str) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM review_decisions WHERE id=?", (decision_id,)).fetchone()
        if row is None:
            raise KeyError(decision_id)
        return self._decision(row)

    def list_decisions(self, item_id: str) -> list[dict[str, Any]]:
        return [
            self._decision(row)
            for row in self.db.execute(
                "SELECT * FROM review_decisions WHERE item_id=? ORDER BY revision", (item_id,)
            ).fetchall()
        ]

    def get_candidate_revision(self, item_id: str, revision: int) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT * FROM review_candidate_revisions WHERE item_id=? AND revision=?",
            (item_id, revision),
        ).fetchone()
        if row is None:
            raise KeyError((item_id, revision))
        return self._candidate(row)

    def list_candidate_revisions(self, item_id: str) -> list[dict[str, Any]]:
        return [
            self._candidate(row)
            for row in self.db.execute(
                "SELECT * FROM review_candidate_revisions WHERE item_id=? ORDER BY revision",
                (item_id,),
            ).fetchall()
        ]

    def list_events(self, item_id: str) -> list[dict[str, Any]]:
        values = []
        for row in self.db.execute(
            "SELECT * FROM review_events WHERE item_id=? ORDER BY id", (item_id,)
        ).fetchall():
            value = dict(row)
            value["payload"] = json.loads(value.pop("payload_json"))
            values.append(value)
        return values

    def pending_repair_requests(self, *, run_id: str | None = None) -> list[dict[str, Any]]:
        """Return structured repair and edited-candidate work for the interpreter."""
        items = [
            *self.list_items(
                run_id=run_id, state="REPAIR_REQUESTED", limit=10_000
            ),
            *self.list_items(
                run_id=run_id,
                state="EDITED_PENDING_VERIFICATION",
                limit=10_000,
            ),
        ]
        for item in items:
            item["repair_decision"] = self.list_decisions(item["id"])[-1]
        return items

    def _event(
        self, item_id: str, event_type: str, actor_identity: str, payload: dict[str, Any]
    ) -> None:
        self.db.execute(
            """INSERT INTO review_events
               (item_id,event_type,actor_identity,payload_json,created_at) VALUES (?,?,?,?,?)""",
            (item_id, event_type, actor_identity, _canonical_json(payload), time.time()),
        )

    @staticmethod
    def _item(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["assistant_turn_ids"] = json.loads(value.pop("assistant_turn_ids_json"))
        return value

    @staticmethod
    def _decision(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["feedback"] = json.loads(value.pop("feedback_json"))
        value["assistant_edits"] = json.loads(value.pop("assistant_edits_json"))
        return value

    @staticmethod
    def _candidate(row: sqlite3.Row) -> dict[str, Any]:
        return dict(row)
