#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import Counter
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from zen_agent.dialogue_act import audit_decision  # noqa: E402
from zen_agent.turn_format import (  # noqa: E402
    LEADING_TAG_RE,
    declared_language_tags,
    detect_script_language,
    requires_language_tag,
)

# Shared transport: provider is chosen by ZEN_MODEL_PROVIDER (codex | claude).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _transport import active_model, run_model  # noqa: E402


MODEL = active_model()

# Below this many spoken characters, a turn is not addressing the caller.
# Measured: 71 of 271 tool-calling turns fell under it, several saying "hmm".
MIN_TOOL_TURN_SPEECH = 12

# Control tags are protocol, not speech; they must not count toward the floor.
_CONTROL_TAG = re.compile(r"<\|[A-Z_]+\|>|\b(?:WAITING|ENDCALL)\s*\d*\b")


def _spoken_length(text: str) -> int:
    """Characters actually addressed to the caller."""
    return len(_CONTROL_TAG.sub("", text or "").strip())
ID_RE = re.compile(r"^(?:rd|rp|asg)_[0-9a-f]{64}$")
# Text that describes the mechanism rather than speaking to the caller.
_NARRATION_RE = re.compile(
    r"^(calling|invoking|executing|running|querying)\b"
    r"|^\[?(tool|function)[ _-]?call",
    re.I,
)


def _strip_tag(text: str) -> str:
    """Text without its leading language tag, for tag-insensitive comparison."""

    return LEADING_TAG_RE.sub("", text, count=1).strip()


def apply_language_tags(rows: list[dict], packet: dict) -> None:
    """Deterministically restore mandatory leading language tags.

    ``golden_text`` stays exactly as the model returned it so the KEEP
    byte-identity invariant and independent verification remain checkable.
    The tag-normalised string used for the dataset is written alongside it as
    ``golden_text_final``.
    """

    system_prompt = packet["system_prompt"]
    required = requires_language_tag(system_prompt)
    declared = declared_language_tags(system_prompt)
    for row in rows:
        text = row["golden_text"]
        row["golden_text_final"] = text
        if not required or LEADING_TAG_RE.match(text):
            continue
        # The model's declaration wins: no script heuristic can reliably tell
        # romanised Hindi from English, and mis-tagging fails verification.
        declared_language = (row.get("response_language") or "").strip().upper()
        tag = declared_language if declared_language in declared else None
        if tag is None and declared_language and not declared:
            tag = declared_language
        if tag is None:
            tag = detect_script_language(text)
        if tag is None or (declared and tag not in declared):
            tag = next(iter(sorted(declared))) if len(declared) == 1 else None
        if tag is None:
            continue
        row["golden_text_final"] = f"<|{tag}|> {text.lstrip()}"
        row["language_tag_applied"] = tag


def compact_taxonomy(registry: dict) -> dict:
    axes = []
    for axis in registry["axes"]:
        if not axis.get("enabled"):
            continue
        subaxes = []
        for subaxis in axis["subaxes"]:
            if not subaxis.get("enabled"):
                continue
            variants = [
                {
                    "id": variant["id"],
                    "name": variant["name"],
                    "description": variant["description"],
                }
                for variant in subaxis["variants"]
                if variant.get("enabled")
            ]
            subaxes.append(
                {
                    "id": subaxis["id"],
                    "name": subaxis["name"],
                    "description": subaxis["description"],
                    "variants": variants,
                }
            )
        axes.append(
            {
                "id": axis["id"],
                "name": axis["name"],
                "description": axis["description"],
                "subaxes": subaxes,
            }
        )
    return {
        "taxonomy_id": registry["taxonomy_id"],
        "taxonomy_version": registry["taxonomy_version"],
        "source": registry["source"],
        "axes": axes,
    }


def taxonomy_paths(registry: dict) -> set[tuple[str, str, str]]:
    return {
        (axis["id"], subaxis["id"], variant["id"])
        for axis in registry["axes"]
        if axis.get("enabled")
        for subaxis in axis["subaxes"]
        if subaxis.get("enabled")
        for variant in subaxis["variants"]
        if variant.get("enabled")
    }


