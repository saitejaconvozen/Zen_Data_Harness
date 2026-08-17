from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from zen_agent.config import load_config
from zen_agent.factory_queue import LocalFactoryQueue
from zen_agent.factory_worker_pool import ParallelFactoryWorkerPool
from zen_agent.models import ToolRisk
from zen_agent.tools import ToolRegistry, ToolSpec


ROOT = Path(__file__).resolve().parents[1]
ANY = {"type": "object"}


class FactoryRefinementPipelineTests(unittest.TestCase):
    def test_failed_initial_proposal_is_repaired_and_independently_reverified(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            (root / "ZEN.md").write_text("fixture", encoding="utf-8")
            config = replace(
                load_config(ROOT),
                root=root,
                state_directory=root / ".zen",
                instruction_file=root / "ZEN.md",
                plugin_paths=(),
            )
            tools = ToolRegistry()

            outputs = {
                "golden.refine_one": {
                    "decision_sha256": "1" * 64,
                    "summary": {
                        "prompt_usable": True,
                        "quarantine_reasons": [],
                        "replay_required": False,
                    },
                },
                "golden.verify_one": {
                    "decision_sha256": "2" * 64,
                    "summary": {"decision": "FAIL", "replay_required": False},
                },
                "golden.graph_repair": {
                    "route": "PROPOSED",
                    "summary": {
                        "prompt_usable": True,
                        "quarantine_reasons": [],
                        "replay_required": False,
                    },
                },
                "golden.graph_trajectory_gate": {"route": "SAFE"},
                "golden.graph_terminal": {"route": "DONE"},
            }
            for name, output in outputs.items():
                tools.register(
                    ToolSpec(
                        name, "1", "fixture", ToolRisk.WORKSPACE_WRITE,
                        ANY, ANY, lambda _context, _inputs, value=output: value,
                    )
                )
            graph_verify_calls = []

            def graph_verify(_context, _inputs):
                graph_verify_calls.append(dict(_inputs))
                route = "FAIL" if len(graph_verify_calls) == 1 else "PASS"
                return {"route": route, "summary": {"decision": route}}

            tools.register(
                ToolSpec(
                    "golden.graph_verify", "1", "fixture",
                    ToolRisk.WORKSPACE_WRITE, ANY, ANY, graph_verify,
                )
            )

            queue = LocalFactoryQueue(config.state_directory / "factory-queue.db")
            try:
                queue.enqueue(
                    "run",
                    "conversation-source",
                    "refine",
                    {
                        "tool": "golden.refine_one",
                        "inputs": {"packet_batch": "batch.json", "packet_index": 0},
                        "source_content_sha256": "a" * 64,
                        "packet_id": "rp_" + "b" * 64,
                        "configuration_key": "c" * 64,
                        "qualification_status": "REJECTED",
                        "max_repair_rounds": 3,
                    },
                )
            finally:
                queue.close()

            results = ParallelFactoryWorkerPool(
                config, tools, workers=2
            ).run_until_idle(
                "run",
                (
                    "refine", "verify", "repair", "trajectory_gate",
                    "verify_repair", "terminal",
                ),
                max_items=20,
            )
            self.assertTrue(all(row["status"] == "SUCCEEDED" for row in results))
            queue = LocalFactoryQueue(config.state_directory / "factory-queue.db")
            try:
                counts = queue.counts_by_stage("run")
                for stage in ("refine", "verify", "terminal"):
                    self.assertEqual(counts[stage]["SUCCEEDED"], 1)
                for stage in ("repair", "trajectory_gate", "verify_repair"):
                    self.assertEqual(counts[stage]["SUCCEEDED"], 2)
                self.assertEqual(
                    [row["round_number"] for row in graph_verify_calls], [0, 1]
                )
                terminal = queue.item("run", "conversation-source", "terminal")
                self.assertEqual(
                    terminal["payload"]["inputs"]["terminal_status"],
                    "VERIFIED_CANDIDATE",
                )
            finally:
                queue.close()


if __name__ == "__main__":
    unittest.main()
