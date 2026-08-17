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

from run_refiner import MODEL, compact_taxonomy, taxonomy_paths


ID_RE = re.compile(r"^(?:rd|rp|asg)_[0-9a-f]{64}$")


def assignment_id(packet_id: str, round_number: int) -> str:
    return "asg_" + sha256(f"{packet_id}:GRAPH_VERIFIER:v1:{round_number}".encode()).hexdigest()


def validate(decision: dict, packet: dict, proposal: dict, registry: dict, round_number: int) -> None:
    for field in ("decision_id", "packet_id", "assignment_id"):
        if not isinstance(decision.get(field), str) or not ID_RE.fullmatch(decision[field]):
            raise ValueError(f"invalid {field}")
    if decision["packet_id"] != packet["packet_id"] or decision["assignment_id"] != assignment_id(packet["packet_id"], round_number):
        raise ValueError("verifier binding mismatch")
    worker = decision.get("worker", {})
    if worker.get("role") != "VERIFIER" or worker.get("model_id") != MODEL:
        raise ValueError("verifier worker identity mismatch")
    if worker.get("session_id") == proposal.get("worker", {}).get("session_id"):
        raise ValueError("verifier and repairer sessions must differ")
    result = decision["decision"]
    verdict = result["decision"]
    findings = result["findings"]
    if verdict == "PASS" and not (result["user_turns_unchanged"] and result["assistant_turns_all_acceptable"] and result["annotations_complete"] and not findings):
        raise ValueError("PASS violates verifier gates")
    if verdict == "FAIL" and not findings:
        raise ValueError("FAIL requires findings")
    if verdict == "ABSTAIN" and not findings:
        raise ValueError("ABSTAIN requires a missing-evidence finding")
    turn_ids = {turn["turn_id"] for turn in packet["turns"]}
    variant_ids = {path[2] for path in taxonomy_paths(registry)}
    for finding in findings:
        if finding["turn_id"] is not None and finding["turn_id"] not in turn_ids:
            raise ValueError("finding cites unknown turn")
        if finding["variant_id"] is not None and finding["variant_id"] not in variant_ids:
            raise ValueError("finding cites unknown variant")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a fresh graph-round GPT-5.6-sol verifier")
    parser.add_argument("--batch", type=Path, required=True)
    parser.add_argument("--index", type=int, required=True)
    parser.add_argument("--proposal", type=Path, required=True)
    parser.add_argument("--graph-run-id", required=True)
    parser.add_argument("--round", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[3]
    golden = project / "plugins" / "golden-conversations"
    packet = json.loads(args.batch.read_text(encoding="utf-8"))["result"]["packets"][args.index]
    proposal = json.loads(args.proposal.read_text(encoding="utf-8"))
    registry = json.loads((golden / "resources" / "taxonomy" / "zen-eval-taxonomy-2026-q2-v1.json").read_text(encoding="utf-8"))
    contract = (golden / "prompts" / "conversation-verifier.md").read_text(encoding="utf-8")
    assignment = assignment_id(packet["packet_id"], args.round)
    prompt = "\n\n".join((
        contract,
        "# Required identity\n"
        f"packet_id={packet['packet_id']}\nassignment_id={assignment}\n"
        f"principal_id=codex-golden-graph-verifier\nsession_id=graph-verify-{packet['packet_id'][-10:]}-r{args.round}\n"
        f"role=VERIFIER\nmodel_id={MODEL}\n"
        "Generate a valid rd_ decision_id. All evidence follows inline.",
        "# Governed taxonomy\n" + json.dumps(compact_taxonomy(registry), ensure_ascii=False, separators=(",", ":")),
        "# Source-bound packet\n" + json.dumps(packet, ensure_ascii=False, separators=(",", ":")),
        "# Blinded repair proposal\n" + json.dumps(proposal, ensure_ascii=False, separators=(",", ":")),
    ))
    codex = shutil.which("codex")
    if codex is None:
        raise RuntimeError("codex CLI unavailable")
    args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    args.log.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix="zen-graph-verify-") as workspace:
        command = [codex, "exec", "--ephemeral", "--ignore-user-config", "--ignore-rules", "--skip-git-repo-check", "--model", MODEL, "--sandbox", "read-only", "--cd", workspace, "--output-schema", str(golden / "schemas" / "verifier-response-v1.schema.json"), "--output-last-message", str(args.output.resolve()), "-"]
        complete = subprocess.run(command, input=prompt, text=True, capture_output=True, check=False, timeout=900)
    args.log.write_text(complete.stdout + "\n--- STDERR ---\n" + complete.stderr, encoding="utf-8")
    os.chmod(args.log, 0o600)
    if complete.returncode != 0:
        raise RuntimeError(f"verifier exited {complete.returncode}")
    decision = json.loads(args.output.read_text(encoding="utf-8"))
    validate(decision, packet, proposal, registry, args.round)
    os.chmod(args.output, 0o600)
    findings = decision["decision"]["findings"]
    # Which turns are actually implicated, so an exhausted repair can drop just
    # those instead of discarding a conversation that is otherwise sound.
    print(json.dumps({
        "packet_id": packet["packet_id"], "round": args.round,
        "decision": decision["decision"]["decision"],
        "findings": len(findings),
        "finding_turn_ids": sorted({f["turn_id"] for f in findings if f.get("turn_id")}),
        # Only a wrong assistant response justifies another rewrite. An
        # imprecise citation is corrected in review, not by re-generating a
        # sound answer.
        "blocking_turn_ids": sorted({
            f["turn_id"] for f in findings
            if f.get("turn_id") and f.get("severity") in {"CRITICAL", "MAJOR"}
            and f.get("scope", "GOLDEN_TEXT") == "GOLDEN_TEXT"
        }),
        "text_findings": sum(
            1 for f in findings if f.get("scope", "GOLDEN_TEXT") == "GOLDEN_TEXT"
        ),
        "metadata_findings": sum(
            1 for f in findings if f.get("scope", "GOLDEN_TEXT") != "GOLDEN_TEXT"
        ),
        "replay_required": decision["decision"]["replay_required"],
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
