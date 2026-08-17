from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any

from .artifacts import ArtifactStore
from .config import HarnessConfig
from .graph import GraphPlan, resolve_inputs
from .graph_state import GraphState
from .models import ToolRisk
from .policy import PolicyEngine
from .tools import ToolContext, ToolRegistry


class GraphSupervisor:
    def __init__(
        self,
        config: HarnessConfig,
        tools: ToolRegistry,
        state: GraphState,
        artifacts: ArtifactStore,
    ):
        self.config = config
        self.tools = tools
        self.state = state
        self.artifacts = artifacts
        self.policy = PolicyEngine(config)

    def start(self, plan: GraphPlan) -> str:
        plan.validate(set(self.tools.names()))
        run_id = self.state.create_run(plan)
        self.execute(run_id)
        return run_id

    def execute(self, run_id: str) -> None:
        run = self.state.run(run_id)
        plan = GraphPlan.from_dict(run["plan"])
        plan.validate(set(self.tools.names()))
        self.state.set_run(run_id, "RUNNING")
        tool_calls = 0
        futures: dict[Future, dict[str, Any]] = {}
        with ThreadPoolExecutor(
            max_workers=plan.max_parallel_workers,
            thread_name_prefix="zen-graph",
        ) as pool:
            while True:
                executions = self.state.executions(run_id)
                terminal_keys = {node.key for node in plan.nodes if node.terminal}
                terminal_lanes = {
                    item["lane_key"]
                    for item in executions
                    if item["status"] == "SUCCEEDED" and item["node_key"] in terminal_keys
                }
                if terminal_lanes == {lane.key for lane in plan.lanes}:
                    manifest = {
                        "schema_version": "zen.graph-completion/1",
                        "run_id": run_id,
                        "graph": plan.graph,
                        "lanes": len(plan.lanes),
                        "executions": len(executions),
                        "terminal_lanes": sorted(terminal_lanes),
                    }
                    self.artifacts.put_json(manifest)
                    self.state.set_run(run_id, "SUCCEEDED", "all lanes reached a terminal node")
                    return
                exhausted = [
                    item
                    for item in executions
                    if item["status"] == "FAILED" and item["attempts"] >= item["max_attempts"]
                ]
                if exhausted:
                    self.state.set_run(
                        run_id,
                        "FAILED",
                        "node attempts exhausted: "
                        + ", ".join(f"{item['lane_key']}:{item['node_key']}" for item in exhausted),
                    )
                    return
                if len(executions) > plan.max_node_executions:
                    self.state.set_run(run_id, "BLOCKED", "node-execution budget exhausted")
                    return

                active_ids = {item["id"] for item in futures.values()}
                ready = [
                    item
                    for item in executions
                    if item["status"] == "PENDING" and item["id"] not in active_ids
                ]
                ready.sort(
                    key=lambda item: (
                        -plan.node(item["node_key"]).priority,
                        item["created_at"],
                        item["lane_key"],
                    )
                )
                for item in ready:
                    if len(futures) >= plan.max_parallel_workers:
                        break
                    if tool_calls >= plan.max_node_executions:
                        self.state.set_run(run_id, "BLOCKED", "tool-call budget exhausted")
                        return
                    node = plan.node(item["node_key"])
                    tool = self.tools.get(node.tool)
                    decision = self.policy.evaluate(tool.name, tool.risk)
                    self.state.event(
                        run_id,
                        "policy.evaluated",
                        {
                            "lane": item["lane_key"],
                            "node": node.key,
                            "tool": tool.name,
                            "effect": decision.effect,
                            "reason": decision.reason,
                        },
                        item["id"],
                    )
                    if decision.effect == "needs_approval":
                        self.state.set_run(run_id, "NEEDS_HUMAN", decision.reason)
                        return
                    if decision.effect != "allow":
                        self.state.set_run(run_id, "BLOCKED", decision.reason)
                        return
                    lane = plan.lane(item["lane_key"])
                    inputs = resolve_inputs(node.inputs, lane, item["round_number"], run_id)
                    started = self.state.start(item["id"], inputs)
                    future = pool.submit(
                        self.tools.invoke,
                        tool.name,
                        ToolContext(run_id, started["id"], self.config.root),
                        inputs,
                    )
                    futures[future] = started
                    tool_calls += 1
                    self.state.event(
                        run_id,
                        "worker.dispatched",
                        {
                            "lane": started["lane_key"],
                            "node": started["node_key"],
                            "round": started["round_number"],
                            "role": node.role,
                        },
                        started["id"],
                    )

                if not futures:
                    self.state.set_run(run_id, "BLOCKED", "no runnable graph node remains")
                    return
                completed, _pending = wait(tuple(futures), return_when=FIRST_COMPLETED)
                for future in completed:
                    execution = futures.pop(future)
                    node = plan.node(execution["node_key"])
                    try:
                        output = future.result()
                        route = output.get("route")
                        if not isinstance(route, str) or not route:
                            raise ValueError("graph tool output requires a non-empty route")
                        record = self.artifacts.put_json(
                            {
                                "schema_version": "zen.graph-node-output/1",
                                "graph_run_id": run_id,
                                "execution_id": execution["id"],
                                "lane": execution["lane_key"],
                                "node": execution["node_key"],
                                "round": execution["round_number"],
                                "role": node.role,
                                "tool": node.tool,
                                "result": output,
                            }
                        )
                        self.state.finish(execution["id"], route=route, output_sha256=record.sha256)
                        matched = [
                            edge
                            for edge in plan.edges
                            if edge.source == node.key
                            and edge.matches(route, execution["round_number"])
                        ]
                        if not node.terminal and not matched:
                            raise ValueError(
                                f"no graph edge matches route {route!r} from node {node.key!r}"
                            )
                        for edge in matched:
                            next_round = execution["round_number"] + edge.round_delta
                            if next_round >= plan.max_rounds:
                                raise ValueError("edge would exceed max_rounds")
                            target = plan.node(edge.target)
                            self.state.schedule(
                                run_id,
                                execution["lane_key"],
                                target.key,
                                next_round,
                                target.max_attempts,
                                {},
                            )
                    except Exception as exc:
                        self.state.fail(execution["id"], f"{type(exc).__name__}: {exc}")

    def resume(self, run_id: str) -> None:
        self.state.resume(run_id)
        self.execute(run_id)


def graph_artifact_store(config: HarnessConfig) -> ArtifactStore:
    return ArtifactStore(config.state_directory / "graph-artifacts")
