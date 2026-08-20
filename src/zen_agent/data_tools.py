"""Tools that let an agent investigate and improve the data factory.

The factory is a deterministic mill: fixed stages, no model agency, ~5 calls per
conversation. That is the right shape for 1.6M items — an exploratory loop at
that volume costs an order of magnitude more and destroys reproducibility.

But a mill cannot answer *why it is failing*. Why were 353 turns judged harmful?
Why does the judge reject half of what the verifier passes? Those are unknown
shape, low volume, high value — exactly what agency is for.

So these tools give the agent kernel in `coding_runtime` a domain: not the
corpus, but the factory's own record of what it did. The agent reads ledgers,
decisions and contracts, forms a hypothesis, and **proposes a patch**. It never
edits a contract in place and never writes to the dataset — its output is a file
under `.zen/proposals/` for a human to review, exactly as `RFC-0003` describes.

Read-only tools open every store with `mode=ro`; `data.query_ledgers` further
refuses any statement that is not a single SELECT.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import sqlite3
import subprocess
import time
from typing import Any

from .models import ToolRisk
from .tools import ToolContext, ToolRegistry, ToolSpec


# Ledgers the agent may read. Naming them here keeps the surface auditable and
# stops a generated query reaching a database nobody vetted.
LEDGERS = {
    "queue": ".zen/factory-queue.db",
    "qa": ".zen/qa-audit.db",
    "qualification": ".zen/factory-qualification.db",
    "control": ".zen/factory-control.db",
    "review": ".zen/review-feedback.db",
}

# Contract files the agent may read and propose changes to.
CONTRACT_ROOTS = ("plugins", "src/zen_agent", "tests")

_SELECT_ONLY = re.compile(r"^\s*select\b", re.I)
_FORBIDDEN = re.compile(
    r"\b(attach|pragma|insert|update|delete|drop|create|alter|replace|vacuum)\b", re.I
)

MAX_ROWS = 200
MAX_CELL_CHARS = 2_000


def _read_only(path: Path) -> sqlite3.Connection:
    """Open a ledger read-only.

    `mode=ro` rather than `immutable=1`: immutable caches the schema, which once
    hid newly added columns from a live reader for hours.
    """
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=15)
    connection.row_factory = sqlite3.Row
    return connection


def _truncate(value: Any) -> Any:
    if isinstance(value, str) and len(value) > MAX_CELL_CHARS:
        return value[:MAX_CELL_CHARS] + f"… [{len(value) - MAX_CELL_CHARS} more chars]"
    return value


def _query_ledgers(context: ToolContext, inputs: dict[str, Any]) -> dict[str, Any]:
    ledger = str(inputs["ledger"])
    if ledger not in LEDGERS:
        raise ValueError(f"unknown ledger {ledger}; choose from {sorted(LEDGERS)}")
    sql = str(inputs["sql"])
    # Two independent checks: the statement must *be* a select, and must not
    # contain a writing verb anywhere (a CTE or subquery could hide one).
    if not _SELECT_ONLY.match(sql):
        raise ValueError("only SELECT statements are permitted")
    if _FORBIDDEN.search(sql):
        raise ValueError("statement contains a non-read keyword")
    if ";" in sql.rstrip().rstrip(";"):
        raise ValueError("only a single statement is permitted")

    path = context.workspace / LEDGERS[ledger]
    if not path.is_file():
        raise FileNotFoundError(f"ledger not present: {LEDGERS[ledger]}")
    connection = _read_only(path)
    try:
        cursor = connection.execute(sql)
        rows = [
            {key: _truncate(row[key]) for key in row.keys()}
            for row in cursor.fetchmany(MAX_ROWS)
        ]
        truncated = cursor.fetchone() is not None
    finally:
        connection.close()
    return {"ledger": ledger, "rows": rows, "row_count": len(rows), "truncated": truncated}


def _failure_clusters(context: ToolContext, inputs: dict[str, Any]) -> dict[str, Any]:
    """Group QA findings by kind, with example source ids.

    The agent's usual first question is "what is going wrong, and where do I
    look?". Answering that with raw SQL costs several turns, so it is one call.
    """
    run_id = str(inputs["run_id"])
    limit = int(inputs.get("examples_per_kind", 5))
    path = context.workspace / LEDGERS["qa"]
    if not path.is_file():
        raise FileNotFoundError("qa-audit ledger not present; run zen-factory-audit first")
    connection = _read_only(path)
    clusters: dict[str, dict[str, Any]] = {}
    try:
        query = "SELECT source_id, status, judge_verdict, findings_json FROM qa_audits WHERE run_id=?"
        for row in connection.execute(query, (run_id,)):
            try:
                findings = json.loads(row["findings_json"] or "[]")
            except ValueError:
                continue
            for finding in findings:
                kind = str(finding.get("kind", "unknown"))
                bucket = clusters.setdefault(
                    kind, {"kind": kind, "count": 0, "examples": []}
                )
                bucket["count"] += 1
                if len(bucket["examples"]) < limit:
                    bucket["examples"].append({
                        "source_id": row["source_id"],
                        "terminal_status": row["status"],
                        "judge_verdict": row["judge_verdict"],
                        "detail": str(finding.get("detail") or finding.get("message") or "")[:400],
                    })
    finally:
        connection.close()
    ordered = sorted(clusters.values(), key=lambda item: -item["count"])
    return {"run_id": run_id, "clusters": ordered, "kinds": len(ordered)}


def _read_conversation(context: ToolContext, inputs: dict[str, Any]) -> dict[str, Any]:
    """Return one conversation's source turns beside every decision made about it.

    This is the evidence an investigation actually needs: what the agent said,
    what the refiner proposed, and what each reviewing stage concluded.
    """
    from .factory_review import build_review

    run_id = str(inputs["run_id"])
    source_id = str(inputs["source_id"])
    review = build_review(context.workspace, run_id)
    for conversation in review["conversations"]:
        # Accept whichever identifier the caller has. An agent reading the
        # ledgers sees packet_ids; a reviewer reading the site sees short
        # source ids. Refusing one of them just costs a turn to discover.
        identifiers = {
            conversation.get("source_id"),
            conversation.get("source_id_full"),
            conversation.get("packet_id"),
        }
        if source_id not in identifiers:
            continue
        turns = [
            {
                "turn_id": turn.get("turn_id"),
                "role": turn.get("role"),
                "text": _truncate(turn.get("text") or turn.get("source_text") or ""),
                "golden_text": _truncate(turn.get("golden_text") or ""),
                "action": turn.get("action"),
                "source_quality": turn.get("source_quality"),
                "downstream_coherence": turn.get("downstream_coherence"),
                "excluded_from_golden": turn.get("excluded_from_golden"),
            }
            for turn in conversation.get("turns", [])
        ]
        return {
            "source_id": conversation["source_id_full"],
            "terminal": conversation["terminal"],
            "audit": conversation.get("audit"),
            "iterations": conversation.get("iterations"),
            "turns": turns,
        }
    raise KeyError(
        f"conversation {source_id} not found in run {run_id}; pass a source_id "
        "or packet_id from data.failure_clusters or data.query_ledgers"
    )


def _contract_path(workspace: Path, relative: str) -> Path:
    candidate = (workspace / relative).resolve()
    if not str(candidate).startswith(str(workspace.resolve())):
        raise PermissionError("path escapes the workspace")
    if not any(relative.startswith(root) for root in CONTRACT_ROOTS):
        raise PermissionError(f"path must live under one of {CONTRACT_ROOTS}")
    return candidate


def _read_contract(context: ToolContext, inputs: dict[str, Any]) -> dict[str, Any]:
    path = _contract_path(context.workspace, str(inputs["path"]))
    if not path.is_file():
        raise FileNotFoundError(str(inputs["path"]))
    text = path.read_text(encoding="utf-8")
    return {
        "path": str(inputs["path"]),
        "lines": text.count("\n") + 1,
        "content": text[:120_000],
    }


def _propose_change(context: ToolContext, inputs: dict[str, Any]) -> dict[str, Any]:
    """Record a proposed contract change. Never applies it.

    A pipeline that edits its own contracts mid-run produces a corpus refined
    under several different rules with nothing recording which. So the agent
    writes a proposal and a human merges it.
    """
    relative = str(inputs["path"])
    _contract_path(context.workspace, relative)  # validate, do not write there
    rationale = str(inputs["rationale"]).strip()
    if len(rationale) < 40:
        raise ValueError("a proposal needs a rationale of at least 40 characters")
    evidence = list(inputs.get("evidence_source_ids") or [])
    if not evidence:
        raise ValueError("a proposal must cite the conversations that motivated it")

    directory = context.workspace / ".zen" / "proposals"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = time.strftime("%Y%m%dT%H%M%S")
    safe = relative.replace("/", "__")
    target = directory / f"{stamp}-{safe}.json"
    payload = {
        "path": relative,
        "rationale": rationale,
        "evidence_source_ids": evidence,
        "replacement": str(inputs["replacement"]),
        "proposed_at": time.time(),
        "task_id": context.task_id,
        "status": "PENDING_HUMAN_REVIEW",
    }
    target.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    target.chmod(0o600)
    return {
        "proposal": str(target.relative_to(context.workspace)),
        "status": "PENDING_HUMAN_REVIEW",
        "note": "written for human review; no contract was modified",
    }


def _run_tests(context: ToolContext, inputs: dict[str, Any]) -> dict[str, Any]:
    """Run the test suite so a proposal can be checked before a human sees it."""
    pattern = str(inputs.get("pattern") or "test*.py")
    if not re.fullmatch(r"[A-Za-z0-9_.*-]+", pattern):
        raise ValueError("pattern may contain only letters, digits, dot, dash, star, underscore")
    completed = subprocess.run(
        [".venv/bin/python", "-m", "unittest", "discover", "-s", "tests", "-p", pattern, "-q"],
        cwd=context.workspace, text=True, capture_output=True, timeout=900, check=False,
    )
    tail = (completed.stderr or completed.stdout)[-4_000:]
    return {"passed": completed.returncode == 0, "returncode": completed.returncode, "output": tail}


def data_tool_specs() -> list[ToolSpec]:
    string = {"type": "string", "minLength": 1}
    return [
        ToolSpec(
            "data.query_ledgers", "0.1.0",
            "Run one read-only SELECT against a named factory ledger",
            ToolRisk.READ_ONLY,
            {"type": "object", "required": ["ledger", "sql"], "additionalProperties": False,
             "properties": {"ledger": {"enum": sorted(LEDGERS)}, "sql": string}},
            {"type": "object"}, _query_ledgers,
        ),
        ToolSpec(
            "data.failure_clusters", "0.1.0",
            "Group QA findings for a run by kind, with example conversations",
            ToolRisk.READ_ONLY,
            {"type": "object", "required": ["run_id"], "additionalProperties": False,
             "properties": {"run_id": string,
                            "examples_per_kind": {"type": "integer", "minimum": 1, "maximum": 25}}},
            {"type": "object"}, _failure_clusters,
        ),
        ToolSpec(
            "data.read_conversation", "0.1.0",
            "Read one conversation's turns and decisions, by source_id or packet_id",
            ToolRisk.READ_ONLY,
            {"type": "object", "required": ["run_id", "source_id"], "additionalProperties": False,
             "properties": {"run_id": string, "source_id": string}},
            {"type": "object"}, _read_conversation,
        ),
        ToolSpec(
            "data.read_contract", "0.1.0",
            "Read a prompt, schema, or source file that governs the factory",
            ToolRisk.READ_ONLY,
            {"type": "object", "required": ["path"], "additionalProperties": False,
             "properties": {"path": string}},
            {"type": "object"}, _read_contract,
        ),
        ToolSpec(
            "data.propose_change", "0.1.0",
            "Record a proposed contract change for human review; applies nothing",
            ToolRisk.WORKSPACE_WRITE,
            {"type": "object",
             "required": ["path", "replacement", "rationale", "evidence_source_ids"],
             "additionalProperties": False,
             "properties": {"path": string, "replacement": {"type": "string"},
                            "rationale": string,
                            "evidence_source_ids": {"type": "array", "items": string,
                                                    "minItems": 1}}},
            {"type": "object"}, _propose_change,
        ),
        ToolSpec(
            "data.run_tests", "0.1.0",
            "Run the harness test suite and report pass or fail",
            ToolRisk.WORKSPACE_WRITE,
            {"type": "object", "additionalProperties": False,
             "properties": {"pattern": {"type": "string"}}},
            {"type": "object"}, _run_tests,
        ),
    ]


def register_data_tools(registry: ToolRegistry) -> None:
    for specification in data_tool_specs():
        registry.register(specification)
