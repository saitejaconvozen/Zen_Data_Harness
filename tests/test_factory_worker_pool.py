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




if __name__ == "__main__":
    unittest.main()
