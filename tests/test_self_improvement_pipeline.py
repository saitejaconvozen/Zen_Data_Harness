from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from zen_agent.config import load_config
from zen_agent.factory_queue import LocalFactoryQueue
from zen_agent.factory_worker_pool import ParallelFactoryWorkerPool
from zen_agent.models import ToolRisk
from zen_agent.self_improve_cli import _router_decision, _targets
from zen_agent.tools import ToolRegistry, ToolSpec


ROOT = Path(__file__).resolve().parents[1]
ANY = {"type": "object"}


class SelfImprovementPipelineTests(unittest.TestCase):
    def test_exact_human_edit_becomes_assistant_only_repair_instruction(self):
        decision = {
            "action": "EDIT",
            "assistant_edits": [{"turn_id": "turn_0001", "text": "I can help with that."}],
            "feedback": {"summary": "Use a grounded answer", "evidence_turn_ids": []},
        }
        targets = _targets(decision)
        self.assertEqual([row["turn_id"] for row in targets], ["turn_0001"])
        self.assertIn("I can help with that.", targets[0]["instruction"])
        self.assertNotIn("role", targets[0])

    def test_review_ledger_adapter_preserves_decision_and_source_lineage(self):
        item = {
            "run_id": "run", "conversation_id": "rp_" + "a" * 64,
            "source_content_sha256": "b" * 64,
            "repair_decision": {
                "id": "hfd_" + "c" * 64, "action": "REQUEST_REPAIR",
                "reviewer_identity": "reviewer@example.org", "created_at": 1.0,
                "assistant_edits": [],
                "feedback": {
                    "summary": "Ground the claim", "evidence_turn_ids": ["turn_0001"],
                    "requested_changes": [{"turn_id": "turn_0001", "instruction": "Do not claim success."}],
                },
            },
        }
        sample = {"packet_batch": "batch.json", "packet_index": 4}
        value = _router_decision(item, sample)
        self.assertEqual(value["decision_id"], item["repair_decision"]["id"])
        self.assertEqual(value["source_content_sha256"], "b" * 64)
        self.assertEqual(value["packet_locator"], sample)
        self.assertEqual(value["approval"]["reviewer_id"], "reviewer@example.org")

    def test_human_feedback_is_repaired_then_independently_reverified(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            (root / "ZEN.md").write_text("fixture", encoding="utf-8")
            config = replace(
                load_config(ROOT), root=root, state_directory=root / ".zen",
                instruction_file=root / "ZEN.md", plugin_paths=(),
            )
            tools = ToolRegistry()
            calls = []
            outputs = {
                "golden.human_feedback_repair": {
                    "route": "PROPOSED", "summary": {
                        "prompt_usable": True, "quarantine_reasons": [],
                        "replay_required": False,
                    },
                },
                "golden.graph_trajectory_gate": {"route": "SAFE"},
                "golden.graph_verify": {"route": "PASS", "summary": {"decision": "PASS"}},
                "golden.graph_terminal": {"route": "DONE"},
            }
            for name, output in outputs.items():
                def invoke(_context, inputs, *, tool=name, value=output):
                    calls.append((tool, dict(inputs)))
                    return value
                tools.register(ToolSpec(name, "1", "fixture", ToolRisk.WORKSPACE_WRITE, ANY, ANY, invoke))
            decision_id = "hfd_" + "d" * 64
            conversation_key = "conversation-source:review-" + decision_id
            queue = LocalFactoryQueue(config.state_directory / "factory-queue.db")
            try:
                queue.enqueue(
                    "run", conversation_key + ":human-feedback-round-00",
                    "human_feedback_repair",
                    {
                        "tool": "golden.human_feedback_repair",
                        "inputs": {
                            "packet_batch": "batch.json", "packet_index": 0,
                            "packet_id": "rp_" + "e" * 64,
                            "source_decision_run_id": "run", "round_number": 3,
                        },
                        "source_content_sha256": "a" * 64,
                        "packet_id": "rp_" + "e" * 64,
                        "conversation_job_key": conversation_key,
                        "review_decision_id": decision_id,
                        "max_repair_rounds": 6,
                    },
                )
            finally:
                queue.close()
            results = ParallelFactoryWorkerPool(config, tools, workers=2).run_until_idle(
                "run", ("human_feedback_repair", "trajectory_gate", "verify_repair", "terminal"),
                max_items=10,
            )
            self.assertTrue(all(row["status"] == "SUCCEEDED" for row in results))
            self.assertEqual([name for name, _ in calls], [
                "golden.human_feedback_repair", "golden.graph_trajectory_gate",
                "golden.graph_verify", "golden.graph_terminal",
            ])
            queue = LocalFactoryQueue(config.state_directory / "factory-queue.db")
            try:
                terminal = queue.item("run", conversation_key, "terminal")
                self.assertEqual(terminal["payload"]["review_decision_id"], decision_id)
                self.assertEqual(terminal["payload"]["inputs"]["terminal_status"], "VERIFIED_CANDIDATE")
            finally:
                queue.close()


if __name__ == "__main__":
    unittest.main()
