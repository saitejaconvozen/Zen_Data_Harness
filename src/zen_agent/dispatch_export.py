"""Assemble a reviewable batch of golden calls for dispatch.

`dataset_export` emits training rows — flat message arrays, nothing a person
would read. This emits the same conversations for **human review**: every
assistant turn paired with the caller turn it answers, and annotated with the
eval metrics that justified each correction.

Two things it gets right that a flat export cannot:

**Call direction.** An inbound call opens with the caller; an outbound call
opens with the agent. Pairing turns without knowing which produces exchanges
that are off by one for half the corpus. Direction is derived from the first
non-system turn — what actually happened — rather than from agent metadata,
which describes how the agent is configured, not how the call went.

**Metric attribution.** Every correction cites an `axis -> sub-axis -> variant`
path from the Zen eval taxonomy. Those citations are carried here per turn, with
names resolved, so a reviewer sees *which* rule a turn was judged against instead
of a bare identifier.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from .factory_review import build_review


RELEASABLE = ("VERIFIED_CANDIDATE", "PARTIAL_CANDIDATE")

# A call this short is not a conversation. The same floor is applied at binding,
# at the audit, and in the classifier, but it is repeated here because the export
# is the last gate before data leaves the harness — and the one place a loosened
# upstream rule would otherwise go unnoticed.
MIN_EXCHANGES = 3
MIN_ASSISTANT_TURNS = 3

INBOUND = "INBOUND"
OUTBOUND = "OUTBOUND"


def call_direction(turns: Sequence[Mapping[str, Any]]) -> str:
    """Who spoke first, ignoring system and runtime-metadata turns.

    Inbound: the caller dialled in and speaks first. Outbound: the agent placed
    the call and opens. Read from the transcript rather than agent
    configuration, because a nominally outbound agent can appear in an inbound
    recording and the pairing has to follow the actual call.
    """
    for turn in turns:
        role = turn.get("role")
        if role in {"user", "assistant"}:
            return INBOUND if role == "user" else OUTBOUND
    return INBOUND


def _assistant_turn(turn: Mapping[str, Any]) -> dict[str, Any]:
    """One agent turn with its correction and the metrics behind it."""
    citations = [
        {
            "axis_id": c.get("axis_id"),
            "axis": c.get("axis_name"),
            "subaxis_id": c.get("subaxis_id"),
            "subaxis": c.get("subaxis_name"),
            "variant_id": c.get("variant_id"),
            "variant": c.get("variant_name"),
            "source_verdict": c.get("source_verdict"),
            "golden_verdict": c.get("golden_verdict"),
        }
        for c in turn.get("metric_citations") or []
    ]
    record = {
        "turn_id": turn.get("turn_id"),
        "role": "assistant",
        "action": turn.get("action"),
        "source_text": turn.get("source_text"),
        "golden_text": turn.get("golden_text") or turn.get("source_text"),
        "changed": turn.get("action") == "REPLACE",
        "source_quality": turn.get("source_quality"),
        "correction_reason": turn.get("correction_reason") or "",
        # Named so the reviewer never has to look up an identifier.
        "metrics": citations,
        "metric_count": len(citations),
        "excluded_from_golden": bool(turn.get("excluded_from_golden")),
    }
    if turn.get("golden_tool_calls"):
        record["tool_calls"] = turn["golden_tool_calls"]
    return record


def _caller_turn(turn: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "turn_id": turn.get("turn_id"),
        "role": turn.get("role"),
        "text": turn.get("text", ""),
        # Stated explicitly on every row: caller speech is never rewritten.
        "source_preserved": True,
    }


def build_exchanges(
    turns: Sequence[Mapping[str, Any]], direction: str
) -> list[dict[str, Any]]:
    """Group turns into exchanges ordered by who speaks first.

    Inbound exchanges read (caller, agent); outbound read (agent, caller).
    Consecutive turns from the same speaker are kept together rather than
    forced into alternation, because real calls contain them and splitting
    them would misrepresent the transcript.
    """
    lead, follow = ("user", "assistant") if direction == INBOUND else ("assistant", "user")
    exchanges: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    for turn in turns:
        role = turn.get("role")
        if role not in {"user", "assistant", "tool"}:
            continue
        rendered = (
            _assistant_turn(turn) if role == "assistant" else _caller_turn(turn)
        )
        if role == lead and (current is None or current.get(follow)):
            current = {"index": len(exchanges), lead: [rendered], follow: []}
            exchanges.append(current)
            continue
        if current is None:
            # A call that opens against its own direction — record it rather
            # than dropping the turn.
            current = {"index": len(exchanges), lead: [], follow: []}
            exchanges.append(current)
        current.setdefault(role if role != "tool" else follow, []).append(rendered)
    return exchanges


def _coverage_tree(citations: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Group citations into axis -> sub-axis -> variant with counts.

    A flat citation list answers "what was cited"; the tree answers "which parts
    of the eval taxonomy this data actually exercises", which is the question
    that decides whether a batch is worth training on.
    """
    axes: dict[str, dict[str, Any]] = {}
    for citation in citations:
        axis = citation.get("axis_name") or citation.get("axis_id") or "unknown"
        sub = citation.get("subaxis_name") or citation.get("subaxis_id") or "unknown"
        variant = citation.get("variant_name") or citation.get("variant_id") or "unknown"
        node = axes.setdefault(axis, {"axis": axis, "count": 0, "subaxes": {}})
        node["count"] += 1
        sub_node = node["subaxes"].setdefault(sub, {"subaxis": sub, "count": 0, "variants": {}})
        sub_node["count"] += 1
        sub_node["variants"][variant] = sub_node["variants"].get(variant, 0) + 1

    return [
        {
            "axis": node["axis"],
            "count": node["count"],
            "subaxes": [
                {
                    "subaxis": s["subaxis"],
                    "count": s["count"],
                    "variants": [
                        {"variant": v, "count": n}
                        for v, n in sorted(s["variants"].items(), key=lambda x: -x[1])
                    ],
                }
                for s in sorted(node["subaxes"].values(), key=lambda x: -x["count"])
            ],
        }
        for node in sorted(axes.values(), key=lambda x: -x["count"])
    ]


