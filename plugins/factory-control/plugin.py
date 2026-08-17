from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys

from zen_agent.models import ToolRisk
from zen_agent.tools import ToolSpec


def _inside(root: Path, value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    path = path.resolve()
    if root != path and root not in path.parents:
        raise PermissionError("path escapes harness workspace")
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


def _audit(context, inputs):
    batch = _inside(context.workspace, inputs["packet_batch"])
    wrapper = json.loads(batch.read_text(encoding="utf-8"))
    packets = wrapper.get("result", wrapper).get("packets")
    index = inputs["packet_index"]
    if not isinstance(packets, list) or not 0 <= index < len(packets):
        raise ValueError("invalid packet batch/index")
    packet = packets[index]
    job = context.workspace / ".zen" / "factory-jobs" / context.run_id / packet["packet_id"]
    job.mkdir(parents=True, exist_ok=True, mode=0o700)
    output = job / "agent-audit.json"
    log = job / "agent-audit.log"
    script = context.workspace / "plugins" / "factory-control" / "scripts" / "run_agent_auditor.py"
    completed = subprocess.run(
        [
            sys.executable, str(script), "--batch", str(batch), "--index", str(index),
            "--run-id", context.run_id, "--output", str(output), "--log", str(log),
        ],
        text=True, capture_output=True, check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout)[-2000:])
    summary = json.loads(completed.stdout)
    return {
        "stage": "AGENT_AUDIT",
        "model_id": "gpt-5.6-sol",
        "packet_id": packet["packet_id"],
        "decision_path": str(output.relative_to(context.workspace)),
        "decision_sha256": sha256(output.read_bytes()).hexdigest(),
        "summary": summary,
    }


def register(registry):
    registry.tools.register(ToolSpec(
        "factory.audit_conversation", "0.1.0",
        "Audit prompt coherence and workflow adherence for one source-bound conversation",
        ToolRisk.WORKSPACE_WRITE,
        {
            "type": "object", "required": ["packet_batch", "packet_index"],
            "additionalProperties": False,
            "properties": {
                "packet_batch": {"type": "string", "minLength": 1},
                "packet_index": {"type": "integer", "minimum": 0},
            },
        },
        {
            "type": "object",
            "required": ["stage", "model_id", "packet_id", "decision_path", "decision_sha256", "summary"],
            "additionalProperties": False,
            "properties": {
                "stage": {"type": "string", "enum": ["AGENT_AUDIT"]},
                "model_id": {"type": "string", "enum": ["gpt-5.6-sol"]},
                "packet_id": {"type": "string"},
                "decision_path": {"type": "string"},
                "decision_sha256": {"type": "string"},
                "summary": {"type": "object"},
            },
        },
        _audit,
    ))
