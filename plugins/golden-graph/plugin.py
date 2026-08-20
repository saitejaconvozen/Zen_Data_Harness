from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys

from zen_agent.graph import GraphEdgeSpec, GraphNodeSpec, GraphPlan, GraphSpec, LaneSpec
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


def _load(path: Path) -> dict:
    if path.stat().st_size > 100_000_000:
        raise ValueError("graph input exceeds 100 MB")
    return json.loads(path.read_text(encoding="utf-8"))


def _job(context, packet_id: str, round_number: int) -> Path:
    path = context.workspace / ".zen" / "graph-jobs" / context.run_id / packet_id / f"round-{round_number:02d}"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    return path


def _execute(command: list[str]) -> dict:
    complete = subprocess.run(command, text=True, capture_output=True, check=False)
    if complete.returncode != 0:
        detail = (complete.stderr or complete.stdout).strip()[-2000:]
        raise RuntimeError(detail or f"worker exited {complete.returncode}")
    return json.loads(complete.stdout)


def _packet(inputs: dict, root: Path) -> tuple[Path, dict]:
    batch = _inside(root, inputs["packet_batch"])
    wrapper = _load(batch)
    packet = wrapper["result"]["packets"][inputs["packet_index"]]
    if packet["packet_id"] != inputs["packet_id"]:
        raise ValueError("lane packet identity mismatch")
    return batch, packet


def _repair(context, inputs):
    batch, packet = _packet(inputs, context.workspace)
    round_number = inputs["round_number"]
    job = _job(context, packet["packet_id"], round_number)
    script = context.workspace / "plugins" / "golden-conversations" / "scripts" / "run_repairer.py"
    summary = _execute(
        [
            sys.executable,
            str(script),
            "--batch", str(batch),
            "--index", str(inputs["packet_index"]),
            "--source-run-id", inputs["source_decision_run_id"],
            "--graph-run-id", context.run_id,
            "--round", str(round_number),
            "--output", str(job / "repair.json"),
            "--log", str(job / "repair.log"),
        ]
    )
    return {"route": "PROPOSED", "packet_id": packet["packet_id"], "round": round_number, "summary": summary}


def _human_feedback_repair(context, inputs):
    batch, packet = _packet(inputs, context.workspace)
    binding = inputs["source_binding"]
    if packet.get("source", {}).get("source_content_sha256") != binding["source_content_sha256"]:
        raise ValueError("human feedback source binding mismatch")
    observed_user_hashes = [
        sha256(turn["text"].encode("utf-8")).hexdigest()
        for turn in packet["turns"] if turn["role"] == "user"
    ]
    if observed_user_hashes != binding["user_turn_sha256"]:
        raise ValueError("human feedback changed immutable user-turn bindings")
    feedback = inputs["human_feedback"]
    assistant_ids = {
        turn["turn_id"] for turn in packet["turns"] if turn["role"] == "assistant"
    }
    targets = feedback.get("targets", [])
    if (
        feedback.get("schema_version") != "zen.human-feedback/1"
        or not feedback.get("decision_id")
        or not feedback.get("reviewer_id")
        or not targets
        or any(target.get("turn_id") not in assistant_ids for target in targets)
    ):
        raise ValueError("invalid assistant-turn human feedback")
    round_number = inputs["round_number"]
    job = _job(context, packet["packet_id"], round_number)
    feedback_path = job / "human-feedback.json"
    feedback_path.write_text(json.dumps(feedback, indent=2), encoding="utf-8")
    os.chmod(feedback_path, 0o600)
    script = context.workspace / "plugins" / "golden-conversations" / "scripts" / "run_repairer.py"
    summary = _execute([
        sys.executable, str(script), "--batch", str(batch),
        "--index", str(inputs["packet_index"]),
        "--source-run-id", inputs["source_decision_run_id"],
        "--graph-run-id", context.run_id, "--round", str(round_number),
        "--human-feedback", str(feedback_path),
        "--output", str(job / "repair.json"), "--log", str(job / "repair.log"),
    ])
    return {"route": "PROPOSED", "packet_id": packet["packet_id"], "round": round_number, "summary": summary}


