from __future__ import annotations

import re
from pathlib import Path
import tempfile
import unittest

from zen_agent.artifacts import ArtifactStore
from zen_agent.factory import default_factory_manifest
from zen_agent.factory_control_state import FactoryControlState
from zen_agent.factory_operator import FactoryOperator
from zen_agent.factory_qualification import FactoryQualificationStore
from zen_agent.factory_queue import LocalFactoryQueue
from zen_agent.factory_planner import PlanRejected


ROOT = Path(__file__).resolve().parents[1]


class FakeRole:
    def execute(self, prompt, schema):
        session = re.search(r"session_id=([^\n]+)", prompt).group(1)
        role = schema["properties"]["worker"]["properties"]["role"]["enum"][0]
        if role == "FACTORY_PLANNER":
            return {
                "schema_version": "zen.factory-plan-proposal/1",
                "worker": {"role": role, "model_id": "gpt-5.6-sol", "session_id": session},
                "decision": {
                    "action": "FETCH_CONVERSATIONS", "rationale": "need candidates",
                    "selected_agent_ids": ["agent-a"], "per_agent": 3,
                    "scan_per_agent": 10, "seed": 1, "expected_candidates": 3,
                    "coverage_priorities": [],
                },
            }
        return {
            "schema_version": "zen.factory-plan-critique/1",
            "worker": {"role": role, "model_id": "gpt-5.6-sol", "session_id": session},
            "decision": {"verdict": "APPROVE", "summary": "safe", "violations": [], "required_changes": []},
        }


class NeverWorker:
    def run_until_idle(self, *_args, **_kwargs):
        raise AssertionError("no work should be executed in this test")


class RetryWorker:
    def run_until_idle(self, *_args, **_kwargs):
        return [{"stage": "trace_fetch", "status": "READY", "error": "transient timeout"}]


class RejectingRole(FakeRole):
    """Planner proposes; critic always withholds approval."""

    def execute(self, prompt, schema):
        result = super().execute(prompt, schema)
        if result["schema_version"] == "zen.factory-plan-critique/1":
            result["decision"] = {
                "verdict": "REJECT", "summary": "scan budget too wide",
                "violations": ["over-broad"], "required_changes": ["narrow the seed"],
            }
        return result


class DeadWorker:
    def run_until_idle(self, *_args, **_kwargs):
        return [{"stage": "trace_fetch", "status": "DEAD", "error": "attempts exhausted"}]