def conversation_record(
    conversation: Mapping[str, Any], run_id: str
) -> dict[str, Any] | None:
    turns = conversation.get("turns") or []
    if not turns:
        return None
    direction = call_direction(turns)
    exchanges = build_exchanges(turns, direction)
    assistant_turns = [t for t in turns if t.get("role") == "assistant"]
    changed = [t for t in assistant_turns if t.get("action") == "REPLACE"]
    citations = [c for t in assistant_turns for c in (t.get("metric_citations") or [])]

    if len(exchanges) < MIN_EXCHANGES or len(assistant_turns) < MIN_ASSISTANT_TURNS:
        return None

    classification = conversation.get("classification") or {}
    audit = conversation.get("audit") or {}
    return {
        "conversation_id": conversation.get("source_id_full"),
        "short_id": conversation.get("source_id"),
        "run_id": run_id,
        # Which agent configuration produced this call. Two conversations from
        # the same configuration share a system prompt, so a defect that recurs
        # across them is a prompt problem, not an agent problem.
        "configuration_id": conversation.get("configuration_id"),
        "source_audit": {
            "verdict": audit.get("verdict"),
            "prompt_coherent": audit.get("prompt_coherent"),
            "workflow_obeyed": audit.get("workflow_obeyed"),
            "critical_failures": len(
                [f for f in audit.get("findings") or []
                 if f.get("severity") == "CRITICAL"]
            ),
        },
        "call_direction": direction,
        "turn_order": (
            "caller speaks first (user, assistant)" if direction == INBOUND
            else "agent speaks first (assistant, user)"
        ),
        "terminal_status": (conversation.get("terminal") or {}).get("status"),
        "domain": classification.get("domain"),
        "primary_language": classification.get("primary_language"),
        "code_switching": bool(classification.get("code_switching")),
        "counts": {
            "exchanges": len(exchanges),
            "assistant_turns": len(assistant_turns),
            "corrected_turns": len(changed),
            "metric_citations": len(citations),
        },
        # Distinct axes touched, so a reviewer can see the shape of a call's
        # problems before reading a single turn.
        "axes_touched": sorted({
            c.get("axis_name") or c.get("axis_id") for c in citations if c
        }),
        # Full axis -> sub-axis -> variant rollup for this one call, so a
        # reviewer can see its metric profile without walking every turn.
        "metric_coverage": _coverage_tree(citations),
        "exchanges": exchanges,
        # Consumed by the batch rollup and removed before writing.
        "_citations": list(citations),
    }


