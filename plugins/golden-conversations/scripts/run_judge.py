#!/usr/bin/env python3
"""Run one isolated GPT-5.6-sol judge over a refined conversation.

The verifier asks "is this proposal valid?" and has been observed answering yes
to edits that destroyed good answers. The judge asks a different question — "is
the golden version better than the source?" — from a fresh context that never
sees the refiner's reasoning. Different question, different failure modes, so the
signal is genuinely independent rather than a second opinion on the same test.
"""

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
# The prompts run to 49 KB; the judge needs the rules, not the whole document.
PROMPT_EXCERPT_CHARS = 12_000


def session_id(packet_id: str) -> str:
    return "judge-" + sha256(f"{packet_id}:JUDGE:v1".encode()).hexdigest()[:16]


def build_case(conversation: dict, system_prompt: str) -> dict:
    """Only the turns that changed, each with the context needed to judge it."""

    turns = conversation.get("turns") or []
    changed = []
    for index, turn in enumerate(turns):
        if turn.get("role") != "assistant":
            continue
        tool_changed = turn.get("golden_tool_calls") != turn.get("source_tool_calls")
        if turn.get("action") != "REPLACE" and not tool_changed:
            continue
        prior = [
            {"turn_id": t["turn_id"], "role": t["role"],
             "text": (t.get("text") or t.get("source_text") or "")[:600]}
            for t in turns[max(0, index - 4):index]
        ]
        following = next(
            (t for t in turns[index + 1:] if t.get("role") == "user"), None
        )
        changed.append({
            "turn_id": turn.get("turn_id"),
            "prior_context": prior,
            "source_text": turn.get("source_text"),
            "golden_text": turn.get("golden_text"),
            "recorded_next_user_turn": (following or {}).get("text"),
            "source_tool_calls": turn.get("source_tool_calls"),
            "golden_tool_calls": turn.get("golden_tool_calls"),
            "pipeline_claimed_quality": turn.get("source_quality"),
            "pipeline_claimed_coherence": turn.get("downstream_coherence"),
        })
    return {
        "source_id": conversation.get("source_id"),
        "domain": (conversation.get("classification") or {}).get("domain"),
        "system_prompt_excerpt": (system_prompt or "")[:PROMPT_EXCERPT_CHARS],
        "changed_turns": changed,
    }


def judge(case: dict, packet_id: str, golden_root: Path, timeout: int = 900) -> dict:
    codex = shutil.which("codex")
    if codex is None:
        raise RuntimeError("codex CLI is unavailable")
    contract = (golden_root / "prompts" / "refinement-judge.md").read_text(encoding="utf-8")
    schema_path = golden_root / "schemas" / "judge-response-v1.schema.json"
    session = session_id(packet_id)
    prompt = "\n\n".join((
        contract,
        "# Required identity\n"
        f"role=JUDGE\nmodel_id={MODEL}\nsession_id={session}\n"
        "Use exactly this identity. All evidence follows inline.",
        "# Conversation under audit\n"
        + json.dumps(case, ensure_ascii=False, separators=(",", ":")),
    ))
    with tempfile.TemporaryDirectory(prefix="zen-judge-") as workspace:
        output = Path(workspace) / "judgement.json"
        command = [
            codex, "exec", "--ephemeral", "--ignore-user-config", "--ignore-rules",
            "--skip-git-repo-check", "--model", MODEL, "--sandbox", "read-only",
            "--cd", workspace, "--output-schema", str(schema_path),
            "--output-last-message", str(output), "-",
        ]
        completed = subprocess.run(
            command, input=prompt, text=True, capture_output=True,
            check=False, timeout=timeout,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"judge exited {completed.returncode}: "
                + (completed.stderr or completed.stdout)[-1500:]
            )
        decision = json.loads(output.read_text(encoding="utf-8"))
    worker = decision.get("worker", {})
    if worker.get("role") != "JUDGE" or worker.get("model_id") != MODEL:
        raise ValueError("judge worker identity mismatch")
    expected = {t["turn_id"] for t in case["changed_turns"]}
    seen = {t["turn_id"] for t in decision["judgement"]["turns"]}
    if seen - expected:
        raise ValueError(f"judge invented turns: {sorted(seen - expected)}")
    return decision


def main() -> int:
    parser = argparse.ArgumentParser(description="Judge one refined conversation")
    parser.add_argument("--case", type=Path, required=True,
                        help="JSON file holding {conversation, system_prompt}")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = json.loads(args.case.read_text(encoding="utf-8"))
    conversation = payload["conversation"]
    case = build_case(conversation, payload.get("system_prompt", ""))
    if not case["changed_turns"]:
        result = {
            "schema_version": "zen.refinement-judgement/1",
            "worker": {"role": "JUDGE", "model_id": MODEL,
                       "session_id": session_id(conversation.get("packet_id", ""))},
            "judgement": {"conversation_verdict": "USE",
                          "summary": "no assistant turn was changed",
                          "turns": []},
        }
    else:
        golden = Path(__file__).resolve().parents[1]
        result = judge(case, conversation.get("packet_id", ""), golden)
    args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    args.output.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    os.chmod(args.output, 0o600)
    print(json.dumps({
        "source_id": conversation.get("source_id"),
        "verdict": result["judgement"]["conversation_verdict"],
        "turns_judged": len(result["judgement"]["turns"]),
        "harmful": sum(1 for t in result["judgement"]["turns"] if t["verdict"] == "HARMFUL"),
        "unnecessary": sum(1 for t in result["judgement"]["turns"] if t["verdict"] == "UNNECESSARY"),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
