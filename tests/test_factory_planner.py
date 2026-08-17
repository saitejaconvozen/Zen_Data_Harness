from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from zen_agent.artifacts import ArtifactStore
from zen_agent.config import load_config
from zen_agent.factory import default_factory_manifest
from zen_agent.factory_control_state import FactoryControlState
from zen_agent.factory_planner import FactoryObservation, compile_plan
from zen_agent.factory_queue import LocalFactoryQueue
from zen_agent.factory_worker import FactoryWorker
from zen_agent.models import ToolRisk
from zen_agent.tools import ToolRegistry, ToolSpec


ROOT = Path(__file__).resolve().parents[1]


def observation(**changes):
    values = {
        "run_id": "factory-run",
        "cycle": 0,
        "target_accepted": 5000,
        "candidate_floor": 20000,
        "accepted_count": 0,
        "unique_candidates_seen": 0,
        "remaining_scan_budget": 20000,
        "queue_counts": {},
        "coverage_gaps": ({"axis": "language", "cell": "te", "remaining": 50},),
        "agents": (
            {"agent_id": "agent-a", "conversation_count": 100, "languages": ["te"]},
            {"agent_id": "agent-b", "conversation_count": 0, "languages": ["en"]},
        ),
    }
    values.update(changes)
    return FactoryObservation(**values)


def proposal(action="FETCH_CONVERSATIONS", selected=None, expected=2):
    return {
        "schema_version": "zen.factory-plan-proposal/1",
        "worker": {"role": "FACTORY_PLANNER", "model_id": "gpt-5.6-sol", "session_id": "factory-plan-factory-run-c0"},
        "decision": {
            "action": action,
            "rationale": "fill Telugu coverage",
            "selected_agent_ids": ["agent-a"] if selected is None else selected,
            "per_agent": 2,
            "scan_per_agent": 20,
            "seed": 20260813,
            "expected_candidates": expected,
            "coverage_priorities": ["language:te"],
        },
    }


def critique(verdict="APPROVE"):
    return {
        "schema_version": "zen.factory-plan-critique/1",
        "worker": {"role": "PLAN_CRITIC", "model_id": "gpt-5.6-sol", "session_id": "factory-critic-factory-run-c0"},
        "decision": {"verdict": verdict, "summary": "bounded and supported", "violations": [], "required_changes": []},
    }


class FactoryPlannerTests(unittest.TestCase):
    def test_approved_fetch_compiles_to_locator_only_queue_seed(self):
        plan = compile_plan(observation(), proposal(), critique())
        self.assertEqual(plan.action, "FETCH_CONVERSATIONS")
        self.assertEqual(len(plan.queue_seeds), 1)
        seed = plan.queue_seeds[0]
        self.assertEqual(seed.stage, "trace_fetch")
        self.assertEqual(seed.payload["tool"], "golden.sample_conversations")
        self.assertNotIn("chat_history", str(seed.payload))

    def test_compiler_rejects_unknown_agent_even_if_critic_approves(self):
        with self.assertRaisesRegex(ValueError, "unknown or empty"):
            compile_plan(observation(), proposal(selected=["agent-b"]), critique())

    def test_compiler_rejects_premature_completion(self):
        with self.assertRaisesRegex(ValueError, "cannot complete"):
            compile_plan(observation(), proposal("COMPLETE", selected=[], expected=0), critique())

    def test_failure_rates_force_pause(self):
        with self.assertRaisesRegex(ValueError, "requires PAUSE"):
            compile_plan(observation(dead_letter_rate=0.10), proposal(), critique())

    def test_control_state_records_cycles(self):
        with tempfile.TemporaryDirectory() as directory:
            state = FactoryControlState(Path(directory) / "control.db")
            try:
                run_id = state.create_run(default_factory_manifest(target_accepted=10))
                state.start_cycle(run_id, 0, "a" * 64)
                state.finish_cycle(
                    run_id, 0, status="COMPILED", proposal_sha256="b" * 64,
                    critique_sha256="c" * 64, compiled_sha256="d" * 64,
                    action="FETCH_CONVERSATIONS",
                )
                self.assertEqual(state.run(run_id)["status"], "RUNNING")
                self.assertEqual(state.next_cycle(run_id), 1)
            finally:
                state.close()

    def test_worker_claims_tool_and_fans_out_packet_preparation(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            (root / "ZEN.md").write_text("fixture", encoding="utf-8")
            config = replace(
                load_config(ROOT), root=root, state_directory=root / ".zen",
                instruction_file=root / "ZEN.md", plugin_paths=(),
            )
            queue = LocalFactoryQueue(root / ".zen" / "queue.db")
            tools = ToolRegistry()
            input_schema = {
                "type": "object", "required": ["agent_ids", "per_agent", "scan_per_agent", "seed"],
                "additionalProperties": False,
                "properties": {
                    "agent_ids": {"type": "array", "items": {"type": "string"}},
                    "per_agent": {"type": "integer"}, "scan_per_agent": {"type": "integer"},
                    "seed": {"type": "integer"},
                },
            }
            output_schema = {
                "type": "object", "required": ["selected_count", "conversations"],
                "additionalProperties": False,
                "properties": {"selected_count": {"type": "integer"}, "conversations": {"type": "array"}},
            }
            tools.register(ToolSpec(
                "golden.sample_conversations", "1", "fixture", ToolRisk.READ_ONLY,
                input_schema, output_schema,
                lambda _context, _inputs: {"selected_count": 1, "conversations": [{"opaque": True}]},
            ))
            queue.enqueue(
                "run", "cycle-0", "trace_fetch",
                {"tool": "golden.sample_conversations", "inputs": {"agent_ids": ["a"], "per_agent": 1, "scan_per_agent": 1, "seed": 1}},
            )
            worker = FactoryWorker(config, tools, queue, ArtifactStore(root / ".zen" / "artifacts"), "worker")
            try:
                result = worker.run_one("run", ("trace_fetch",))
                self.assertEqual(result["status"], "SUCCEEDED")
                self.assertEqual(queue.item("run", "cycle-0:packets", "prepare_packets")["status"], "READY")
            finally:
                queue.close()


if __name__ == "__main__":
    unittest.main()