def assignment_id(packet_id: str) -> str:
    digest = sha256(f"{packet_id}:REFINER:v2".encode("utf-8")).hexdigest()
    return "asg_" + digest


def validate_decision(decision: dict, packet: dict, registry: dict) -> None:
    if decision.get("schema_version") != "zen.review-decision/1":
        raise ValueError("wrong decision schema version")
    for field in ("decision_id", "packet_id", "assignment_id"):
        if not isinstance(decision.get(field), str) or not ID_RE.fullmatch(decision[field]):
            raise ValueError(f"invalid {field}")
    if decision["packet_id"] != packet["packet_id"]:
        raise ValueError("decision packet_id mismatch")
    if decision["assignment_id"] != assignment_id(packet["packet_id"]):
        raise ValueError("decision assignment_id mismatch")
    worker = decision.get("worker", {})
    # The model id is the harness's fact, not the model's claim.
    worker["model_id"] = MODEL
    if worker.get("role") != "REFINER":
        raise ValueError("worker role/model mismatch")

    # A `tool` turn is what the backend actually returned — immutable evidence,
    # not something the refiner may propose changes to.
    for turn in packet["turns"]:
        if turn.get("role") == "tool" and turn.get("text") is None:
            raise ValueError(f"tool result turn lost its content: {turn.get('turn_id')}")
    source_assistant = [turn for turn in packet["turns"] if turn["role"] == "assistant"]
    rows = decision.get("decision", {}).get("assistant_turns")
    if not isinstance(rows, list):
        raise ValueError("assistant_turns is not an array")
    expected_ids = [turn["turn_id"] for turn in source_assistant]
    observed_ids = [row.get("turn_id") for row in rows]
    if observed_ids != expected_ids:
        raise ValueError("refiner must return every assistant turn once in source order")
    by_id = {turn["turn_id"]: turn for turn in source_assistant}
    # A turn is terminal when no user turn follows it anywhere in the packet.
    order = [turn["turn_id"] for turn in packet["turns"]]
    roles = {turn["turn_id"]: turn["role"] for turn in packet["turns"]}
    terminal_ids = set()
    for turn_id in expected_ids:
        after = order[order.index(turn_id) + 1 :]
        if not any(roles[later] == "user" for later in after):
            terminal_ids.add(turn_id)

    valid_paths = taxonomy_paths(registry)
    for row in rows:
        source = by_id[row["turn_id"]]
        if row["action"] == "KEEP" and row["golden_text"] != source["text"]:
            # The contract tells the model to omit leading tags on REPLACE, so a
            # KEEP that differs only by its tag is that rule over-applied, not a
            # content change. Restore the source exactly and continue.
            if _strip_tag(row["golden_text"]) == _strip_tag(source["text"]):
                row["golden_text"] = source["text"]
            elif source.get("tool_calls") and not source["text"].strip():
                # A tool-only turn has no speech; any text here is invented
                # narration that would be read aloud. Restore the silence.
                row["golden_text"] = source["text"]
                row["narration_removed_by_harness"] = True
            else:
                row["golden_text"] = source["text"]
                row["keep_text_restored_by_harness"] = True
        if row["action"] == "KEEP" and row["semantic_delta"] != "NONE":
            row["semantic_delta"] = "NONE"
            row["semantic_delta_corrected_by_harness"] = True
        # An unchanged "replacement" is a no-op — unless the tool call changed,
        # which is a real correction even when the spoken text is identical.
        tool_changed = (
            row.get("golden_tool_calls") is not None
            and row["golden_tool_calls"] != source.get("tool_calls")
        )
        if row["action"] == "REPLACE" and row["golden_text"] == source["text"] and not tool_changed:
            row["action"] = "KEEP"
            row["semantic_delta"] = "NONE"
            row["source_quality"] = "PERFECT"
            row["annotations"] = []
            row["coerced_to_keep_by_harness"] = True
            continue
        # A stylistic observation is not a defect. Only a real violation earns a
        # rewrite; anything else is left exactly as it was spoken.
        # The model kept a turn it graded as defective — it found no grounded
        # correction. Preserving the source is right; presenting it as exemplary
        # is not. Exclude the turn and keep the rest of the conversation.
        if row["action"] == "KEEP" and row["source_quality"] not in {"PERFECT", "MINOR_GAP"}:
            row["evidence_status"] = "INSUFFICIENT"
            row["kept_defect_excluded_by_harness"] = True
        # Stylistic preference is not a defect. Restore what was actually said
        # rather than shipping an unjustified rewrite.
        if row["action"] == "REPLACE" and row["source_quality"] not in {
            "MAJOR_GAP", "CRITICAL_GAP"
        }:
            row["action"] = "KEEP"
            row["golden_text"] = source["text"]
            row["semantic_delta"] = "NONE"
            row["annotations"] = []
            row["unjustified_replacement_reverted_by_harness"] = True
            continue
        # A tool-calling turn with no speech is a training example that teaches
        # the model to invoke a backend while saying nothing. The contract asks
        # for a holding phrase; when the refiner leaves one short anyway, the
        # turn is excluded rather than shipped — for SFT every assistant turn is
        # a target, and this one would teach silence.
        spoken = _spoken_length(row.get("golden_text") or "")
        if (row.get("golden_tool_calls") or source.get("tool_calls")) and spoken < MIN_TOOL_TURN_SPEECH:
            row["evidence_status"] = "INSUFFICIENT"
            row["silent_tool_turn_excluded_by_harness"] = True

        # Never let a fabricated tool call reach the dataset. Adding a call
        # invents an action that never happened and whose result never existed —
        # the model would learn to claim work it did not do. A genuinely missing
        # call is a finding, not something the harness writes for the agent.
        source_calls = source.get("tool_calls") or []
        golden_calls = row.get("golden_tool_calls") or []
        if len(golden_calls) > len(source_calls):
            row["golden_tool_calls"] = source_calls or None
            row["evidence_status"] = "INSUFFICIENT"
            row["invented_tool_calls_removed_by_harness"] = True
        elif golden_calls and not source_calls:
            row["golden_tool_calls"] = None
            row["evidence_status"] = "INSUFFICIENT"
            row["invented_tool_calls_removed_by_harness"] = True
        # Mechanical narration would be spoken aloud to the caller.
        if _NARRATION_RE.match((row.get("golden_text") or "").strip()):
            row["golden_text"] = source["text"]
            row["narration_removed_by_harness"] = True
        # Only a REPLACE must justify itself; a perfect turn may cite nothing.
        if row["action"] == "REPLACE" and not row["annotations"]:
            row["action"] = "KEEP"
            row["golden_text"] = source["text"]
            row["semantic_delta"] = "NONE"
            row["source_quality"] = "MINOR_GAP"
            row["unjustified_replacement_reverted_by_harness"] = True
            continue
        # Whether a user turn follows is a fact the harness already computed.
        # Correct a mislabel instead of discarding the whole conversation.
        coherence = row["downstream_coherence"]
        is_terminal = row["turn_id"] in terminal_ids
        if is_terminal and coherence != "TERMINAL_TURN":
            row["downstream_coherence"] = coherence = "TERMINAL_TURN"
            row["coherence_corrected_by_harness"] = True
        elif not is_terminal and coherence == "TERMINAL_TURN":
            row["downstream_coherence"] = coherence = "PRESERVED"
            row["coherence_corrected_by_harness"] = True
        if coherence == "DIVERGENT" and not row.get("divergence_reason"):
            row["divergence_reason"] = (
                "the refiner marked this turn divergent without stating why; "
                "excluded for human review"
            )
            row["divergence_reason_supplied_by_harness"] = True
        invalid = []
        for annotation in row["annotations"]:
            path = (
                annotation["axis_id"],
                annotation["subaxis_id"],
                annotation["variant_id"],
            )
            if path not in valid_paths:
                invalid.append(annotation)
        if invalid:
            row["annotations"] = [a for a in row["annotations"] if a not in invalid]
            row["invalid_annotations_dropped_by_harness"] = True
            if row["action"] == "REPLACE" and not row["annotations"]:
                row["action"] = "KEEP"
                row["golden_text"] = source["text"]
                row["semantic_delta"] = "NONE"
                row["source_quality"] = "MINOR_GAP"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one isolated GPT-5.6-sol refiner")
    parser.add_argument("--batch", type=Path, required=True)
    parser.add_argument("--index", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    args = parser.parse_args()

    project = Path(__file__).resolve().parents[3]
    golden = project / "plugins" / "golden-conversations"
    batch_wrapper = json.loads(args.batch.read_text(encoding="utf-8"))
    packet = batch_wrapper["result"]["packets"][args.index]
    registry = json.loads(
        (golden / "resources" / "taxonomy" / "zen-eval-taxonomy-2026-q2-v1.json").read_text(encoding="utf-8")
    )
    contract = (golden / "prompts" / "conversation-refiner.md").read_text(encoding="utf-8")
    assignment = assignment_id(packet["packet_id"])
    prompt = "\n\n".join(
        (
            contract,
            "# Required worker identity\n"
            f"packet_id={packet['packet_id']}\nassignment_id={assignment}\n"
            f"principal_id=codex-golden-refiner\nsession_id=refiner-{packet['packet_id'][-12:]}-v2\n"
            f"role=REFINER\nmodel_id={MODEL}\n"
            "A decision_id is supplied by the harness; emit any rd_ value. Do not use tools; all evidence follows inline.",
            "# Governed active taxonomy JSON\n" + json.dumps(compact_taxonomy(registry), ensure_ascii=False, separators=(",", ":")),
            "# Source-bound refinement packet JSON\n" + json.dumps(packet, ensure_ascii=False, separators=(",", ":")),
        )
    )
    if len(prompt) > 1_500_000:
        raise ValueError("inline refiner prompt exceeds 1.5 million characters")

    decision = run_model(
        prompt=prompt,
        schema_path=Path(golden / "schemas" / "refiner-response-v1.schema.json"),
        output_path=args.output,
        log_path=args.log,
        role="REFINER",
    )
    validate_decision(decision, packet, registry)

    rows = decision["decision"]["assistant_turns"]
    apply_language_tags(rows, packet)

    # A self-reported PRESERVED that the transcript contradicts is downgraded
    # here. Both the refiner and an independent verifier have agreed on a wrong
    # label; only a check outside the model can catch that.
    contradicted = audit_decision(rows, packet)
    by_id = {row["turn_id"]: row for row in rows}
    for item in contradicted:
        row = by_id[item["turn_id"]]
        row["downstream_coherence"] = "DIVERGENT"
        row["divergence_reason"] = "harness dialogue-act check: " + item["reason"]
        row["coherence_downgraded_by_harness"] = True

    # replay_required is derived from the turns, never taken on trust.
    divergent = [row["turn_id"] for row in rows if row["downstream_coherence"] == "DIVERGENT"]
    decision["decision"]["replay_required"] = bool(divergent)
    decision["decision"]["replay_from_turn_id"] = divergent[0] if divergent else None
    args.output.write_text(json.dumps(decision, ensure_ascii=False), encoding="utf-8")
    os.chmod(args.output, 0o600)

    unassessable = [row["turn_id"] for row in rows if row["evidence_status"] == "INSUFFICIENT"]
    quality = Counter(row["source_quality"] for row in rows)
    summary = {
        "packet_index": args.index,
        "packet_id": packet["packet_id"],
        "assistant_turns": len(rows),
        "kept": sum(row["action"] == "KEEP" for row in rows),
        "replaced": sum(row["action"] == "REPLACE" for row in rows),
        "annotations": sum(len(row["annotations"]) for row in rows),
        "source_quality": dict(quality),
        # Turns whose golden text is usable because the recorded reply still fits.
        "coherent_turns": sum(
            row["downstream_coherence"] in {"PRESERVED", "TERMINAL_TURN"} for row in rows
        ),
        "divergent_turns": divergent,
        "unassessable_turns": unassessable,
        "prompt_usable": decision["decision"]["prompt_usable"],
        "conversation_assessable": decision["decision"]["conversation_assessable"],
        "quarantine_reasons": decision["decision"]["quarantine_reasons"],
        "replay_required": bool(divergent),
        "output": str(args.output),
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