def _trajectory(context, inputs):
    _batch_path, packet = _packet(inputs, context.workspace)
    round_number = inputs["round_number"]
    decision = _load(_job(context, packet["packet_id"], round_number) / "repair.json")["decision"]
    assistant_source_index = {
        turn["turn_id"]: turn["source_index"]
        for turn in packet["turns"]
        if turn["role"] == "assistant"
    }
    user_indexes = [turn["source_index"] for turn in packet["turns"] if turn["role"] == "user"]
    unsafe = []
    for row in decision["assistant_turns"]:
        # Prefer the refiner's own per-turn judgement when the decision carries
        # it; fall back to the structural heuristic for older decisions.
        coherence = row.get("downstream_coherence")
        if coherence is not None:
            if coherence == "DIVERGENT":
                unsafe.append(row["turn_id"])
            continue
        changed_trajectory = row["action"] == "REPLACE" and row["semantic_delta"] in {
            "DIALOGUE_ACT", "FACT", "TOOL_ACTION", "WORKFLOW_STATE"
        }
        later_real_user = any(index > assistant_source_index[row["turn_id"]] for index in user_indexes)
        if changed_trajectory and later_real_user:
            unsafe.append(row["turn_id"])
    # An unsafe turn is excluded on its own. The gate only fails closed when
    # nothing is left to keep, which is what "invalidates the trajectory" means.
    total = len(decision["assistant_turns"])
    if unsafe and len(unsafe) >= total:
        route = "REPLAY_REQUIRED"
        reason = "every repaired assistant turn precedes an immutable real user turn"
    elif unsafe:
        route = "SAFE"
        reason = (
            f"{len(unsafe)} of {total} repaired turns excluded; the remainder are "
            "trajectory-compatible with preserved user turns"
        )
    else:
        route = "SAFE"
        reason = "repairs are trajectory-compatible with preserved user turns"
    record = {
        "schema_version": "zen.trajectory-gate/1",
        "packet_id": packet["packet_id"],
        "round": round_number,
        "route": route,
        "reason": reason,
        "unsafe_turn_ids": unsafe,
        "excluded_turn_ids": unsafe if route == "SAFE" else [],
        "user_turns_remain_immutable": True,
    }
    path = _job(context, packet["packet_id"], round_number) / "trajectory.json"
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    os.chmod(path, 0o600)
    return record


def _verify(context, inputs):
    batch, packet = _packet(inputs, context.workspace)
    round_number = inputs["round_number"]
    job = _job(context, packet["packet_id"], round_number)
    script = context.workspace / "plugins" / "golden-conversations" / "scripts" / "run_graph_verifier.py"
    summary = _execute(
        [
            sys.executable,
            str(script),
            "--batch", str(batch),
            "--index", str(inputs["packet_index"]),
            "--proposal", str(job / "repair.json"),
            "--graph-run-id", context.run_id,
            "--round", str(round_number),
            "--output", str(job / "verifier.json"),
            "--log", str(job / "verifier.log"),
        ]
    )
    verdict = summary["decision"]
    route = "EXHAUSTED" if verdict == "FAIL" and round_number + 1 >= inputs["max_rounds"] else verdict
    return {
        "route": route, "packet_id": packet["packet_id"], "round": round_number,
        "finding_turn_ids": summary.get("finding_turn_ids", []),
        "blocking_turn_ids": summary.get("blocking_turn_ids", []),
        "text_findings": summary.get("text_findings", 0),
        "metadata_findings": summary.get("metadata_findings", 0),
        "summary": summary,
    }


def _terminal(context, inputs):
    _batch_path, packet = _packet(inputs, context.workspace)
    round_number = inputs["round_number"]
    status = inputs["terminal_status"]
    record = {
        "schema_version": "zen.graph-lane-terminal/1",
        "packet_id": packet["packet_id"],
        "round": round_number,
        "status": status,
        "route": "DONE",
        "graph_run_id": context.run_id,
        "lineage_root_run_id": inputs["source_decision_run_id"],
    }
    path = _job(context, packet["packet_id"], round_number) / "terminal.json"
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    os.chmod(path, 0o600)
    return record


_BASE_INPUT = {
    "type": "object",
    "required": ["packet_batch", "packet_index", "packet_id", "source_decision_run_id", "round_number"],
    "additionalProperties": False,
    "properties": {
        "packet_batch": {"type": "string", "minLength": 1},
        "packet_index": {"type": "integer", "minimum": 0},
        "packet_id": {"type": "string", "minLength": 1},
        "source_decision_run_id": {"type": "string", "minLength": 1},
        "round_number": {"type": "integer", "minimum": 0},
    },
}


