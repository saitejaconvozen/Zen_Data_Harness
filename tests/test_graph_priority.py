from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from zen_agent.artifacts import ArtifactStore
from zen_agent.config import load_config
from zen_agent.graph import GraphEdgeSpec, GraphNodeSpec, GraphPlan, LaneSpec
from zen_agent.graph_runtime import GraphSupervisor
from zen_agent.graph_state import GraphState
from zen_agent.models import ToolRisk
from zen_agent.tools import ToolRegistry, ToolSpec


ROOT = Path(__file__).resolve().parents[1]
EMPTY = {"type": "object", "additionalProperties": False, "properties": {}}
OUTPUT = {
    "type": "object",
    "required": ["route"],
    "additionalProperties": False,
    "properties": {"route": {"type": "string"}},
}


class GraphPriorityTests(unittest.TestCase):
    def test_downstream_lane_work_overlaps_upstream_backlog(self):
        tools = ToolRegistry()
        tools.register(ToolSpec("fixture.produce", "1", "produce", ToolRisk.READ_ONLY, EMPTY, OUTPUT, lambda _c, _i: {"route": "NEXT"}))
        tools.register(ToolSpec("fixture.verify", "1", "verify", ToolRisk.READ_ONLY, EMPTY, OUTPUT, lambda _c, _i: {"route": "DONE"}))
        plan = GraphPlan(
            "priority", "priority", "produce",
            (LaneSpec("lane-a", {}), LaneSpec("lane-b", {})),
            (
                GraphNodeSpec("produce", "REFINER", "fixture.produce", {}, priority=10),
                GraphNodeSpec("verify", "VERIFIER", "fixture.verify", {}, terminal=True, priority=20),
            ),
            (GraphEdgeSpec("produce", "verify", ("NEXT",)),),
            1, 1, 4, {},
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = GraphState(root / "state.db")
            try:
                run_id = GraphSupervisor(load_config(ROOT), tools, state, ArtifactStore(root / "artifacts")).start(plan)
                dispatched = [event["payload"] for event in state.trace(run_id) if event["event_type"] == "worker.dispatched"]
                self.assertEqual(
                    [(item["lane"], item["node"]) for item in dispatched],
                    [("lane-a", "produce"), ("lane-a", "verify"), ("lane-b", "produce"), ("lane-b", "verify")],
                )
            finally:
                state.close()


if __name__ == "__main__":
    unittest.main()
