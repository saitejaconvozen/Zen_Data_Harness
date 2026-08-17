from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys

from zen_agent.models import Plan, TaskSpec, ToolRisk
from zen_agent.plugins import WorkflowSpec
from zen_agent.tools import ToolSpec


def _sha(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _path(root: Path, value: str, *, must_exist: bool = True) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.resolve()
    if root != candidate and root not in candidate.parents:
        raise PermissionError("path escapes the harness workspace")
    if must_exist and not candidate.is_file():
        raise FileNotFoundError(candidate)
    return candidate


def _batch(root: Path, value: str) -> tuple[Path, dict]:
    path = _path(root, value)
    if path.stat().st_size > 50_000_000:
        raise ValueError("packet batch exceeds the 50 MB execution bound")
    wrapper = json.loads(path.read_text(encoding="utf-8"))
    packets = wrapper.get("result", {}).get("packets")
    if not isinstance(packets, list) or not packets:
        raise ValueError("artifact does not contain refinement packets")
    if len(packets) > 100:
        raise ValueError("one packet artifact is limited to 100 conversations")
    return path, wrapper


def _job_dir(context, packet_id: str) -> Path:
    target = context.workspace / ".zen" / "jobs" / context.run_id / packet_id
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(target, 0o700)
    return target


def _execute(command: list[str]) -> dict:
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(detail[-2000:] or f"worker exited {completed.returncode}")
    return json.loads(completed.stdout)


def _refine(context, inputs):
    batch_path, wrapper = _batch(context.workspace, inputs["packet_batch"])
    index = inputs["packet_index"]
    packet = wrapper["result"]["packets"][index]
    job = _job_dir(context, packet["packet_id"])
    output = job / "refiner.json"
    log = job / "refiner.log"
    script = context.workspace / "plugins" / "golden-conversations" / "scripts" / "run_refiner.py"
    summary = _execute(
        [
            sys.executable,
            str(script),
            "--batch",
            str(batch_path),
            "--index",
            str(index),
            "--output",
            str(output),
            "--log",
            str(log),
        ]
    )
    return {
        "stage": "REFINER",
        "model_id": "gpt-5.6-sol",
        "packet_index": index,
        "packet_id": packet["packet_id"],
        "decision_path": str(output.relative_to(context.workspace)),
        "decision_sha256": _sha(output),
        "summary": summary,
    }


def _verify(context, inputs):
    batch_path, wrapper = _batch(context.workspace, inputs["packet_batch"])
    index = inputs["packet_index"]
    packet = wrapper["result"]["packets"][index]
    job = _job_dir(context, packet["packet_id"])
    refiner = job / "refiner.json"
    if not refiner.is_file():
        raise FileNotFoundError("refiner decision is missing")
    output = job / "verifier.json"
    log = job / "verifier.log"
    script = context.workspace / "plugins" / "golden-conversations" / "scripts" / "run_verifier.py"
    summary = _execute(
        [
            sys.executable,
            str(script),
            "--batch",
            str(batch_path),
            "--index",
            str(index),
            "--refiner",
            str(refiner),
            "--output",
            str(output),
            "--log",
            str(log),
        ]
    )
    return {
        "stage": "VERIFIER",
        "model_id": "gpt-5.6-sol",
        "packet_index": index,
        "packet_id": packet["packet_id"],
        "decision_path": str(output.relative_to(context.workspace)),
        "decision_sha256": _sha(output),
        "summary": summary,
    }


def _taxonomy_names(root: Path) -> dict[tuple[str, str, str], dict[str, str]]:
    path = root / "plugins" / "golden-conversations" / "resources" / "taxonomy" / "zen-eval-taxonomy-2026-q2-v1.json"
    registry = json.loads(path.read_text(encoding="utf-8"))
    names = {}
    for axis in registry["axes"]:
        for subaxis in axis["subaxes"]:
            for variant in subaxis["variants"]:
                names[(axis["id"], subaxis["id"], variant["id"])] = {
                    "axis_name": axis["name"],
                    "subaxis_name": subaxis["name"],
                    "variant_name": variant["name"],
                }
    return names


def _assemble(context, inputs):
    batch_path, wrapper = _batch(context.workspace, inputs["packet_batch"])
    names = _taxonomy_names(context.workspace)
    conversations = []
    counts = {"READY_FOR_HUMAN_REVIEW": 0, "QUARANTINED": 0}
    for index, packet in enumerate(wrapper["result"]["packets"]):
        job = _job_dir(context, packet["packet_id"])
        refiner_path = job / "refiner.json"
        verifier_path = job / "verifier.json"
        refiner = json.loads(refiner_path.read_text(encoding="utf-8"))
        verifier = json.loads(verifier_path.read_text(encoding="utf-8"))
        proposal = refiner["decision"]
        verification = verifier["decision"]
        # Divergent turns are dropped individually; they no longer disqualify
        # the conversation that contains them.
        excluded = {
            row["turn_id"]
            for row in proposal["assistant_turns"]
            if row.get("downstream_coherence") == "DIVERGENT"
            or row.get("evidence_status") == "INSUFFICIENT"
        }
        usable_turns = len(proposal["assistant_turns"]) - len(excluded)
        # quarantine_reasons is advisory prose; assessability is the real gate.
        ready = (
            proposal["prompt_usable"]
            and proposal.get("conversation_assessable", True)
            and verification["decision"] == "PASS"
            and usable_turns > 0
        )
        status = "READY_FOR_HUMAN_REVIEW" if ready else "QUARANTINED"
        counts[status] += 1
        assistant_rows = {row["turn_id"]: row for row in proposal["assistant_turns"]}
        output_turns = []
        user_hashes = []
        for turn in packet["turns"]:
            if turn["role"] == "user":
                user_hashes.append(sha256(turn["text"].encode("utf-8")).hexdigest())
                output_turns.append(
                    {
                        "turn_id": turn["turn_id"],
                        "role": "user",
                        "text": turn["text"],
                        "text_sha256": turn["text_sha256"],
                        "source_preserved": True,
                    }
                )
                continue
            row = assistant_rows[turn["turn_id"]]
            citations = []
            for annotation in row["annotations"]:
                key = (annotation["axis_id"], annotation["subaxis_id"], annotation["variant_id"])
                citation = dict(annotation)
                citation.update(names.get(key) or {
                    "axis_name": annotation["axis_id"],
                    "subaxis_name": annotation["subaxis_id"],
                    "variant_name": annotation["variant_id"],
                })
                citation["taxonomy_path_resolved"] = key in names
                citations.append(citation)
            output_turns.append(
                {
                    "turn_id": turn["turn_id"],
                    "role": "assistant",
                    "source_text": turn["text"],
                    # golden_text_final carries the harness-applied language tag.
                    "golden_text": row.get("golden_text_final", row["golden_text"]),
                    "golden_text_model": row["golden_text"],
                    "action": row["action"],
                    "semantic_delta": row["semantic_delta"],
                    "source_quality": row.get("source_quality"),
                    "downstream_coherence": row.get("downstream_coherence"),
                    "excluded_from_golden": turn["turn_id"] in excluded,
                    "correction_reason": row["correction_reason"],
                    "metric_citations": citations,
                }
            )
        if user_hashes != packet["user_turn_sha256"]:
            raise ValueError("assembled user turns differ from source packet")
        conversations.append(
            {
                "packet_index": index,
                "packet_id": packet["packet_id"],
                "status": status,
                "source": packet["source"],
                "classification": proposal["classification"],
                "prompt_usable": proposal["prompt_usable"],
                "prompt_issues": proposal["prompt_issues"],
                "replay_required": proposal["replay_required"],
                "quarantine_reasons": proposal["quarantine_reasons"],
                "verifier": verification,
                "turns": output_turns,
            }
        )
    review = {
        "schema_version": "zen.golden-review-batch/1",
        "run_id": context.run_id,
        "source_packet_batch": {
            "path": str(batch_path.relative_to(context.workspace)),
            "sha256": _sha(batch_path),
        },
        "model_policy": {
            "refiner": "gpt-5.6-sol",
            "verifier": "gpt-5.6-sol",
            "independent_sessions": True,
        },
        "counts": {"total": len(conversations), **counts},
        "conversations": conversations,
    }
    target = context.workspace / ".zen" / "jobs" / context.run_id / "human-review-batch.json"
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.write_text(json.dumps(review, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(target, 0o600)
    return {
        "stage": "ASSEMBLE_HUMAN_REVIEW",
        "review_path": str(target.relative_to(context.workspace)),
        "review_sha256": _sha(target),
        "counts": review["counts"],
        "review_batch": review,
    }


def _planning_batch(value: str) -> tuple[Path, dict]:
    root = Path(__file__).resolve().parents[2]
    return _batch(root, value)


def _plan(objective, inputs, max_attempts):
    value = inputs.get("packet_batch")
    if not isinstance(value, str) or not value:
        raise ValueError("packet_batch is required")
    _path_value, wrapper = _planning_batch(value)
    tasks = []
    verify_keys = []
    for index, packet in enumerate(wrapper["result"]["packets"]):
        refine_key = f"refine-{index:03d}"
        verify_key = f"verify-{index:03d}"
        common = {"packet_batch": value, "packet_index": index}
        tasks.append(
            TaskSpec(
                refine_key,
                f"Assess and refine packet {index}: {packet['packet_id']}",
                "golden.refine_one",
                common,
                max_attempts=max_attempts,
            )
        )
        tasks.append(
            TaskSpec(
                verify_key,
                f"Independently verify packet {index}: {packet['packet_id']}",
                "golden.verify_one",
                common,
                depends_on=(refine_key,),
                max_attempts=max_attempts,
            )
        )
        verify_keys.append(verify_key)
    tasks.append(
        TaskSpec(
            "assemble-review",
            "Assemble protected human-review batch",
            "golden.assemble_review_batch",
            {"packet_batch": value},
            depends_on=tuple(verify_keys),
            max_attempts=1,
        )
    )
    return Plan(
        "golden-refine-and-verify",
        objective,
        tuple(tasks),
        "Run one fresh GPT-5.6-sol refiner and one independent verifier per packet, then assemble only verified candidates for human review.",
        {"packet_batch": value},
    )


_COMMON_INPUT = {
    "type": "object",
    "required": ["packet_batch", "packet_index"],
    "additionalProperties": False,
    "properties": {
        "packet_batch": {"type": "string", "minLength": 1},
        "packet_index": {"type": "integer", "minimum": 0},
    },
}

_STAGE_OUTPUT = {
    "type": "object",
    "required": ["stage", "model_id", "packet_index", "packet_id", "decision_path", "decision_sha256", "summary"],
    "additionalProperties": False,
    "properties": {
        "stage": {"type": "string"},
        "model_id": {"type": "string", "enum": ["gpt-5.6-sol"]},
        "packet_index": {"type": "integer"},
        "packet_id": {"type": "string"},
        "decision_path": {"type": "string"},
        "decision_sha256": {"type": "string"},
        "summary": {"type": "object"},
    },
}


def register(registry):
    registry.tools.register(
        ToolSpec(
            "golden.refine_one",
            "0.1.0",
            "Assess every assistant turn and produce taxonomy-cited golden candidates with GPT-5.6-sol",
            ToolRisk.WORKSPACE_WRITE,
            _COMMON_INPUT,
            _STAGE_OUTPUT,
            _refine,
        )
    )
    registry.tools.register(
        ToolSpec(
            "golden.verify_one",
            "0.1.0",
            "Independently verify a refinement in a fresh GPT-5.6-sol session",
            ToolRisk.WORKSPACE_WRITE,
            _COMMON_INPUT,
            _STAGE_OUTPUT,
            _verify,
        )
    )
    registry.tools.register(
        ToolSpec(
            "golden.assemble_review_batch",
            "0.1.0",
            "Assemble source-preserving, metric-cited candidates for human review",
            ToolRisk.WORKSPACE_WRITE,
            {
                "type": "object",
                "required": ["packet_batch"],
                "additionalProperties": False,
                "properties": {"packet_batch": {"type": "string", "minLength": 1}},
            },
            {
                "type": "object",
                "required": ["stage", "review_path", "review_sha256", "counts", "review_batch"],
                "additionalProperties": False,
                "properties": {
                    "stage": {"type": "string"},
                    "review_path": {"type": "string"},
                    "review_sha256": {"type": "string"},
                    "counts": {"type": "object"},
                    "review_batch": {"type": "object"},
                },
            },
            _assemble,
        )
    )
    registry.register_workflow(
        WorkflowSpec(
            "golden-refine-and-verify",
            "Autonomously assess, refine, independently verify, and assemble a human-review batch",
            ("refine conversations", "assess conversations", "golden conversations", "verify refinements"),
            _plan,
        )
    )