class FactoryOperatorTests(unittest.TestCase):
    def test_operator_plans_and_seeds_without_human_step(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            state = FactoryControlState(root / "control.db")
            queue = LocalFactoryQueue(root / "queue.db")
            qualification = FactoryQualificationStore(root / "qualification.db")
            try:
                run_id = state.create_run(default_factory_manifest(target_accepted=10))
                operator = FactoryOperator(
                    ROOT, state, queue, qualification, ArtifactStore(root / "artifacts"),
                    NeverWorker(), FakeRole(),
                )
                summary = operator.operate(
                    run_id,
                    {"result": {"agents": [{"agent_id": "agent-a", "conversation_count": 100}]}},
                    max_planning_cycles=1,
                    max_work_items=10,
                )
                self.assertEqual(summary["operator_status"], "BUDGET_EXHAUSTED")
                self.assertEqual(summary["planning_cycles"], 1)
                self.assertEqual(queue.counts_by_stage(run_id)["trace_fetch"]["READY"], 1)
            finally:
                qualification.close()
                queue.close()
                state.close()

    def test_retryable_failures_do_not_halt_a_long_run(self):
        """The queue re-runs these. At batch scale they are expected, not fatal."""
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            state = FactoryControlState(root / "control.db")
            queue = LocalFactoryQueue(root / "queue.db")
            qualification = FactoryQualificationStore(root / "qualification.db")
            try:
                run_id = state.create_run(default_factory_manifest(target_accepted=10))
                queue.enqueue(run_id, "fetch", "trace_fetch", {})
                operator = FactoryOperator(
                    ROOT, state, queue, qualification, ArtifactStore(root / "artifacts"),
                    RetryWorker(), FakeRole(),
                )
                summary = operator.operate(
                    run_id, {"result": {"agents": []}},
                    max_planning_cycles=1, max_work_items=10,
                )
                self.assertNotEqual(summary["operator_status"], "BLOCKED")
                self.assertNotEqual(state.run(run_id)["status"], "NEEDS_HUMAN")
            finally:
                qualification.close()
                queue.close()
                state.close()

    def test_dead_work_beyond_budget_escalates(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            state = FactoryControlState(root / "control.db")
            queue = LocalFactoryQueue(root / "queue.db")
            qualification = FactoryQualificationStore(root / "qualification.db")
            try:
                run_id = state.create_run(default_factory_manifest(target_accepted=10))
                queue.enqueue(run_id, "fetch", "trace_fetch", {})
                operator = FactoryOperator(
                    ROOT, state, queue, qualification, ArtifactStore(root / "artifacts"),
                    DeadWorker(), FakeRole(),
                )
                summary = operator.operate(
                    run_id, {"result": {"agents": []}},
                    max_planning_cycles=1, max_work_items=10, dead_budget=0,
                )
                self.assertEqual(summary["operator_status"], "BLOCKED")
                self.assertEqual(state.run(run_id)["status"], "NEEDS_HUMAN")
            finally:
                qualification.close()
                queue.close()
                state.close()

    def test_dead_work_within_budget_is_absorbed(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            state = FactoryControlState(root / "control.db")
            queue = LocalFactoryQueue(root / "queue.db")
            qualification = FactoryQualificationStore(root / "qualification.db")
            try:
                run_id = state.create_run(default_factory_manifest(target_accepted=10))
                queue.enqueue(run_id, "fetch", "trace_fetch", {})
                operator = FactoryOperator(
                    ROOT, state, queue, qualification, ArtifactStore(root / "artifacts"),
                    DeadWorker(), FakeRole(),
                )
                summary = operator.operate(
                    run_id, {"result": {"agents": []}},
                    max_planning_cycles=1, max_work_items=3, dead_budget=5,
                )
                self.assertNotEqual(summary["operator_status"], "BLOCKED")
            finally:
                qualification.close()
                queue.close()
                state.close()


class PlanRejectionTests(unittest.TestCase):
    """A withheld approval must not destroy an unattended batch."""

    def _operate(self, **kwargs):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            state = FactoryControlState(root / "control.db")
            queue = LocalFactoryQueue(root / "queue.db")
            qualification = FactoryQualificationStore(root / "qualification.db")
            try:
                run_id = state.create_run(default_factory_manifest(target_accepted=10))
                operator = FactoryOperator(
                    ROOT, state, queue, qualification, ArtifactStore(root / "artifacts"),
                    NeverWorker(), RejectingRole(),
                )
                summary = operator.operate(
                    run_id,
                    {"result": {"agents": [{"agent_id": "agent-a", "conversation_count": 100}]}},
                    max_work_items=10, **kwargs,
                )
                return summary, state.run(run_id), queue.counts_by_stage(run_id)
            finally:
                qualification.close()
                queue.close()
                state.close()

    def test_rejection_does_not_raise(self):
        summary, _run, _counts = self._operate(max_planning_cycles=2)
        self.assertIn("PLAN_REJECTED", [a["kind"] for a in summary["actions"]])

    def test_rejection_seeds_no_work(self):
        _summary, _run, counts = self._operate(max_planning_cycles=2)
        self.assertEqual(counts, {}, "a rejected plan must never enqueue work")

    def test_repeated_rejection_escalates_instead_of_looping(self):
        summary, run, _counts = self._operate(
            max_planning_cycles=10, max_consecutive_rejections=2
        )
        self.assertEqual(summary["operator_status"], "BLOCKED")
        self.assertEqual(run["status"], "NEEDS_HUMAN")
        self.assertIn("consecutive", summary["reason"])

    def test_cycle_budget_still_bounds_the_loop(self):
        summary, _run, _counts = self._operate(
            max_planning_cycles=2, max_consecutive_rejections=99
        )
        self.assertEqual(summary["planning_cycles"], 2)


if __name__ == "__main__":
    unittest.main()