def export_batch(
    root: Path,
    run_ids: Iterable[str],
    *,
    limit: int,
    out_dir: Path,
    name: str = "batch-001",
) -> dict[str, Any]:
    """Write one dispatch batch plus a manifest describing it."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{name}.jsonl"
    manifest: dict[str, Any] = {
        "batch": name,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "runs": list(run_ids),
        "conversations": 0,
        "skipped_too_short": 0,
        "by_direction": {INBOUND: 0, OUTBOUND: 0},
        "by_language": {},
        "assistant_turns": 0,
        "corrected_turns": 0,
        "metric_citations": 0,
        "axis_histogram": {},
        "by_domain": {},
        "by_terminal_status": {},
        "metric_coverage": [],
        # How much of the governed taxonomy this batch actually exercises.
        # A batch that only ever cites two variants trains for two variants.
        "taxonomy_reach": {},
    }

    written = 0
    all_citations: list[Mapping[str, Any]] = []
    with path.open("w", encoding="utf-8") as handle:
        for run_id in manifest["runs"]:
            if written >= limit:
                break
            review = build_review(root, run_id)
            for conversation in review["conversations"]:
                if written >= limit:
                    break
                if (conversation.get("terminal") or {}).get("status") not in RELEASABLE:
                    continue
                record = conversation_record(conversation, run_id)
                if record is None:
                    manifest["skipped_too_short"] += 1
                    continue
                # Raw citations feed the batch rollup only; strip them before
                # writing so the dispatched record carries the resolved tree.
                all_citations.extend(record.pop("_citations", []))
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                written += 1
                manifest["conversations"] += 1
                manifest["by_direction"][record["call_direction"]] += 1
                language = record.get("primary_language") or "unknown"
                manifest["by_language"][language] = (
                    manifest["by_language"].get(language, 0) + 1
                )
                counts = record["counts"]
                manifest["assistant_turns"] += counts["assistant_turns"]
                manifest["corrected_turns"] += counts["corrected_turns"]
                manifest["metric_citations"] += counts["metric_citations"]
                for axis in record["axes_touched"]:
                    manifest["axis_histogram"][axis] = (
                        manifest["axis_histogram"].get(axis, 0) + 1
                    )
                domain = record.get("domain") or "unclassified"
                manifest["by_domain"][domain] = manifest["by_domain"].get(domain, 0) + 1
                status = record.get("terminal_status") or "unknown"
                manifest["by_terminal_status"][status] = (
                    manifest["by_terminal_status"].get(status, 0) + 1
                )

    manifest["metric_coverage"] = _coverage_tree(all_citations)
    variants = {
        (c.get("axis_name"), c.get("subaxis_name"), c.get("variant_name"))
        for c in all_citations
    }
    manifest["taxonomy_reach"] = {
        "axes": len(manifest["axis_histogram"]),
        "distinct_variants_cited": len(variants),
        "citations": len(all_citations),
    }
    path.chmod(0o600)
    manifest["path"] = str(path)
    manifest_path = out_dir / f"{name}.manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    manifest_path.chmod(0o600)
    return manifest
