from __future__ import annotations

from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path

from zen_agent.models import Plan, TaskSpec, ToolRisk
from zen_agent.plugins import WorkflowSpec
from zen_agent.tools import ToolSpec


def _execution_module(root: Path):
    path = root / "plugins" / "golden-execution" / "plugin.py"
    spec = importlib.util.spec_from_file_location("zen_golden_execution_helpers", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load golden execution helpers")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _assemble(context, inputs):
    source_run_id = inputs["source_run_id"]
    if not source_run_id.isalnum() or len(source_run_id) > 64:
        raise ValueError("source_run_id is malformed")
    helpers = _execution_module(context.workspace)
    batch_path, wrapper = helpers._batch(context.workspace, inputs["packet_batch"])
    names = helpers._taxonomy_names(context.workspace)
    conversations = []
    counts = {"READY_FOR_HUMAN_REVIEW": 0, "QUARANTINED": 0}
    for index, packet in enumerate(wrapper["result"]["packets"]):
        job = context.workspace / ".zen" / "jobs" / source_run_id / packet["packet_id"]
        if not job.is_dir():
            raise FileNotFoundError(f"decision job is missing for packet {packet['packet_id']}")
        refiner_path = job / "refiner.json"
        verifier_path = job / "verifier.json"
        refiner = json.loads(refiner_path.read_text(encoding="utf-8"))
        verifier = json.loads(verifier_path.read_text(encoding="utf-8"))
        proposal = refiner["decision"]
        verification = verifier["decision"]
        # A divergent turn is dropped on its own; the conversation survives.
        excluded = {
            row["turn_id"]
            for row in proposal["assistant_turns"]
            if row.get("downstream_coherence") == "DIVERGENT"
            or row.get("evidence_status") == "INSUFFICIENT"
        }
        # quarantine_reasons is advisory prose; assessability is the real gate.
        ready = (
            proposal["prompt_usable"]
            and proposal.get("conversation_assessable", True)
            and verification["decision"] == "PASS"
            and len(proposal["assistant_turns"]) > len(excluded)
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
            if turn["role"] != "assistant":
                output_turns.append(
                    {
                        "turn_id": turn["turn_id"],
                        "role": turn["role"],
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
                    "golden_text": row["golden_text"],
                    "action": row["action"],
                    "semantic_delta": row["semantic_delta"],
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
                "excluded_turn_ids": sorted(excluded),
                "quarantine_reasons": proposal["quarantine_reasons"],
                "verifier": verification,
                "turns": output_turns,
            }
        )
    review = {
        "schema_version": "zen.golden-review-batch/1",
        "run_id": context.run_id,
        "decision_run_id": source_run_id,
        "source_packet_batch": {
            "path": str(batch_path.relative_to(context.workspace)),
            "sha256": helpers._sha(batch_path),
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
        "schema_version": "zen.review-assembly-repair/1",
        "review_path": str(target.relative_to(context.workspace)),
        "review_sha256": helpers._sha(target),
        "counts": review["counts"],
        "review_batch": review,
    }


def _plan(objective, inputs, _max_attempts):
    packet_batch = inputs.get("packet_batch")
    source_run_id = inputs.get("source_run_id")
    if not isinstance(packet_batch, str) or not packet_batch:
        raise ValueError("packet_batch is required")
    if not isinstance(source_run_id, str) or not source_run_id:
        raise ValueError("source_run_id is required")
    return Plan(
        "golden-assemble-existing-review",
        objective,
        (
            TaskSpec(
                "assemble-review",
                "Assemble review batch from completed verified decisions",
                "golden.reassemble_review_batch",
                {"packet_batch": packet_batch, "source_run_id": source_run_id},
                max_attempts=1,
            ),
        ),
        "Reuse completed GPT-5.6-sol decisions, preserve user and tool evidence, and assemble the protected review batch.",
        {"packet_batch": packet_batch, "source_run_id": source_run_id},
    )


def register(registry):
    registry.tools.register(
        ToolSpec(
            "golden.reassemble_review_batch",
            "0.1.0",
            "Assemble an audited review batch from completed verified decisions",
            ToolRisk.WORKSPACE_WRITE,
            {
                "type": "object",
                "required": ["packet_batch", "source_run_id"],
                "additionalProperties": False,
                "properties": {
                    "packet_batch": {"type": "string", "minLength": 1},
                    "source_run_id": {"type": "string", "minLength": 1},
                },
            },
            {
                "type": "object",
                "required": ["schema_version", "review_path", "review_sha256", "counts", "review_batch"],
                "additionalProperties": False,
                "properties": {
                    "schema_version": {"type": "string"},
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
            "golden-assemble-existing-review",
            "Assemble a review batch from an existing completed refinement run",
            ("assemble existing review", "repair review assembly"),
            _plan,
        )
    )