_HUMAN_FEEDBACK_INPUT = json.loads(json.dumps(_BASE_INPUT))
_HUMAN_FEEDBACK_INPUT["required"].extend(["human_feedback", "source_binding"])
_HUMAN_FEEDBACK_INPUT["properties"]["human_feedback"] = {
    "type": "object",
    "required": ["schema_version", "decision_id", "reviewer_id", "approved_at", "targets"],
    "additionalProperties": False,
    "properties": {
        "schema_version": {"const": "zen.human-feedback/1"},
        "decision_id": {"type": "string", "minLength": 1},
        "reviewer_id": {"type": "string", "minLength": 1},
        "approved_at": {"type": "string", "minLength": 1},
        "targets": {"type": "array", "minItems": 1, "items": {
            "type": "object", "required": ["turn_id", "instruction"],
            "additionalProperties": False, "properties": {
                "turn_id": {"type": "string", "minLength": 1},
                "instruction": {"type": "string", "minLength": 1, "maxLength": 4000},
            }
        }},
    },
}
_HUMAN_FEEDBACK_INPUT["properties"]["source_binding"] = {
    "type": "object",
    "required": ["source_content_sha256", "user_turn_sha256"],
    "additionalProperties": False,
    "properties": {
        "source_content_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "user_turn_sha256": {"type": "array", "minItems": 1, "items": {"type": "string", "pattern": "^[0-9a-f]{64}$"}},
    },
}


def _route_schema(extra_required=(), extra_properties=None):
    properties = {
        "schema_version": {"type": "string", "minLength": 1},
        "route": {"type": "string", "minLength": 1},
        "packet_id": {"type": "string", "minLength": 1},
        "round": {"type": "integer", "minimum": 0},
    }
    properties.update(extra_properties or {})
    return {
        "type": "object",
        "required": ["route", "packet_id", "round", *extra_required],
        "additionalProperties": False,
        "properties": properties,
    }


def _graph_plan(objective: str, inputs: dict) -> GraphPlan:
    root = Path(__file__).resolve().parents[2]
    packet_batch = inputs.get("packet_batch")
    review_batch = inputs.get("review_batch")
    source_run = inputs.get("source_decision_run_id")
    if not all(isinstance(value, str) and value for value in (packet_batch, review_batch, source_run)):
        raise ValueError("packet_batch, review_batch, and source_decision_run_id are required")
    packets = _load(_inside(root, packet_batch))["result"]["packets"]
    review = _load(_inside(root, review_batch))
    packet_index = {packet["packet_id"]: index for index, packet in enumerate(packets)}
    candidates = [item for item in review["conversations"] if item["status"] == "QUARANTINED"]
    if not candidates:
        raise ValueError("review batch contains no quarantined conversations to repair")
    max_rounds = int(inputs.get("max_rounds", 3))
    max_workers = int(inputs.get("max_workers", 3))
    lanes = tuple(
        LaneSpec(
            key=item["packet_id"],
            payload={
                "packet_batch": packet_batch,
                "packet_index": packet_index[item["packet_id"]],
                "packet_id": item["packet_id"],
                "source_decision_run_id": source_run,
            },
        )
        for item in candidates
    )
    common = {
        "packet_batch": {"$lane": "packet_batch"},
        "packet_index": {"$lane": "packet_index"},
        "packet_id": {"$lane": "packet_id"},
        "source_decision_run_id": {"$lane": "source_decision_run_id"},
        "round_number": {"$round": True},
    }
    nodes = (
        GraphNodeSpec("repair", "REPAIRER", "golden.graph_repair", common, priority=10),
        GraphNodeSpec("trajectory", "TRAJECTORY_GATE", "golden.graph_trajectory_gate", common, max_attempts=1, priority=30),
        GraphNodeSpec("verify", "VERIFIER", "golden.graph_verify", {**common, "max_rounds": max_rounds}, priority=20),
        GraphNodeSpec("accept", "ACCEPTANCE_GATE", "golden.graph_terminal", {**common, "terminal_status": "VERIFIED_CANDIDATE"}, max_attempts=1, terminal=True, priority=40),
        GraphNodeSpec("quarantine", "HUMAN_ESCALATION", "golden.graph_terminal", {**common, "terminal_status": "QUARANTINED"}, max_attempts=1, terminal=True, priority=40),
    )
    edges = [
        GraphEdgeSpec("repair", "trajectory", ("PROPOSED",)),
        GraphEdgeSpec("trajectory", "verify", ("SAFE",)),
        GraphEdgeSpec("trajectory", "quarantine", ("REPLAY_REQUIRED",)),
        GraphEdgeSpec("verify", "accept", ("PASS",)),
        GraphEdgeSpec("verify", "quarantine", ("ABSTAIN", "EXHAUSTED")),
    ]
    if max_rounds > 1:
        edges.append(GraphEdgeSpec("verify", "repair", ("FAIL",), round_delta=1, max_round=max_rounds - 2))
    return GraphPlan(
        graph="golden-iterative-repair",
        objective=objective,
        start_node="repair",
        lanes=lanes,
        nodes=nodes,
        edges=tuple(edges),
        max_rounds=max_rounds,
        max_parallel_workers=max_workers,
        max_node_executions=len(lanes) * max_rounds * 4 + len(lanes),
        inputs=inputs,
    )


