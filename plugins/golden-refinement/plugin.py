from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

from zen_agent.models import Plan, TaskSpec, ToolRisk
from zen_agent.plugins import WorkflowSpec
from zen_agent.tools import ToolSpec
from zen_agent.turn_format import analyze_conversation, summarize


def _sha256(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _workspace_path(context, value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = context.workspace / path
    path = path.resolve()
    if context.workspace != path and context.workspace not in path.parents:
        raise PermissionError("path escapes the harness workspace")
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


def _load_json(path: Path):
    if path.stat().st_size > 50_000_000:
        raise ValueError("input artifact exceeds 50 MB packet-preparation bound")
    return json.loads(path.read_text(encoding="utf-8"))


def _prepare_packets(context, inputs):
    sample_path = _workspace_path(context, inputs["sample_artifact"])
    sample = _load_json(sample_path)
    conversations = sample.get("result", {}).get("conversations")
    if not isinstance(conversations, list) or not conversations:
        raise ValueError("sample artifact contains no conversations")
    if len(conversations) > 100:
        raise ValueError("one packet-preparation task may not exceed 100 conversations")

    root = context.workspace / "plugins" / "golden-conversations"
    registry_path = root / "resources" / "taxonomy" / "zen-eval-taxonomy-2026-q2-v1.json"
    registry = _load_json(registry_path)
    expected_source_sha = "e923c80119f9016c4508de2662a8dd776c4329d0615df461f9aeb4f893afc629"
    if registry.get("source", {}).get("sha256") != expected_source_sha:
        raise ValueError("taxonomy source checksum differs from the approved sheet")
    if registry.get("counts") != {
        "axes": 10, "subaxes": 35, "variants": 286, "warnings": 2
    }:
        raise ValueError("compiled taxonomy counts differ from the governed registry")

    prompt_names = (
        "agent-configuration-auditor.md",
        "conversation-refiner.md",
        "conversation-verifier.md",
        "human-review.md",
    )
    schema_names = (
        "agent-audit-decision-v1.schema.json",
        "refiner-decision-v1.schema.json",
        "verifier-decision-v1.schema.json",
        "refinement-packet-v1.schema.json",
    )
    prompts = {
        name: {"path": f"prompts/{name}", "sha256": _sha256(root / "prompts" / name)}
        for name in prompt_names
    }
    schemas = {
        name: {"path": f"schemas/{name}", "sha256": _sha256(root / "schemas" / name)}
        for name in schema_names
    }
    taxonomy_ref = {
        "taxonomy_id": registry["taxonomy_id"],
        "taxonomy_version": registry["taxonomy_version"],
        "source_sha256": expected_source_sha,
        "registry_sha256": _sha256(registry_path),
        "registry_path": str(registry_path.relative_to(context.workspace)),
        "counts": registry["counts"],
    }

    packets = []
    seen_sources = set()
    for conversation in conversations:
        source_hash = conversation.get("source_content_sha256")
        prompt_hash = conversation.get("system_prompt_sha256")
        system_prompt = conversation.get("system_prompt")
        turns = conversation.get("turns")
        if not isinstance(source_hash, str) or len(source_hash) != 64:
            raise ValueError("conversation has invalid source-content checksum")
        if source_hash in seen_sources:
            raise ValueError("sample contains duplicate source-content checksum")
        seen_sources.add(source_hash)
        if not isinstance(system_prompt, str) or not system_prompt:
            raise ValueError("conversation has no system prompt")
        if sha256(system_prompt.encode("utf-8")).hexdigest() != prompt_hash:
            raise ValueError("system-prompt checksum mismatch")
        if not isinstance(turns, list) or not turns:
            raise ValueError("conversation has no turns")

        normalized_turns = []
        user_hashes = []
        assistant_count = 0
        for turn in turns:
            text = turn.get("text")
            role = turn.get("role")
            source_index = turn.get("source_index")
            if not isinstance(text, str) or not isinstance(source_index, int):
                raise ValueError("turn text/index is malformed")
            observed = sha256(text.encode("utf-8")).hexdigest()
            if observed != turn.get("text_sha256"):
                raise ValueError("turn checksum mismatch")
            turn_id = f"turn_{source_index:04d}"
            entry = {
                    "turn_id": turn_id,
                    "source_index": source_index,
                    "role": role,
                    "text": text,
                    "text_sha256": observed,
            }
            # Carry the agent's tool invocations and the backend's replies through
            # verbatim; the model is being trained to make these calls.
            if turn.get("tool_calls"):
                entry["tool_calls"] = turn["tool_calls"]
                entry["tool_calls_sha256"] = turn.get("tool_calls_sha256")
            if turn.get("tool_call_id"):
                entry["tool_call_id"] = turn["tool_call_id"]
            normalized_turns.append(entry)
            if role == "user":
                user_hashes.append(observed)
            elif role == "assistant":
                assistant_count += 1
        if not user_hashes or not assistant_count:
            raise ValueError("conversation needs user and assistant turns")

        # Tag compliance is a mechanical string property. Decide it here so the
        # refiner spends its budget on conversational quality instead.
        format_findings = analyze_conversation(system_prompt, normalized_turns)
        findings_by_turn = {
            finding.turn_id: finding.as_dict() for finding in format_findings
        }
        for turn in normalized_turns:
            if turn["turn_id"] in findings_by_turn:
                turn["format"] = findings_by_turn[turn["turn_id"]]

        identity = json.dumps(
            {
                "source": source_hash,
                "prompt": prompt_hash,
                "taxonomy": taxonomy_ref["registry_sha256"],
                "prompts": prompts,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        packet_id = "rp_" + sha256(identity.encode("utf-8")).hexdigest()
        packets.append(
            {
                "schema_version": "zen.refinement-packet/1",
                "packet_id": packet_id,
                "source": {
                    "source_mongo_id": conversation.get("source_mongo_id"),
                    "source_mongo_id_type": conversation.get("source_mongo_id_type"),
                    "call_id": conversation.get("call_id"),
                    "agent_id": conversation.get("agent_id"),
                    "agent_version": conversation.get("agent_version"),
                    "system_prompt_sha256": prompt_hash,
                    "source_content_sha256": source_hash,
                    "privacy_status": "RESTRICTED_SOURCE_NOT_DEIDENTIFIED",
                },
                "taxonomy": taxonomy_ref,
                "prompts": prompts,
                "schemas": schemas,
                "system_prompt": system_prompt,
                "turns": normalized_turns,
                "user_turn_sha256": user_hashes,
                "assistant_turn_count": assistant_count,
                "format_compliance": summarize(format_findings),
                "status": "READY_FOR_AGENT_AUDIT",
            }
        )
    return {
        "schema_version": "zen.refinement-packet-batch/1",
        "source_sample": {
            "path": str(sample_path.relative_to(context.workspace)),
            "sha256": _sha256(sample_path),
        },
        "taxonomy": taxonomy_ref,
        "model_policy": {
            "allowed_model": "gpt-5.6-sol",
            "agent_auditor_session_must_be_fresh": True,
            "verifier_session_must_differ_from_refiner": True,
        },
        "packet_count": len(packets),
        "packets": packets,
    }


def _plan(objective, inputs, _max_attempts):
    sample_artifact = inputs.get("sample_artifact")
    if not isinstance(sample_artifact, str) or not sample_artifact:
        raise ValueError("sample_artifact is required")
    return Plan(
        "golden-prepare-refinement",
        objective,
        (
            TaskSpec(
                "prepare",
                "Prepare checksum-bound audit and refinement packets",
                "golden.prepare_refinement_packets",
                {"sample_artifact": sample_artifact},
                max_attempts=1,
            ),
        ),
        "Validate exact turns and governed taxonomy, pin role prompts/schemas, and prepare one agent-audit packet per conversation.",
        {"sample_artifact": sample_artifact},
    )


def register(registry):
    registry.tools.register(
        ToolSpec(
            "golden.prepare_refinement_packets",
            "0.1.0",
            "Prepare immutable GPT-5.6-sol audit/refinement packets from a source-bound sample",
            ToolRisk.READ_ONLY,
            {
                "type": "object",
                "required": ["sample_artifact"],
                "additionalProperties": False,
                "properties": {"sample_artifact": {"type": "string", "minLength": 1}},
            },
            {
                "type": "object",
                "required": ["schema_version", "source_sample", "taxonomy", "model_policy", "packet_count", "packets"],
                "additionalProperties": False,
                "properties": {
                    "schema_version": {"type": "string"},
                    "source_sample": {"type": "object"},
                    "taxonomy": {"type": "object"},
                    "model_policy": {"type": "object"},
                    "packet_count": {"type": "integer", "minimum": 1},
                    "packets": {"type": "array", "minItems": 1, "items": {"type": "object"}},
                },
            },
            _prepare_packets,
        )
    )
    registry.register_workflow(
        WorkflowSpec(
            "golden-prepare-refinement",
            "Prepare the next agent-audit and assistant-refinement tasks",
            ("prepare refinement", "audit conversations", "next golden steps"),
            _plan,
        )
    )
