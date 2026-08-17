from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Callable


@dataclass(frozen=True, slots=True)
class LaneSpec:
    key: str
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class GraphNodeSpec:
    key: str
    role: str
    tool: str
    inputs: dict[str, Any]
    max_attempts: int = 2
    terminal: bool = False
    priority: int = 0


@dataclass(frozen=True, slots=True)
class GraphEdgeSpec:
    source: str
    target: str
    routes: tuple[str, ...]
    round_delta: int = 0
    min_round: int = 0
    max_round: int | None = None

    def matches(self, route: str, round_number: int) -> bool:
        return (
            ("*" in self.routes or route in self.routes)
            and round_number >= self.min_round
            and (self.max_round is None or round_number <= self.max_round)
        )


@dataclass(frozen=True, slots=True)
class GraphPlan:
    graph: str
    objective: str
    start_node: str
    lanes: tuple[LaneSpec, ...]
    nodes: tuple[GraphNodeSpec, ...]
    edges: tuple[GraphEdgeSpec, ...]
    max_rounds: int
    max_parallel_workers: int
    max_node_executions: int
    inputs: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "GraphPlan":
        return cls(
            graph=value["graph"],
            objective=value["objective"],
            start_node=value["start_node"],
            lanes=tuple(LaneSpec(**item) for item in value["lanes"]),
            nodes=tuple(GraphNodeSpec(**item) for item in value["nodes"]),
            edges=tuple(
                GraphEdgeSpec(
                    source=item["source"],
                    target=item["target"],
                    routes=tuple(item["routes"]),
                    round_delta=item.get("round_delta", 0),
                    min_round=item.get("min_round", 0),
                    max_round=item.get("max_round"),
                )
                for item in value["edges"]
            ),
            max_rounds=value["max_rounds"],
            max_parallel_workers=value["max_parallel_workers"],
            max_node_executions=value["max_node_executions"],
            inputs=value.get("inputs", {}),
        )

    def node(self, key: str) -> GraphNodeSpec:
        try:
            return next(item for item in self.nodes if item.key == key)
        except StopIteration as exc:
            raise KeyError(key) from exc

    def lane(self, key: str) -> LaneSpec:
        try:
            return next(item for item in self.lanes if item.key == key)
        except StopIteration as exc:
            raise KeyError(key) from exc

    def validate(self, known_tools: set[str]) -> None:
        if not self.lanes or not self.nodes:
            raise ValueError("graph requires lanes and nodes")
        if not 1 <= self.max_parallel_workers <= 32:
            raise ValueError("max_parallel_workers must be between 1 and 32")
        if not 1 <= self.max_rounds <= 10:
            raise ValueError("max_rounds must be between 1 and 10")
        if self.max_node_executions < len(self.lanes):
            raise ValueError("max_node_executions is smaller than lane count")
        lane_keys = [item.key for item in self.lanes]
        node_keys = [item.key for item in self.nodes]
        if len(lane_keys) != len(set(lane_keys)) or len(node_keys) != len(set(node_keys)):
            raise ValueError("graph lane/node keys must be unique")
        if self.start_node not in node_keys:
            raise ValueError("start node is unknown")
        if not any(item.terminal for item in self.nodes):
            raise ValueError("graph requires a terminal node")
        for node in self.nodes:
            if node.tool not in known_tools:
                raise ValueError(f"graph node {node.key} uses unknown tool {node.tool}")
            if node.max_attempts < 1:
                raise ValueError("node max_attempts must be positive")
            if not 0 <= node.priority <= 100:
                raise ValueError("node priority must be between 0 and 100")
        for edge in self.edges:
            if edge.source not in node_keys or edge.target not in node_keys:
                raise ValueError("graph edge references unknown node")
            if not edge.routes:
                raise ValueError("graph edge requires at least one route")
            if edge.round_delta not in {0, 1}:
                raise ValueError("round_delta must be zero or one")
            if edge.max_round is not None and edge.max_round >= self.max_rounds:
                raise ValueError("edge max_round must be below graph max_rounds")
        zero_edges = {key: [] for key in node_keys}
        for edge in self.edges:
            if edge.round_delta == 0:
                zero_edges[edge.source].append(edge.target)
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(key: str) -> None:
            if key in visiting:
                raise ValueError("zero-round cycle is forbidden")
            if key in visited:
                return
            visiting.add(key)
            for target in zero_edges[key]:
                visit(target)
            visiting.remove(key)
            visited.add(key)

        for key in node_keys:
            visit(key)


def resolve_inputs(value: Any, lane: LaneSpec, round_number: int, run_id: str) -> Any:
    if isinstance(value, dict):
        if set(value) == {"$lane"}:
            return lane.payload[value["$lane"]]
        if set(value) == {"$round"} and value["$round"] is True:
            return round_number
        if set(value) == {"$run_id"} and value["$run_id"] is True:
            return run_id
        return {key: resolve_inputs(item, lane, round_number, run_id) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_inputs(item, lane, round_number, run_id) for item in value]
    return value


GraphPlanner = Callable[[str, dict[str, Any]], GraphPlan]


@dataclass(frozen=True, slots=True)
class GraphSpec:
    name: str
    description: str
    triggers: tuple[str, ...]
    planner: GraphPlanner


class GraphCatalog:
    def __init__(self):
        self.graphs: dict[str, GraphSpec] = {}

    def register(self, spec: GraphSpec) -> None:
        if spec.name in self.graphs:
            raise ValueError(f"duplicate graph: {spec.name}")
        self.graphs[spec.name] = spec

    def choose(self, objective: str, explicit: str | None = None) -> GraphSpec:
        if explicit:
            try:
                return self.graphs[explicit]
            except KeyError as exc:
                raise ValueError(f"unknown graph: {explicit}") from exc
        lowered = objective.casefold()
        ranked = sorted(
            (
                sum(trigger.casefold() in lowered for trigger in item.triggers),
                item.name,
                item,
            )
            for item in self.graphs.values()
        )
        if not ranked or ranked[-1][0] == 0:
            raise ValueError("objective matches no registered graph")
        return ranked[-1][2]