def register(registry):
    registry.tools.register(ToolSpec("golden.human_feedback_repair", "0.1.0", "Apply approved assistant-turn feedback with a fresh GPT-5.6-sol worker", ToolRisk.WORKSPACE_WRITE, _HUMAN_FEEDBACK_INPUT, _route_schema(("summary",), {"summary": {"type": "object"}}), _human_feedback_repair))
    registry.tools.register(ToolSpec("golden.graph_repair", "0.1.0", "Repair a verifier-rejected proposal with a fresh GPT-5.6-sol worker", ToolRisk.WORKSPACE_WRITE, _BASE_INPUT, _route_schema(("summary",), {"summary": {"type": "object"}}), _repair))
    registry.tools.register(ToolSpec("golden.graph_trajectory_gate", "0.1.0", "Fail closed when a repair invalidates immutable downstream user turns", ToolRisk.WORKSPACE_WRITE, _BASE_INPUT, _route_schema(("schema_version", "reason", "unsafe_turn_ids", "excluded_turn_ids", "user_turns_remain_immutable"), {"reason": {"type": "string"}, "unsafe_turn_ids": {"type": "array", "items": {"type": "string"}}, "excluded_turn_ids": {"type": "array", "items": {"type": "string"}}, "user_turns_remain_immutable": {"type": "boolean"}}), _trajectory))
    verify_input = json.loads(json.dumps(_BASE_INPUT))
    verify_input["required"].append("max_rounds")
    verify_input["properties"]["max_rounds"] = {"type": "integer", "minimum": 1}
    registry.tools.register(ToolSpec("golden.graph_verify", "0.1.0", "Independently verify one repair in a fresh GPT-5.6-sol session", ToolRisk.WORKSPACE_WRITE, verify_input, _route_schema(("summary", "finding_turn_ids", "blocking_turn_ids", "text_findings", "metadata_findings"), {"summary": {"type": "object"}, "finding_turn_ids": {"type": "array", "items": {"type": "string"}}, "blocking_turn_ids": {"type": "array", "items": {"type": "string"}}, "text_findings": {"type": "integer"}, "metadata_findings": {"type": "integer"}}), _verify))
    terminal_input = json.loads(json.dumps(_BASE_INPUT))
    terminal_input["required"].append("terminal_status")
    terminal_input["properties"]["terminal_status"] = {"type": "string", "enum": ["VERIFIED_CANDIDATE", "PARTIAL_CANDIDATE", "QUARANTINED", "NOT_SELECTED"]}
    registry.tools.register(ToolSpec("golden.graph_terminal", "0.1.0", "Commit a graph lane terminal state", ToolRisk.WORKSPACE_WRITE, terminal_input, _route_schema(("schema_version", "status", "graph_run_id", "lineage_root_run_id"), {"status": {"type": "string"}, "graph_run_id": {"type": "string"}, "lineage_root_run_id": {"type": "string"}}), _terminal))


def register_graphs(catalog, _tools):
    catalog.register(
        GraphSpec(
            "golden-iterative-repair",
            "Run quarantined conversations through parallel repair, trajectory safety, and independent re-verification loops",
            ("reiterate conversations", "repair and verify", "iterative golden"),
            _graph_plan,
        )
    )


