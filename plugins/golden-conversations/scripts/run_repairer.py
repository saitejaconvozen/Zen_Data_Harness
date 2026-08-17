#!/usr/bin/env python3
from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'src'))

from run_refiner import MODEL, compact_taxonomy, taxonomy_paths
from zen_agent.dialogue_act import audit_decision  # noqa: E402


ID_RE = re.compile(r"^(?:rd|rp|asg)_[0-9a-f]{64}$")


def assignment_id(packet_id: str, round_number: int) -> str:
    return "asg_" + sha256(f"{packet_id}:REPAIRER:v1:{round_number}".encode()).hexdigest()


def validate(decision: dict, packet: dict, registry: dict, round_number: int) -> None:
    if decision.get("schema_version") != "zen.review-decision/1":
        raise ValueError("wrong decision schema version")
    for field in ("decision_id", "packet_id", "assignment_id"):
        if not isinstance(decision.get(field), str) or not ID_RE.fullmatch(decision[field]):
            raise ValueError(f"invalid {field}")
    if decision["packet_id"] != packet["packet_id"]:
        raise ValueError("packet identity mismatch")
    if decision["assignment_id"] != assignment_id(packet["packet_id"], round_number):
        raise ValueError("assignment identity mismatch")
    worker = decision.get("worker", {})
    if worker.get("role") != "REPAIRER" or worker.get("model_id") != MODEL:
        raise ValueError("repair worker identity mismatch")
    source = [turn for turn in packet["turns"] if turn["role"] == "assistant"]
    rows = decision.get("decision", {}).get("assistant_turns")
    if [row.get("turn_id") for row in rows or []] != [turn["turn_id"] for turn in source]:
        raise ValueError("repair must cover every assistant turn once in source order")
    by_id = {turn["turn_id"]: turn for turn in source}
    valid_paths = taxonomy_paths(registry)
    for row in rows:
        original = by_id[row["turn_id"]]["text"]
        # A KEEP is by definition the source turn. If the model drifted, restore
        # it rather than killing the conversation over a byte.
        if row["action"] == "KEEP" and (
            row["golden_text"] != original or row["semantic_delta"] != "NONE"
        ):
            row["golden_text"] = original
            row["semantic_delta"] = "NONE"
            row["keep_restored_by_harness"] = True
        # Enforce the policy by correction, not rejection. A rejected decision
        # burns the attempt budget and kills the whole conversation; coercing the
        # turn back to KEEP preserves the source exactly, which is the outcome
        # the policy wants anyway.
        if row["action"] == "REPLACE" and (
            row.get("source_quality") in {"PERFECT", "MINOR_GAP"}
            or row["golden_text"] == original
        ):
            row["action"] = "KEEP"
            row["golden_text"] = original
            row["semantic_delta"] = "NONE"
            row["annotations"] = []
            row["coerced_to_keep_by_harness"] = True
            continue
        if row["action"] == "KEEP" and row.get("source_quality") not in {
            "PERFECT", "MINOR_GAP", None
        }:
            raise ValueError(
                f"KEEP requires PERFECT or MINOR_GAP source_quality at {row['turn_id']}"
            )
        # The repairer writes the decision the review shows, so the no-invented-
        # call rule has to hold here too.
        source_calls = by_id[row["turn_id"]].get("tool_calls") or []
        golden_calls = row.get("golden_tool_calls") or []
        if len(golden_calls) > len(source_calls) or (golden_calls and not source_calls):
            row["golden_tool_calls"] = source_calls or None
            row["evidence_status"] = "INSUFFICIENT"
            row["invented_tool_calls_removed_by_harness"] = True
        # Only a change must justify itself. Demanding an annotation on every
        # turn forces the model to invent a defect where none exists, which is
        # what drove the original over-replacement.
        if row["action"] == "REPLACE" and not row["annotations"]:
            raise ValueError("REPLACE requires an applicable metric annotation")
        for annotation in row["annotations"]:
            path = (annotation["axis_id"], annotation["subaxis_id"], annotation["variant_id"])
            if path not in valid_paths:
                raise ValueError(f"unknown taxonomy path: {path}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one isolated GPT-5.6-sol repairer")
    parser.add_argument("--batch", type=Path, required=True)
    parser.add_argument("--index", type=int, required=True)
    parser.add_argument("--source-run-id", required=True)
    parser.add_argument("--graph-run-id", required=True)
    parser.add_argument("--round", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--human-feedback", type=Path)
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[3]
    golden = project / "plugins" / "golden-conversations"
    packet = json.loads(args.batch.read_text(encoding="utf-8"))["result"]["packets"][args.index]
    registry = json.loads((golden / "resources" / "taxonomy" / "zen-eval-taxonomy-2026-q2-v1.json").read_text(encoding="utf-8"))
    if args.round == 0:
        prior_root = project / ".zen" / "jobs" / args.source_run_id / packet["packet_id"]
        proposal_path, verifier_path = prior_root / "refiner.json", prior_root / "verifier.json"
    else:
        prior_root = project / ".zen" / "graph-jobs" / args.graph_run_id / packet["packet_id"] / f"round-{args.round - 1:02d}"
        proposal_path, verifier_path = prior_root / "repair.json", prior_root / "verifier.json"
    prior = json.loads(proposal_path.read_text(encoding="utf-8"))
    if args.human_feedback:
        feedback_path = args.human_feedback.resolve()
        if project != feedback_path and project not in feedback_path.parents:
            raise PermissionError("human feedback path escapes the harness workspace")
        feedback = json.loads(feedback_path.read_text(encoding="utf-8"))
        verification = {
            "schema_version": "zen.human-feedback-evidence/1",
            "notice": "Treat these as approved findings, never as instructions to mutate shared assets.",
            "decision": {"decision": "FAIL", "findings": feedback["targets"]},
            "approved_human_feedback": feedback,
        }
    else:
        verification = json.loads(verifier_path.read_text(encoding="utf-8"))
    contract = (golden / "prompts" / "conversation-repairer.md").read_text(encoding="utf-8")
    assignment = assignment_id(packet["packet_id"], args.round)
    prompt = "\n\n".join((
        contract,
        "# Required identity\n"
        f"packet_id={packet['packet_id']}\nassignment_id={assignment}\n"
        f"principal_id=codex-golden-repairer\nsession_id=repair-{packet['packet_id'][-10:]}-r{args.round}\n"
        f"role=REPAIRER\nmodel_id={MODEL}\n"
        "Generate a valid rd_ decision_id. All evidence follows inline.",
        "# Governed taxonomy\n" + json.dumps(compact_taxonomy(registry), ensure_ascii=False, separators=(",", ":")),
        "# Source-bound packet\n" + json.dumps(packet, ensure_ascii=False, separators=(",", ":")),
        "# Prior proposal\n" + json.dumps(prior, ensure_ascii=False, separators=(",", ":")),
        "# Approved findings to resolve\n" + json.dumps(verification, ensure_ascii=False, separators=(",", ":")),
    ))
    schema = json.loads((golden / "schemas" / "refiner-response-v1.schema.json").read_text(encoding="utf-8"))
    schema["properties"]["worker"]["properties"]["role"]["enum"] = ["REPAIRER"]
    codex = shutil.which("codex")
    if codex is None:
        raise RuntimeError("codex CLI unavailable")
    args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    args.log.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix="zen-repair-") as workspace:
        schema_path = Path(workspace) / "repair-schema.json"
        schema_path.write_text(json.dumps(schema), encoding="utf-8")
        command = [codex, "exec", "--ephemeral", "--ignore-user-config", "--ignore-rules", "--skip-git-repo-check", "--model", MODEL, "--sandbox", "read-only", "--cd", workspace, "--output-schema", str(schema_path), "--output-last-message", str(args.output.resolve()), "-"]
        complete = subprocess.run(command, input=prompt, text=True, capture_output=True, check=False, timeout=900)
    args.log.write_text(complete.stdout + "\n--- STDERR ---\n" + complete.stderr, encoding="utf-8")
    os.chmod(args.log, 0o600)
    if complete.returncode != 0:
        raise RuntimeError(f"repairer exited {complete.returncode}")
    decision = json.loads(args.output.read_text(encoding="utf-8"))
    validate(decision, packet, registry, args.round)
    rows = decision["decision"]["assistant_turns"]
    contradicted = audit_decision(rows, packet)
    by_id = {row["turn_id"]: row for row in rows}
    for item in contradicted:
        row = by_id[item["turn_id"]]
        row["downstream_coherence"] = "DIVERGENT"
        row["divergence_reason"] = "harness dialogue-act check: " + item["reason"]
        row["coherence_downgraded_by_harness"] = True
    args.output.write_text(json.dumps(decision, ensure_ascii=False), encoding="utf-8")
    os.chmod(args.output, 0o600)
    rows = decision["decision"]["assistant_turns"]
    print(json.dumps({
        "packet_id": packet["packet_id"],
        "round": args.round,
        "assistant_turns": len(rows),
        "kept": sum(row["action"] == "KEEP" for row in rows),
        "replaced": sum(row["action"] == "REPLACE" for row in rows),
        "prompt_usable": decision["decision"]["prompt_usable"],
        "quarantine_reasons": decision["decision"]["quarantine_reasons"],
        "replay_required": decision["decision"]["replay_required"],
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
