from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .config import load_config
from .graph_loader import load_graphs
from .graph_runtime import GraphSupervisor, graph_artifact_store
from .graph_state import GraphState
from .plugins import load_plugins


def _inputs(values: list[str]) -> dict:
    output = {}
    for value in values:
        key, separator, raw = value.partition("=")
        if not separator or not key or key in output:
            raise ValueError(f"input must be unique key=value: {value}")
        try:
            output[key] = json.loads(raw)
        except json.JSONDecodeError:
            output[key] = raw
    return output


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(prog="zen-graph", description="Bounded cyclic multi-agent graph runtime")
    command.add_argument("--root", type=Path, default=Path.cwd())
    sub = command.add_subparsers(dest="command", required=True)
    sub.add_parser("graphs")
    for name in ("plan", "run"):
        item = sub.add_parser(name)
        item.add_argument("objective")
        item.add_argument("--graph")
        item.add_argument("--input", action="append", default=[])
    for name in ("status", "trace", "resume"):
        item = sub.add_parser(name)
        item.add_argument("run_id")
    return command


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    state = None
    try:
        config = load_config(args.root)
        plugins = load_plugins(config.plugin_paths)
        graphs = load_graphs(config.plugin_paths, plugins.tools)
        state = GraphState(config.state_directory / "graph-state.db")
        supervisor = GraphSupervisor(config, plugins.tools, state, graph_artifact_store(config))
        if args.command == "graphs":
            print(json.dumps({key: {"description": item.description, "triggers": item.triggers} for key, item in graphs.graphs.items()}, indent=2))
            return 0
        if args.command in {"plan", "run"}:
            inputs = _inputs(args.input)
            spec = graphs.choose(args.objective, args.graph)
            plan = spec.planner(args.objective, inputs)
            plan.validate(set(plugins.tools.names()))
            if args.command == "plan":
                print(json.dumps(plan.to_dict(), indent=2))
                return 0
            run_id = supervisor.start(plan)
            print(json.dumps({"run": state.run(run_id), "executions": state.executions(run_id)}, indent=2))
            return 0 if state.run(run_id)["status"] == "SUCCEEDED" else 2
        if args.command == "status":
            print(json.dumps({"run": state.run(args.run_id), "executions": state.executions(args.run_id)}, indent=2))
            return 0
        if args.command == "trace":
            print(json.dumps(state.trace(args.run_id), indent=2))
            return 0
        if args.command == "resume":
            supervisor.resume(args.run_id)
            print(json.dumps({"run": state.run(args.run_id), "executions": state.executions(args.run_id)}, indent=2))
            return 0 if state.run(args.run_id)["status"] == "SUCCEEDED" else 2
        raise AssertionError(args.command)
    except Exception as exc:
        print(f"zen-graph failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        if state is not None:
            state.close()


if __name__ == "__main__":
    raise SystemExit(main())
