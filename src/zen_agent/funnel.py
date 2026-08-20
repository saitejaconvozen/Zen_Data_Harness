"""Where conversations are lost between MongoDB and the training set.

The target is a fixed number of high-quality conversations, not a fixed number
of fetches: fetch 25,000, discard what does not survive the gates, ship what
does. That only works if attrition is visible at every stage — otherwise the
only way to hit a number is to guess how many to fetch and find out hours later.

Each stage reports how many entered, how many survived, and *why* the rest were
dropped. Two things fall out that nothing else in the harness can answer:

* the realised survival rate, so the fetch target is arithmetic rather than
  hope — measured at 34%, meaning 10,000 candidates needs ~29,000 fetched;
* which gate is doing the discarding, so tightening quality is a decision with
  a visible cost instead of a silent one.

Every number here is counted from the durable stores, never estimated.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
from pathlib import Path
import re
import sqlite3
from typing import Any


# Stages in the order a conversation passes through them. Only stages that can
# *lose* a conversation are gates; the rest are recorded for completeness.
GATES = (
    ("bound", "read from MongoDB and bound to an immutable packet"),
    ("audited", "agent audit completed"),
    ("selected", "zero critical failures — the quality gate"),
    ("refined", "refiner produced a decision"),
    ("verified", "independent verification completed"),
    ("candidate", "reached a releasable terminal state"),
    ("exportable", "survived the export floors and is a training row"),
)

_CONTROL_TAG = re.compile(r"<\|[A-Z_]+\|>|\b(?:WAITING|ENDCALL)\s*\d*\b")


def _spoken(text: str) -> int:
    return len(_CONTROL_TAG.sub("", text or "").strip())


def _read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=20)
    connection.row_factory = sqlite3.Row
    return connection


def stage_counts(root: Path, run_id: str) -> dict[str, Any]:
    """Counts straight from the queue, plus the reasons for each loss."""
    queue = root / ".zen" / "factory-queue.db"
    if not queue.is_file():
        return {"run_id": run_id, "stages": [], "losses": {}}

    connection = _read_only(queue)
    try:
        def count(stage: str, status: str | None = None) -> int:
            sql = "SELECT COUNT(*) n FROM factory_work WHERE run_id=? AND stage=?"
            args: list[Any] = [run_id, stage]
            if status:
                sql += " AND status=?"
                args.append(status)
            return connection.execute(sql, args).fetchone()["n"]

        terminal: dict[str, int] = {}
        for row in connection.execute(
            "SELECT payload_json FROM factory_work WHERE run_id=? AND stage='terminal' "
            "AND status='SUCCEEDED'", (run_id,)
        ):
            try:
                status = (json.loads(row["payload_json"]).get("inputs") or {}).get(
                    "terminal_status")
            except ValueError:
                continue
            if status:
                terminal[status] = terminal.get(status, 0) + 1

        bound = count("agent_audit")
        audited = count("agent_audit", "SUCCEEDED")
        refined = count("refine", "SUCCEEDED")
        # Derive the gate from its own outcome, not from downstream counts. The
        # first version inferred "selected" from refine totals, which
        # double-counted conversations refined before the critical-failure gate
        # existed and reported a 97% pass rate for a gate that rejects 62%.
        not_selected = terminal.get("NOT_SELECTED", 0)
        verified = count("verify", "SUCCEEDED")
        candidate = terminal.get("VERIFIED_CANDIDATE", 0) + terminal.get(
            "PARTIAL_CANDIDATE", 0)
    finally:
        connection.close()

    return {
        "run_id": run_id,
        "bound": bound,
        "audited": audited,
        # Passed the critical-failure gate: audited, minus those it rejected.
        "selected": max(audited - not_selected, 0),
        "refined": refined,
        "verified": verified,
        "candidate": candidate,
        "terminal_breakdown": terminal,
    }


def count_pending(root: Path, run_id: str) -> int:
    """Conversations past the gate but not yet refined; they are not losses."""
    queue = root / ".zen" / "factory-queue.db"
    connection = _read_only(queue)
    try:
        return connection.execute(
            "SELECT COUNT(*) n FROM factory_work WHERE run_id=? AND stage='refine' "
            "AND status IN ('READY','LEASED')", (run_id,)
        ).fetchone()["n"]
    finally:
        connection.close()


def export_survival(conversations: list[Mapping[str, Any]]) -> dict[str, Any]:
    """How many candidates actually become training rows, and what is masked.

    The last gate, and the one that matters most for SFT: an assistant turn
    with no speech is a target that teaches the model to answer with nothing.
    """
    from .dispatch_export import MIN_ASSISTANT_TURNS, MIN_EXCHANGES

    kept = dropped_short = 0
    targets = masked = tool_turns = silent_tool = 0
    for conversation in conversations:
        exchanges = conversation.get("exchanges") or []
        assistant = [
            turn
            for exchange in exchanges
            for turn in (exchange.get("assistant") or [])
        ]
        if len(exchanges) < MIN_EXCHANGES or len(assistant) < MIN_ASSISTANT_TURNS:
            dropped_short += 1
            continue
        kept += 1
        for turn in assistant:
            text = turn.get("golden_text") or ""
            has_tool = bool(turn.get("tool_calls"))
            if has_tool:
                tool_turns += 1
                if _spoken(text) < 12:
                    silent_tool += 1
            if _spoken(text) < 5 and not has_tool:
                masked += 1
            else:
                targets += 1

    return {
        "candidates_in": len(conversations),
        "exportable": kept,
        "dropped_too_short": dropped_short,
        "sft_targets": targets,
        "masked_no_speech": masked,
        "tool_turns": tool_turns,
        "silent_tool_turns": silent_tool,
    }


def build(root: Path, run_id: str, conversations: list[Mapping[str, Any]] | None = None
          ) -> dict[str, Any]:
    """The whole funnel, with survival rate and the fetch target it implies."""
    counts = stage_counts(root, run_id)
    export = export_survival(list(conversations or []))

    bound = counts.get("bound") or 0
    candidate = counts.get("candidate") or 0
    survival = (candidate / bound) if bound else 0.0

    stages = []
    previous = bound
    for key, description in GATES:
        value = export["exportable"] if key == "exportable" else counts.get(key, 0)
        stages.append({
            "stage": key,
            "description": description,
            "count": value,
            "share_of_bound": round(100 * value / bound, 1) if bound else 0.0,
            "lost_here": max(previous - value, 0),
        })
        previous = value

    return {
        "run_id": run_id,
        "stages": stages,
        "terminal_breakdown": counts.get("terminal_breakdown", {}),
        "export": export,
        "survival_rate": round(survival, 4),
        # The arithmetic the fetch target actually depends on.
        "fetch_needed_for_10k": int(10_000 / survival) if survival > 0.01 else None,
    }
