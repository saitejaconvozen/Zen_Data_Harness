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
import sys
import tempfile

from run_refiner import MODEL, compact_taxonomy, taxonomy_paths

# Shared transport: provider is chosen by ZEN_MODEL_PROVIDER (codex | claude).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _transport import active_model, run_model  # noqa: E402



ID_RE = re.compile(r"^(?:rd|rp|asg)_[0-9a-f]{64}$")


def assignment_id(packet_id: str) -> str:
    digest = sha256(f"{packet_id}:VERIFIER:v1".encode("utf-8")).hexdigest()
    return "asg_" + digest


def validate_decision(
    decision: dict, packet: dict, refiner: dict, registry: dict
) -> None:
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
    if worker.get("role") != "VERIFIER":
        raise ValueError("worker role/model mismatch")
    if worker.get("session_id") == refiner.get("worker", {}).get("session_id"):
        raise ValueError("verifier session must differ from refiner session")

    result = decision.get("decision", {})
    verdict = result.get("decision")
    findings = result.get("findings")
    if not isinstance(findings, list):
        raise ValueError("findings must be an array")
    if verdict == "PASS":
        # Divergence is recorded per turn and excluded downstream, so it no
        # longer blocks a PASS. User immutability and turn quality still do.
        if not (
            result.get("user_turns_unchanged") is True
            and result.get("assistant_turns_all_acceptable") is True
            and result.get("annotations_complete") is True
            and not findings
        ):
            raise ValueError("PASS does not satisfy all verifier gates")

    turn_ids = {turn["turn_id"] for turn in packet["turns"]}
    variant_ids = {path[2] for path in taxonomy_paths(registry)}
    for finding in findings:
        if finding["turn_id"] is not None and finding["turn_id"] not in turn_ids:
            raise ValueError("finding cites an unknown turn")
        if finding["variant_id"] is not None and finding["variant_id"] not in variant_ids:
            raise ValueError("finding cites an unknown variant")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one isolated GPT-5.6-sol verifier")
    parser.add_argument("--batch", type=Path, required=True)
    parser.add_argument("--index", type=int, required=True)
    parser.add_argument("--refiner", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    args = parser.parse_args()

    project = Path(__file__).resolve().parents[3]
    golden = project / "plugins" / "golden-conversations"
    batch_wrapper = json.loads(args.batch.read_text(encoding="utf-8"))
    packet = batch_wrapper["result"]["packets"][args.index]
    refiner = json.loads(args.refiner.read_text(encoding="utf-8"))
    registry = json.loads(
        (golden / "resources" / "taxonomy" / "zen-eval-taxonomy-2026-q2-v1.json").read_text(encoding="utf-8")
    )
    contract = (golden / "prompts" / "conversation-verifier.md").read_text(encoding="utf-8")
    assignment = assignment_id(packet["packet_id"])
    prompt = "\n\n".join(
        (
            contract,
            "# Required worker identity\n"
            f"packet_id={packet['packet_id']}\nassignment_id={assignment}\n"
            f"principal_id=codex-golden-verifier\nsession_id=verifier-{packet['packet_id'][-12:]}-v1\n"
            f"role=VERIFIER\nmodel_id={MODEL}\n"
            "A decision_id is supplied by the harness; emit any rd_ value. Do not use tools; all evidence follows inline.",
            "# Governed active taxonomy JSON\n" + json.dumps(compact_taxonomy(registry), ensure_ascii=False, separators=(",", ":")),
            "# Source-bound refinement packet JSON\n" + json.dumps(packet, ensure_ascii=False, separators=(",", ":")),
            "# Blinded proposed refiner decision JSON\n" + json.dumps(refiner, ensure_ascii=False, separators=(",", ":")),
        )
    )
    if len(prompt) > 2_000_000:
        raise ValueError("inline verifier prompt exceeds 2 million characters")

    decision = run_model(
        prompt=prompt,
        schema_path=Path(golden / "schemas" / "verifier-response-v1.schema.json"),
        output_path=args.output,
        log_path=args.log,
        role="VERIFIER",
    )
    decision = json.loads(args.output.read_text(encoding="utf-8"))
    validate_decision(decision, packet, refiner, registry)
    os.chmod(args.output, 0o600)
    summary = {
        "packet_index": args.index,
        "packet_id": packet["packet_id"],
        "decision": decision["decision"]["decision"],
        "findings": len(decision["decision"]["findings"]),
        "replay_required": decision["decision"]["replay_required"],
        "output": str(args.output),
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
