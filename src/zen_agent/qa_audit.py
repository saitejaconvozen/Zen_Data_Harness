"""Sampling QA audit over conversations the harness marked as candidates.

The harness passing its own verification is not evidence the data is good: a
substantive answer was once replaced with "could you please repeat?", labelled
PRESERVED, and passed by an independent verifier with zero findings. A human
found it on first inspection.

So every batch of newly-approved conversations is sampled and re-checked here,
outside the models. These checks produce a triage list for a person to read —
they are deliberately not a gate, because the judgment calls that matter most
are exactly the ones code cannot make.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time

from .dialogue_act import (
    LEADING_TAG_RE,
    coherence_violation,
    is_clarification_request,
    is_question,
)


CANDIDATE_STATUSES = ("VERIFIED_CANDIDATE", "PARTIAL_CANDIDATE")
DEFAULT_BATCH = 50
DEFAULT_RATE = 0.20

_NUMBER_RE = re.compile(r"\b\d[\d,.]*\b")
_PROPER_RE = re.compile(r"\b[A-Z][a-zA-Z]{2,}\b")
_TAG_RE = re.compile(r"<\|[A-Z_]+\|>|\b(PATIENCE|WAITING)\s+[\d.]+\b")


def _clean(text: str) -> str:
    return _TAG_RE.sub(" ", text or "")


def _strip_tag(text: str) -> str:
    return LEADING_TAG_RE.sub("", text or "").strip()


@dataclass
class Finding:
    conversation: str
    turn_id: str
    kind: str
    detail: str
    severity: str = "REVIEW"

    def as_dict(self) -> dict:
        return {
            "conversation": self.conversation,
            "turn_id": self.turn_id,
            "kind": self.kind,
            "detail": self.detail,
            "severity": self.severity,
        }


@dataclass
class ConversationAudit:
    source_id: str
    number: int
    status: str
    assistant_turns: int
    replaced: int
    findings: list[Finding] = field(default_factory=list)
    judge_verdict: str | None = None
    judge_summary: str | None = None

    @property
    def clean(self) -> bool:
        return not self.findings


# A capital at the start of a sentence is grammar, not an entity. Without this
# the check drowns in "Hello", "Would", "Are" and the real fabrications are lost.
_SENTENCE_START_RE = re.compile(r"(?:^|[.?!]\s+|\n\s*)([A-Z][a-zA-Z]{2,})")

# Capitalised words that are grammar or protocol, never entities.
_NOT_ENTITIES = frozenset({
    "PATIENCE", "WAITING", "ENDCALL", "TRANSFER", "Hmm", "Ahh", "Okay", "Oh",
    "Yes", "No", "Sure", "Sorry", "Hello", "Hi", "Namaste", "Thanks", "Thank",
    "Right", "Well", "Please", "Let", "May", "Can", "Could", "Would", "Will",
    "Shall", "Are", "Is", "Do", "Does", "Did", "What", "When", "Where", "Who",
    "How", "Why", "That", "This", "There", "Your", "You", "We", "It", "If",
    "And", "But", "For", "Not", "Just", "Also", "Then", "Now", "Great",
    "Perfect", "Alright", "Understood", "Confirm", "Apologies", "Actually",
})


def _fabricated_tokens(
    source: str, golden: str, system_prompt: str, conversation_text: str = ""
) -> list[str]:
    """Specifics in the replacement that appear nowhere else in the call.

    A customer name arrives in the session metadata on the first turn, not in
    the assistant turn being corrected, so the whole conversation counts as
    known context. Only genuinely new specifics are worth a human read.
    """

    body = _clean(golden)
    known = "\n".join((_clean(source), system_prompt or "", conversation_text or ""))
    added = []

    # Numbers carry the most risk: prices, dates, quantities, phone digits.
    for token in set(_NUMBER_RE.findall(body)):
        if token not in known:
            added.append(token)

    sentence_initial = set(_SENTENCE_START_RE.findall(body))
    for token in set(_PROPER_RE.findall(body)):
        if token in sentence_initial or token in _NOT_ENTITIES or token in known:
            continue
        added.append(token)
    return sorted(added)


def audit_conversation(conversation: dict, system_prompt: str = "") -> ConversationAudit:
    turns = conversation.get("turns") or []
    assistant = [t for t in turns if t.get("role") == "assistant"]
    # Everything actually said in the call, so metadata-sourced names and
    # figures are not mistaken for fabrication.
    conversation_text = "\n".join(
        str(t.get("text") or t.get("source_text") or "") for t in turns
    )
    audit = ConversationAudit(
        source_id=conversation.get("source_id", "?"),
        number=conversation.get("number", 0),
        status=(conversation.get("terminal") or {}).get("status", "?"),
        assistant_turns=len(assistant),
        replaced=sum(1 for t in assistant if t.get("action") == "REPLACE"),
    )
    add = audit.findings.append

    for index, turn in enumerate(turns):
        if turn.get("role") != "assistant":
            continue
        action = turn.get("action")
        source = turn.get("source_text") or ""
        golden = turn.get("golden_text") or ""
        tid = turn.get("turn_id", "?")

        # The review surfaces the tag-applied text, so a kept turn whose source
        # lacked its mandatory language tag always differs by that prefix. Only
        # a difference in the body is a real breach of the KEEP invariant.
        if action == "KEEP" and golden:
            model_text = turn.get("golden_text_model") or golden
            if _strip_tag(model_text) != _strip_tag(source):
                add(Finding(audit.source_id, tid, "keep-not-identical",
                            "a kept turn differs from the source", "SERIOUS"))
        if action != "REPLACE":
            continue

        following = next(
            (t for t in turns[index + 1:] if t.get("role") == "user"), None
        )
        next_text = (following or {}).get("text")

        reason = coherence_violation(source, golden, next_text)
        if reason and turn.get("downstream_coherence") == "PRESERVED":
            add(Finding(audit.source_id, tid, "false-preserved", reason, "SERIOUS"))

        # Turning an answer into "please repeat" is usually correct: these system
        # prompts require asking rather than guessing from unintelligible audio.
        # It is only serious when the recorded reply proves the original answer
        # was the one the caller responded to — which `reason` establishes.
        if (
            is_question(source)
            and is_clarification_request(golden)
            and not is_clarification_request(source)
        ):
            add(Finding(
                audit.source_id, tid, "answer-to-clarification",
                "a substantive answer became a request to repeat",
                "SERIOUS" if reason else "REVIEW",
            ))

        if turn.get("source_quality") in {"PERFECT", "MINOR_GAP"}:
            add(Finding(audit.source_id, tid, "replaced-without-defect",
                        f"replaced on {turn.get('source_quality')}", "SERIOUS"))

        fabricated = _fabricated_tokens(source, golden, system_prompt, conversation_text)
        if fabricated:
            add(Finding(audit.source_id, tid, "possible-fabrication",
                        "specifics not in the source or prompt: "
                        + ", ".join(fabricated[:6])))

        body_source, body_golden = _clean(source).split(), _clean(golden).split()
        if len(body_source) >= 25 and len(body_golden) < len(body_source) * 0.4:
            add(Finding(audit.source_id, tid, "information-loss",
                        f"{len(body_source)} words became {len(body_golden)}"))

    for turn in turns:
        if turn.get("role") == "user" and turn.get("source_preserved") is False:
            add(Finding(audit.source_id, turn.get("turn_id", "?"),
                        "user-turn-modified", "a user turn was not preserved", "CRITICAL"))
    return audit


class AuditLedger:
    """Durable record of what has already been sampled, so batches advance."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS qa_audits (
                 run_id TEXT NOT NULL, source_id TEXT NOT NULL, batch INTEGER NOT NULL,
                 status TEXT NOT NULL, findings_json TEXT NOT NULL, audited_at REAL NOT NULL,
                 PRIMARY KEY (run_id, source_id))"""
        )
        # Added after the first sweeps; older ledgers upgrade in place.
        for column, decl in (("judge_verdict", "TEXT"), ("judge_summary", "TEXT")):
            try:
                self.db.execute(f"ALTER TABLE qa_audits ADD COLUMN {column} {decl}")
            except sqlite3.OperationalError:
                pass
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def audited(self, run_id: str) -> set[str]:
        return {
            row["source_id"]
            for row in self.db.execute(
                "SELECT source_id FROM qa_audits WHERE run_id=?", (run_id,)
            )
        }

    def next_batch(self, run_id: str) -> int:
        row = self.db.execute(
            "SELECT COALESCE(MAX(batch), 0) + 1 AS n FROM qa_audits WHERE run_id=?",
            (run_id,),
        ).fetchone()
        return int(row["n"])

    def record(self, run_id: str, batch: int, audits: list[ConversationAudit]) -> None:
        now = time.time()
        self.db.executemany(
            """INSERT OR REPLACE INTO qa_audits
               (run_id, source_id, batch, status, findings_json, audited_at,
                judge_verdict, judge_summary)
               VALUES (?,?,?,?,?,?,?,?)""",
            [
                (run_id, a.source_id, batch, a.status,
                 json.dumps([f.as_dict() for f in a.findings]), now,
                 a.judge_verdict, a.judge_summary)
                for a in audits
            ],
        )
        self.db.commit()

    def history(self, run_id: str) -> list[dict]:
        return [dict(row) for row in self.db.execute(
            """SELECT batch, COUNT(*) AS sampled,
                      SUM(CASE WHEN findings_json='[]' THEN 0 ELSE 1 END) AS flagged
               FROM qa_audits WHERE run_id=? GROUP BY batch ORDER BY batch""",
            (run_id,),
        )]


def select_sample(
    candidates: list[dict], already: set[str], run_id: str,
    batch_size: int = DEFAULT_BATCH, rate: float = DEFAULT_RATE,
) -> list[dict]:
    """Deterministically sample `rate` of each new batch of `batch_size`.

    Deterministic so a re-run audits the same conversations and two operators
    reviewing the same batch see the same list.
    """

    fresh = [c for c in candidates if c["source_id"] not in already]
    if len(fresh) < batch_size:
        return []
    batch = fresh[:batch_size]
    take = max(1, round(batch_size * rate))
    ranked = sorted(
        batch,
        key=lambda c: hashlib.sha256(
            f"{run_id}:{c['source_id']}".encode()
        ).hexdigest(),
    )
    return ranked[:take]
