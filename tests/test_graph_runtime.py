from __future__ import annotations

from pathlib import Path
import tempfile
import time
import unittest

from zen_agent.artifacts import ArtifactStore
from zen_agent.config import load_config
from zen_agent.graph import GraphEdgeSpec, GraphNodeSpec, GraphPlan, LaneSpec
from zen_agent.graph_runtime import GraphSupervisor
from zen_agent.graph_state import GraphState
from zen_agent.models import ToolRisk
from zen_agent.tools import ToolRegistry, ToolSpec


ROOT = Path(__file__).resolve().parents[1]
EMPTY_INPUT = {"type": "object", "additionalProperties": False, "properties": {}}
ROUND_INPUT = {
    "type": "object",
    "required": ["round"],
    "additionalProperties": False,
    "properties": {"round": {"type": "integer", "minimum": 0}},
}
ROUTE_OUTPUT = {
    "type": "object",
    "required": ["route"],
    "additionalProperties": False,
    "properties": {"route": {"type": "string"}},
}


class GraphRuntimeTests(unittest.TestCase):
    def test_bounded_cycle_advances_round_and_reaches_terminal(self):
        tools = ToolRegistry()
        tools.register(ToolSpec("fixture.route", "1", "fail once then pass", ToolRisk.READ_ONLY, ROUND_INPUT, ROUTE_OUTPUT, lambda _c, i: {"route": "FAIL" if i["round"] == 0 else "PASS"}))
        tools.register(ToolSpec("fixture.done", "1", "terminal", ToolRisk.READ_ONLY, EMPTY_INPUT, ROUTE_OUTPUT, lambda _c, _i: {"route": "DONE"}))
        plan = GraphPlan(
            "fixture-cycle", "cycle", "work", (LaneSpec("conversation", {}),),
            (GraphNodeSpec("work", "WORKER", "fixture.route", {"round": {"$round": True}}), GraphNodeSpec("done", "TERMINAL", "fixture.done", {}, terminal=True)),
            (GraphEdgeSpec("work", "work", ("FAIL",), round_delta=1, max_round=0), GraphEdgeSpec("work", "done", ("PASS",))),
            2, 1, 8, {},
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = GraphState(root / "graph.db")
            try:
                run_id = GraphSupervisor(load_config(ROOT), tools, state, ArtifactStore(root / "artifacts")).start(plan)
                self.assertEqual(state.run(run_id)["status"], "SUCCEEDED")
                visits = [(item["node_key"], item["round_number"], item["route"]) for item in state.executions(run_id)]
                self.assertEqual(visits, [("work", 0, "FAIL"), ("work", 1, "PASS"), ("done", 1, "DONE")])
            finally:
                state.close()

    def test_independent_lanes_execute_in_parallel(self):
        tools = ToolRegistry()
        def slow(_context, _inputs):
            time.sleep(0.15)
            return {"route": "DONE"}
        tools.register(ToolSpec("fixture.slow", "1", "slow terminal", ToolRisk.READ_ONLY, EMPTY_INPUT, ROUTE_OUTPUT, slow))
        plan = GraphPlan("fixture-parallel", "parallel", "work", tuple(LaneSpec(f"lane-{i}", {}) for i in range(3)), (GraphNodeSpec("work", "WORKER", "fixture.slow", {}, terminal=True),), (), 1, 3, 6, {})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = GraphState(root / "graph.db")
            try:
                started = time.monotonic()
                run_id = GraphSupervisor(load_config(ROOT), tools, state, ArtifactStore(root / "artifacts")).start(plan)
                self.assertEqual(state.run(run_id)["status"], "SUCCEEDED")
                self.assertLess(time.monotonic() - started, 0.38)
                self.assertEqual(sum(event["event_type"] == "worker.dispatched" for event in state.trace(run_id)), 3)
            finally:
                state.close()

    def test_zero_round_cycle_is_rejected(self):
        plan = GraphPlan(
            "bad", "bad", "a", (LaneSpec("lane", {}),),
            (GraphNodeSpec("a", "A", "fixture.a", {}), GraphNodeSpec("b", "B", "fixture.b", {}, terminal=True)),
            (GraphEdgeSpec("a", "b", ("X",)), GraphEdgeSpec("b", "a", ("Y",))),
            2, 1, 10, {},
        )
        with self.assertRaisesRegex(ValueError, "zero-round cycle"):
            plan.validate({"fixture.a", "fixture.b"})


if __name__ == "__main__":
    unittest.main()
