from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import time
import unittest

from zen_agent.config import load_config
from zen_agent.factory_queue import LocalFactoryQueue
from zen_agent.factory_worker_pool import ParallelFactoryWorkerPool
from zen_agent.models import ToolRisk
from zen_agent.tools import ToolRegistry, ToolSpec


ROOT = Path(__file__).resolve().parents[1]


class FactoryWorkerPoolTests(unittest.TestCase):
    def test_three_independent_queue_items_run_concurrently(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            (root / "ZEN.md").write_text("fixture", encoding="utf-8")
            config = replace(
                load_config(ROOT), root=root, state_directory=root / ".zen",
                instruction_file=root / "ZEN.md", plugin_paths=(),
            )
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
            def slow(_context, _inputs):
                time.sleep(0.15)
                return {"selected_count": 0, "conversations": []}
            tools.register(ToolSpec(
                "golden.sample_conversations", "1", "fixture", ToolRisk.READ_ONLY,
                input_schema, output_schema, slow,
            ))
            queue = LocalFactoryQueue(config.state_directory / "factory-queue.db")
            try:
                for index in range(3):
                    queue.enqueue(
                        "run", f"fetch-{index}", "trace_fetch",
                        {"tool": "golden.sample_conversations", "inputs": {"agent_ids": [str(index)], "per_agent": 1, "scan_per_agent": 1, "seed": index}},
                    )
            finally:
                queue.close()
            started = time.monotonic()
            results = ParallelFactoryWorkerPool(config, tools, workers=3).run_until_idle(
                "run", ("trace_fetch",), max_items=3
            )
            self.assertEqual(len(results), 3)
            self.assertTrue(all(item["status"] == "SUCCEEDED" for item in results))
            self.assertLess(time.monotonic() - started, 0.40)
    def test_ready_stages_share_worker_batch_without_priority_starvation(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            (root / "ZEN.md").write_text("fixture", encoding="utf-8")
            config = replace(
                load_config(ROOT), root=root, state_directory=root / ".zen",
                instruction_file=root / "ZEN.md", plugin_paths=(),
            )
            queue = LocalFactoryQueue(config.state_directory / "factory-queue.db")
            try:
                for index in range(8):
                    queue.enqueue("run", f"refine-{index}", "refine", {})
                for index in range(3):
                    queue.enqueue("run", f"verify-{index}", "verify", {})
            finally:
                queue.close()
            pool = ParallelFactoryWorkerPool(config, ToolRegistry(), workers=4)
            assignments = pool._stage_assignments(
                "run", ("refine", "verify"), 4
            )
            self.assertIn(("refine",), assignments)
            self.assertIn(("verify",), assignments)

    def test_single_slot_refills_rotate_across_ready_stages(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            (root / "ZEN.md").write_text("fixture", encoding="utf-8")
            config = replace(
                load_config(ROOT), root=root, state_directory=root / ".zen",
                instruction_file=root / "ZEN.md", plugin_paths=(),
            )
            queue = LocalFactoryQueue(config.state_directory / "factory-queue.db")
            try:
                queue.enqueue("run", "refine-a", "refine", {})
                queue.enqueue("run", "verify-a", "verify", {})
            finally:
                queue.close()
            pool = ParallelFactoryWorkerPool(config, ToolRegistry(), workers=2)
            first = pool._stage_assignments("run", ("refine", "verify"), 1)
            second = pool._stage_assignments("run", ("refine", "verify"), 1)
            self.assertEqual({first[0], second[0]}, {("refine",), ("verify",)})
    def test_backlog_pressure_weights_assignments_toward_busier_stage(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            (root / "ZEN.md").write_text("fixture", encoding="utf-8")
            config = replace(
                load_config(ROOT), root=root, state_directory=root / ".zen",
                instruction_file=root / "ZEN.md", plugin_paths=(),
            )
            queue = LocalFactoryQueue(config.state_directory / "factory-queue.db")
            try:
                for index in range(12):
                    queue.enqueue("run", f"refine-{index}", "refine", {})
                queue.enqueue("run", "verify-0", "verify", {})
            finally:
                queue.close()
            assignments = ParallelFactoryWorkerPool(
                config, ToolRegistry(), workers=4
            )._stage_assignments("run", ("refine", "verify"), 4)
            self.assertGreater(
                assignments.count(("refine",)),
                assignments.count(("verify",)),
            )

    def test_aging_serves_low_backlog_stage_within_two_refill_rounds(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            (root / "ZEN.md").write_text("fixture", encoding="utf-8")
            config = replace(
                load_config(ROOT), root=root, state_directory=root / ".zen",
                instruction_file=root / "ZEN.md", plugin_paths=(),
            )
            queue = LocalFactoryQueue(config.state_directory / "factory-queue.db")
            try:
                for index in range(20):
                    queue.enqueue("run", f"refine-{index}", "refine", {})
                queue.enqueue("run", "verify-0", "verify", {})
            finally:
                queue.close()
            pool = ParallelFactoryWorkerPool(config, ToolRegistry(), workers=2)
            assignments = []
            for _ in range(2):
                assignments.extend(
                    pool._stage_assignments("run", ("refine", "verify"), 1)
                )
            self.assertIn(("verify",), assignments)

    def test_empty_stage_is_not_assigned_when_another_stage_is_ready(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            (root / "ZEN.md").write_text("fixture", encoding="utf-8")
            config = replace(
                load_config(ROOT), root=root, state_directory=root / ".zen",
                instruction_file=root / "ZEN.md", plugin_paths=(),
            )
            queue = LocalFactoryQueue(config.state_directory / "factory-queue.db")
            try:
                queue.enqueue("run", "verify-0", "verify", {})
                queue.enqueue("run", "verify-1", "verify", {})
            finally:
                queue.close()
            assignments = ParallelFactoryWorkerPool(
                config, ToolRegistry(), workers=2
            )._stage_assignments("run", ("refine", "verify"), 2)
            self.assertEqual(assignments, [("verify",), ("verify",)])

    def test_leased_work_consumes_stage_capacity(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            (root / "ZEN.md").write_text("fixture", encoding="utf-8")
            config = replace(
                load_config(ROOT), root=root, state_directory=root / ".zen",
                instruction_file=root / "ZEN.md", plugin_paths=(),
            )
            queue = LocalFactoryQueue(config.state_directory / "factory-queue.db")
            try:
                queue.enqueue("run", "refine-leased", "refine", {})
                queue.enqueue("run", "refine-ready", "refine", {})
                queue.enqueue("run", "verify-ready", "verify", {})
                self.assertIsNotNone(
                    queue.claim("run", "fixture-worker", ("refine",))
                )
            finally:
                queue.close()
            pool = ParallelFactoryWorkerPool(
                config, ToolRegistry(), workers=2,
                stage_capacities={"refine": 1, "verify": 1},
            )
            self.assertEqual(
                pool._stage_assignments("run", ("refine", "verify"), 2),
                [("verify",)],
            )

    def test_max_items_limits_initial_submission(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            (root / "ZEN.md").write_text("fixture", encoding="utf-8")
            config = replace(
                load_config(ROOT), root=root, state_directory=root / ".zen",
                instruction_file=root / "ZEN.md", plugin_paths=(),
            )
            pool = ParallelFactoryWorkerPool(config, ToolRegistry(), workers=4)
            calls = []

            def run_one(run_id, stages):
                calls.append((run_id, stages))
                return {"stage": stages[0], "status": "SUCCEEDED"}

            pool._run_one = run_one
            results = pool.run_until_idle("run", ("refine",), max_items=2)
            self.assertEqual(len(calls), 2)
            self.assertEqual(len(results), 2)


if __name__ == "__main__":
    unittest.main()
