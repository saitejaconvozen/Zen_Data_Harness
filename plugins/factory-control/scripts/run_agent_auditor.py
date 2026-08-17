#!/usr/bin/env python3
from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


MODEL = "gpt-5.6-sol"


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
    codex = shutil.which("codex")
    if codex is None:
        raise RuntimeError("codex CLI is unavailable")
    args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    args.log.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix="zen-agent-auditor-") as directory:
        command = [
            codex, "exec", "--ephemeral", "--ignore-user-config", "--ignore-rules",
            "--skip-git-repo-check", "--model", MODEL, "--sandbox", "read-only",
            "--cd", directory,
            "--output-schema", str(control / "schemas" / "agent-audit-response-v1.schema.json"),
            "--output-last-message", str(args.output.resolve()), "-",
        ]
        completed = subprocess.run(
            command, input=prompt, text=True, capture_output=True, check=False, timeout=900
        )
    args.log.write_text(completed.stdout + "\n--- STDERR ---\n" + completed.stderr, encoding="utf-8")
    os.chmod(args.log, 0o600)
    if completed.returncode != 0:
        raise RuntimeError(f"Codex agent auditor failed with code {completed.returncode}")
    decision = json.loads(args.output.read_text(encoding="utf-8"))
    if decision["packet_id"] != packet_id:
        raise ValueError("auditor packet identity mismatch")
    worker = decision["worker"]
    if worker != {"role": "AGENT_AUDITOR", "model_id": MODEL, "session_id": session_id}:
        raise ValueError("auditor worker identity mismatch")
    verdict = decision["decision"]["verdict"]
    if verdict == "PASS" and (
        decision["decision"]["critical_failures"]
        or not decision["decision"]["prompt_coherent"]
        or not decision["decision"]["conversation_usable"]
    ):
        raise ValueError("PASS contradicts audit evidence")
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
