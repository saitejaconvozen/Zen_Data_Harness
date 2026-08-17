from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from zen_agent.factory_planner import FactoryObservation, compile_plan
from zen_agent.factory_queue import LocalFactoryQueue


class FactoryShardingTests(unittest.TestCase):
    def test_compiler_limits_each_fetch_shard_to_one_hundred_candidates(self):
        agents = tuple(
            {"agent_id": f"agent-{index}", "conversation_count": 100}
            for index in range(40)
        )
        observation = FactoryObservation(
            "run", 2, 5000, 20000, 0, 0, 20000, {}, (), agents
        )
        proposal = {
            "schema_version": "zen.factory-plan-proposal/1",
            "worker": {"role": "FACTORY_PLANNER", "model_id": "gpt-5.6-sol", "session_id": "factory-plan-run-c2"},
            "decision": {
                "action": "FETCH_CONVERSATIONS", "rationale": "bounded diversity scan",
                "selected_agent_ids": [item["agent_id"] for item in agents],
                "per_agent": 10, "scan_per_agent": 500, "seed": 10,
                "expected_candidates": 400, "coverage_priorities": [],
            },
        }
        critique = {
            "schema_version": "zen.factory-plan-critique/1",
            "worker": {"role": "PLAN_CRITIC", "model_id": "gpt-5.6-sol", "session_id": "factory-critic-run-c2"},
            "decision": {"verdict": "APPROVE", "summary": "safe", "violations": [], "required_changes": []},
        }
        compiled = compile_plan(observation, proposal, critique)
        self.assertEqual(len(compiled.queue_seeds), 4)
        for seed in compiled.queue_seeds:
            inputs = seed.payload["inputs"]
            self.assertLessEqual(len(inputs["agent_ids"]) * inputs["per_agent"], 100)

    def test_exact_ready_item_can_be_retired_without_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            queue = LocalFactoryQueue(Path(directory) / "queue.db")
            try:
                queue.enqueue("run", "oversized", "trace_fetch", {}, max_attempts=3)
                self.assertTrue(queue.cancel_ready("run", "oversized", "trace_fetch", "superseded by safe shards"))
                item = queue.item("run", "oversized", "trace_fetch")
                self.assertEqual(item["status"], "DEAD")
                self.assertIn("superseded", item["error"])
            finally:
                queue.close()


if __name__ == "__main__":
    unittest.main()
