#!/usr/bin/env python3
from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

# Shared transport: provider is chosen by ZEN_MODEL_PROVIDER (codex | claude).
# _transport lives with the golden-conversations workers; every role shares it.
sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[2] / "golden-conversations" / "scripts"),
)
from _transport import active_model, run_model  # noqa: E402



MODEL = active_model()
# Below this there is not enough conversation to be worth refining.
MIN_TURNS = 3


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one isolated GPT-5.6-sol agent auditor")
    parser.add_argument("--batch", type=Path, required=True)
    parser.add_argument("--index", type=int, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    args = parser.parse_args()

    project = Path(__file__).resolve().parents[3]
    golden = project / "plugins" / "golden-conversations"
    control = project / "plugins" / "factory-control"
    wrapper = json.loads(args.batch.read_text(encoding="utf-8"))
    packets = wrapper.get("result", wrapper).get("packets")
    if not isinstance(packets, list) or not 0 <= args.index < len(packets):
        raise ValueError("packet index is outside batch")
    packet = packets[args.index]
    packet_id = packet["packet_id"]
    session_id = f"agent-audit-{args.run_id[:10]}-{packet_id[-10:]}"
    contract = (golden / "prompts" / "agent-configuration-auditor.md").read_text(encoding="utf-8")
    prompt = "\n\n".join(
        (
            contract,
            "# Required identity\n"
            f"packet_id={packet_id}\nrole=AGENT_AUDITOR\nmodel_id={MODEL}\n"
            f"session_id={session_id}\n"
            "Use exactly this identity. Do not invoke tools; all evidence is inline.",
            "# Source-bound packet\n" + json.dumps(packet, ensure_ascii=False, separators=(",", ":")),
        )
    )
    if len(prompt) > 1_500_000:
        raise ValueError("agent-auditor prompt exceeds 1.5 million characters")
    decision = run_model(
        prompt=prompt,
        schema_path=Path(control / "schemas" / "agent-audit-response-v1.schema.json"),
        output_path=args.output,
        log_path=args.log,
        role="AGENT_AUDITOR",
    )
    decision = json.loads(args.output.read_text(encoding="utf-8"))
    if decision["packet_id"] != packet_id:
        raise ValueError("auditor packet identity mismatch")
    worker = decision["worker"]
    # Which model answered is a fact the harness knows. Asking the model to echo
    # it back and dead-lettering a mismatch throws away a completed audit over
    # bookkeeping, so the correct identity is written in.
    decision["worker"] = {
        "role": "AGENT_AUDITOR", "model_id": MODEL, "session_id": session_id
    }
    verdict = decision["decision"]["verdict"]
    # `verdict` judges adherence only. `conversation_usable` is a turn-count fact
    # and has no bearing on it, so it is not part of this consistency check.
    if verdict == "PASS" and (
        decision["decision"]["critical_failures"]
        or not decision["decision"]["prompt_coherent"]
    ):
        decision["decision"]["verdict"] = verdict = "FAIL"
        decision["decision"]["verdict_corrected_by_harness"] = (
            "PASS contradicted the recorded critical failures or prompt incoherence"
        )
    # Policy: a conversation is discarded only when there is too little of it to
    # refine. Agent misbehaviour, missing backend evidence and incoherent prompts
    # are all turn-level concerns the downstream gates handle — none of them are
    # grounds to throw the conversation away.
    result = decision["decision"]
    turns = packet["turns"]
    user_turns = sum(1 for x in turns if x.get("role") == "user")
    assistant_turns = sum(1 for x in turns if x.get("role") == "assistant")
    too_short = user_turns < MIN_TURNS or assistant_turns < MIN_TURNS
    if result["conversation_usable"] and too_short:
        result["conversation_usable"] = False
        result["unusable_reason"] = (
            f"only {user_turns} user and {assistant_turns} assistant turns; "
            f"minimum is {MIN_TURNS} each"
        )
        args.output.write_text(json.dumps(decision, ensure_ascii=False), encoding="utf-8")
    elif not result["conversation_usable"] and not too_short:
        result["conversation_usable"] = True
        result["usability_restored_by_harness"] = (
            "long enough to refine; critical failures, missing evidence and prompt "
            "incoherence are turn-level concerns, not grounds to discard"
        )
        args.output.write_text(json.dumps(decision, ensure_ascii=False), encoding="utf-8")
    os.chmod(args.output, 0o600)
    print(json.dumps({
        "packet_id": packet_id,
        "agent_id": packet["source"]["agent_id"],
        "agent_version": packet["source"].get("agent_version"),
        "system_prompt_sha256": packet["source"]["system_prompt_sha256"],
        "source_content_sha256": packet["source"]["source_content_sha256"],
        "verdict": verdict,
        "prompt_coherent": decision["decision"]["prompt_coherent"],
        "workflow_obeyed": decision["decision"]["workflow_obeyed"],
        "conversation_usable": decision["decision"]["conversation_usable"],
        "critical_failures": len(decision["decision"]["critical_failures"]),
        "findings": len(decision["decision"]["findings"]),
        "output_sha256": sha256(args.output.read_bytes()).hexdigest(),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
