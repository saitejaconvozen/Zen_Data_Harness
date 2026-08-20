from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
from typing import Any

from .artifacts import ArtifactStore
from .coding_runtime import CodingRuntime, CodingRuntimeLimits
from .coding_state import CodingStateStore
from .config import load_config
from .gateway import create_gateway_server
from .memory import MemoryStore
from .model_adapter import CodexExecAdapter
from .plugins import load_plugins
from .runtime import Supervisor
from .skills import SkillCatalog
from .state import EventStore


def _inputs(values: list[str]) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"input must be key=value: {value}")
        key, raw = value.split("=", 1)
        if not key or key in parsed:
            raise ValueError(f"invalid or duplicate input key: {key}")
        try:
            parsed[key] = json.loads(raw)
        except json.JSONDecodeError:
            parsed[key] = raw
    return parsed


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(prog="zen", description="Governed, resumable data-engine agent runtime")
    command.add_argument("--root", type=Path, default=Path.cwd(), help="harness project root")
    sub = command.add_subparsers(dest="command", required=True)
    sub.add_parser("plugins", help="list installed plugins, workflows, and tools")
    sub.add_parser("skills", help="list discovered skill metadata")
    sub.add_parser("runs", help="list recent runs")

    for name in ("plan", "run"):
        item = sub.add_parser(name, help=f"{name} an objective")
        item.add_argument("objective")
        item.add_argument("--workflow")
        item.add_argument("--input", action="append", default=[], metavar="KEY=VALUE")

    for name in ("status", "trace", "resume", "verify"):
        item = sub.add_parser(name, help=f"{name} a persisted run")
        item.add_argument("run_id")

    task = sub.add_parser("task", help="run a general autonomous coding task")
    task.add_argument("objective")
    task.add_argument("--workspace", type=Path, default=Path.cwd())
    task.add_argument("--max-turns", type=int, default=40)
    task.add_argument("--max-cycles", type=int, default=3)
    task.add_argument("--max-tool-calls", type=int, default=200)
    task.add_argument(
        "--agent", default="executor",
        help="agent manifest to execute with (see agents/); "
             "use 'data-engineer' to investigate the conversation factory",
    )

    task_status = sub.add_parser("task-status", help="inspect a coding task session")
    task_status.add_argument("session_id")
    task_status.add_argument("--events", action="store_true")

    task_list = sub.add_parser("task-list", help="list recent coding task sessions")
    task_list.add_argument("--limit", type=int, default=20)

    task_watch = sub.add_parser("task-watch", help="stream concise progress for a coding task")
    task_watch.add_argument("session_id")
    task_watch.add_argument("--interval", type=float, default=1.0)

    task_report = sub.add_parser("task-report", help="show a concise coding task result")
    task_report.add_argument("session_id")
    task_report.add_argument("--json", action="store_true", dest="as_json")

    task_resume = sub.add_parser("task-resume", help="resume a coding task with durable context")
    task_resume.add_argument("session_id")
    task_resume.add_argument("--workspace", type=Path)
    task_resume.add_argument("--max-turns", type=int, default=40)
    task_resume.add_argument("--max-cycles", type=int, default=3)

    task_feedback = sub.add_parser("task-feedback", help="append human feedback or steering")
    task_feedback.add_argument("session_id")
    task_feedback.add_argument("message")
    task_feedback.add_argument("--steer", action="store_true")

    task_cancel = sub.add_parser("task-cancel", help="request cooperative task cancellation")
    task_cancel.add_argument("session_id")
    task_cancel.add_argument("--reason")

    task_serve = sub.add_parser("task-serve", help="serve the local coding-session control API")
    task_serve.add_argument("--host", default="127.0.0.1")
    task_serve.add_argument("--port", type=int, default=8787)
    return command


def _build(root: Path):
    config = load_config(root)
    registry = load_plugins(config.plugin_paths)
    state = EventStore(config.state_directory / "state.db")
    artifacts = ArtifactStore(config.state_directory / "artifacts")
    supervisor = Supervisor(config, registry, state, artifacts)
    skill_roots = [config.root / ".agents" / "skills"]
    skill_roots.extend(path / plugin_id / "skills" for path in config.plugin_paths for plugin_id in registry.manifests)
    skills = SkillCatalog.discover(skill_roots)
    return config, registry, state, supervisor, skills


def _progress_line(event: str, payload: dict[str, Any]) -> str:
    if event == "session.created":
        return f"session {payload['session_id']} created"
    if event == "session.status":
        reason = payload.get("reason")
        return f"status {payload['status']}" + (f": {reason}" if reason else "")
    if event == "iteration.started":
        maximum = payload.get("maximum")
        suffix = f"/{maximum}" if maximum is not None else ""
        return f"verification cycle {payload['cycle']}{suffix} started"
    if event == "model.requested":
        return f"{payload['agent']} model running: {payload['phase']}"
    if event == "model.responded":
        return f"{payload['agent']} model responded: {payload['phase']}"
    if event == "agent.action":
        target = f" {payload['tool']}" if payload.get("tool") else ""
        return (
            f"{payload['agent']} turn {payload['turn']}: {payload['kind']}{target}"
            f" — {payload.get('summary', '')}"
        )
    if event == "tool.started":
        return f"tool {payload['tool']} started"
    if event == "tool.finished":
        line = f"tool {payload['tool']} {payload['status']}"
        return line + (f": {payload['error']}" if payload.get("error") else "")
    if event == "verification.completed":
        return f"verifier {payload['verdict']}: {payload['summary']}"
    if event == "replan.requested":
        return f"verifier requested repair after cycle {payload['cycle']}"
    return f"{event}: {json.dumps(payload, ensure_ascii=False)}"


def _agent_model_adapter(config):
    """Model transport for the agent runtime, chosen the same way workers choose.

    Reads `.zen/model-provider` so the agent and the factory cannot silently
    disagree about which model is in use.
    """
    from .litellm_adapter import LiteLLMAdapter

    marker = config.state_directory / "model-provider"
    provider = (
        os.environ.get("ZEN_MODEL_PROVIDER")
        or (marker.read_text(encoding="utf-8").strip() if marker.is_file() else "")
        or "codex"
    ).strip().lower()
    if provider == "litellm":
        return LiteLLMAdapter(reasoning_effort=os.environ.get("ZEN_AGENT_REASONING", "medium"))
    return CodexExecAdapter(model=config.default_model)


def _print_progress(event: str, payload: dict[str, Any]) -> None:
    print(f"[zen] {_progress_line(event, payload)}", file=sys.stderr, flush=True)


def _watch_session(state: CodingStateStore, session_id: str, interval: float) -> int:
    if interval <= 0:
        raise ValueError("watch interval must be positive")
    terminal = {"WAITING_FOR_HUMAN", "PAUSED", "SUCCEEDED", "FAILED", "CANCELLED"}
    cursor = 0
    print(f"[zen] watching session {session_id}", flush=True)
    while True:
        for event in state.list_events(session_id, after=cursor):
            cursor = event["id"]
            event_type = event["event_type"]
            payload = event["payload"]
            if event_type in {
                "model.requested", "model.responded", "iteration.started",
                "verification.completed", "replan.requested",
            }:
                print(f"[zen] {_progress_line(event_type, payload)}", flush=True)
            elif event_type == "tool.started":
                print(f"[zen] tool {payload['tool_name']} started", flush=True)
            elif event_type == "tool.finished":
                print(
                    f"[zen] tool call {payload['status']}"
                    + (f": {payload['error']}" if payload.get("error") else ""),
                    flush=True,
                )
            elif event_type == "session.status_changed":
                print(
                    f"[zen] status {payload['to']}"
                    + (f": {payload['reason']}" if payload.get("reason") else ""),
                    flush=True,
                )
        session = state.get_session(session_id)
        if session["status"] in terminal:
            print(
                f"[zen] terminal status {session['status']}"
                + (f": {session['terminal_reason']}" if session.get("terminal_reason") else ""),
                flush=True,
            )
            return 0 if session["status"] == "SUCCEEDED" else 2
        time.sleep(interval)


def _task_report(state: CodingStateStore, session_id: str) -> dict[str, Any]:
    session = state.get_session(session_id)
    turns = state.list_turns(session_id)
    calls = state.list_tool_calls(session_id)
    plan = next(
        (
            turn["content"]
            for turn in turns
            if turn["agent_name"] == "planner" and isinstance(turn["content"], dict)
        ),
        None,
    )
    verdicts = [
        turn["content"]
        for turn in turns
        if turn["agent_name"] == "verifier"
        and isinstance(turn["content"], dict)
        and "verdict" in turn["content"]
    ]
    tests = []
    changes = []
    counts: dict[str, dict[str, int]] = {}
    for call in calls:
        name = call["tool_name"]
        status = call["status"]
        counts.setdefault(name, {})
        counts[name][status] = counts[name].get(status, 0) + 1
        if name == "process.run":
            result = call.get("result") or {}
            tests.append(
                {
                    "argv": call["arguments"].get("argv", []),
                    "status": status,
                    "returncode": result.get("returncode"),
                    "timed_out": result.get("timed_out"),
                    "stdout_tail": str(result.get("stdout", ""))[-2_000:],
                    "stderr_tail": str(result.get("stderr", ""))[-2_000:],
                    "error": call.get("error"),
                }
            )
        if name in {"fs.write", "fs.replace"} and status == "SUCCEEDED":
            changes.append(
                {
                    "tool": name,
                    "path": call["arguments"].get("path"),
                    "result": call.get("result"),
                }
            )
    return {
        "session_id": session_id,
        "objective": session["objective"],
        "status": session["status"],
        "terminal_reason": session["terminal_reason"],
        "model": session["model"],
        "workspace": session["workspace"],
        "plan_summary": plan.get("summary") if plan else None,
        "latest_verdict": verdicts[-1] if verdicts else None,
        "tests": tests,
        "workspace_changes": changes,
        "tool_summary": counts,
        "turn_count": len(turns),
        "tool_call_count": len(calls),
    }


def _print_task_report(report: dict[str, Any]) -> None:
    print(f"# Zen task {report['session_id']}")
    print(f"Status: {report['status']}")
    print(f"Model: {report['model']}")
    print(f"Workspace: {report['workspace']}")
    print(f"Objective: {report['objective']}")
    if report["terminal_reason"]:
        print(f"Result: {report['terminal_reason']}")
    if report["plan_summary"]:
        print(f"Plan: {report['plan_summary']}")
    verdict = report["latest_verdict"]
    if verdict:
        print(f"Verifier: {verdict['verdict']} — {verdict['summary']}")
    print(f"Workspace file changes: {len(report['workspace_changes'])}")
    print(f"Tool calls: {report['tool_call_count']}; turns: {report['turn_count']}")
    if report["tests"]:
        print("Checks:")
        for check in report["tests"]:
            command = " ".join(str(item) for item in check["argv"])
            print(
                f"- {check['status']} returncode={check['returncode']} timed_out={check['timed_out']}: {command}"
            )
            evidence = check["stderr_tail"].strip() or check["stdout_tail"].strip()
            if evidence:
                print("  " + evidence.replace("\n", "\n  "))


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    state = None
    coding_state = None
    memory = None
    try:
        if args.command.startswith("task"):
            config = load_config(args.root)
            coding_state = CodingStateStore(config.state_directory / "coding-state.db")
            if args.command == "task-list":
                print(json.dumps(coding_state.list_sessions(limit=args.limit), indent=2))
                return 0
            if args.command == "task-watch":
                return _watch_session(coding_state, args.session_id, args.interval)
            if args.command == "task-report":
                report = _task_report(coding_state, args.session_id)
                if args.as_json:
                    print(json.dumps(report, indent=2))
                else:
                    _print_task_report(report)
                return 0 if report["status"] == "SUCCEEDED" else 2
            if args.command == "task-status":
                output = {
                    "session": coding_state.get_session(args.session_id),
                    "turns": coding_state.list_turns(args.session_id),
                    "tool_calls": coding_state.list_tool_calls(args.session_id),
                    "feedback": coding_state.list_feedback(args.session_id),
                }
                if args.events:
                    output["events"] = coding_state.list_events(args.session_id)
                print(json.dumps(output, indent=2))
                return 0
            if args.command == "task-feedback":
                method = coding_state.add_steering if args.steer else coding_state.add_feedback
                print(json.dumps(method(args.session_id, args.message), indent=2))
                return 0
            if args.command == "task-cancel":
                print(
                    json.dumps(
                        coding_state.request_cancel(args.session_id, reason=args.reason), indent=2
                    )
                )
                return 0
            if args.command == "task-serve":
                server = create_gateway_server(
                    coding_state, host=args.host, port=args.port
                )
                address, port = server.server_address
                print(
                    json.dumps(
                        {"gateway": f"http://{address}:{port}", "state": str(coding_state.path)}
                    ),
                    flush=True,
                )
                server.serve_forever()
                return 0
            if args.command == "task-resume":
                session = coding_state.get_session(args.session_id)
                workspace = args.workspace or Path(session["workspace"])
                limits = CodingRuntimeLimits(
                    max_executor_turns=args.max_turns,
                    max_verification_cycles=args.max_cycles,
                )
                session_id = args.session_id
            else:
                workspace = args.workspace
                limits = CodingRuntimeLimits(
                    max_executor_turns=args.max_turns,
                    max_verification_cycles=args.max_cycles,
                    max_tool_calls=args.max_tool_calls,
                )
                session_id = None
            memory = MemoryStore(
                config.state_directory / "coding-memory.db",
                curated_path=config.state_directory / "project-memory.md",
            )
            runtime = CodingRuntime(
                harness_root=config.root,
                workspace=workspace,
                state=coding_state,
                # Which provider backs the agent loop is a setting, not a
                # constant. Pinning it to Codex made the whole agentic layer
                # unrunnable the moment that workspace ran out of credits.
                model=_agent_model_adapter(config),
                limits=limits,
                memory=memory,
                progress=_print_progress,
                executor_agent=getattr(args, "agent", "executor"),
            )
            if session_id is None:
                session_id = runtime.start(args.objective)
            else:
                runtime.resume(session_id)
            result = coding_state.get_session(session_id)
            print(
                json.dumps(
                    {
                        "session": result,
                        "turn_count": len(coding_state.list_turns(session_id)),
                        "tool_call_count": len(coding_state.list_tool_calls(session_id)),
                        "inspect": f"zen --root {config.root} task-status {session_id} --events",
                    },
                    indent=2,
                )
            )
            return 0 if result["status"] == "SUCCEEDED" else 2

        config, registry, state, supervisor, skills = _build(args.root)
        if args.command == "plugins":
            print(json.dumps({"plugins": registry.manifests, "workflows": sorted(registry.workflows), "tools": registry.tools.names()}, indent=2))
            return 0
        if args.command == "skills":
            print(json.dumps([{"name": item.name, "description": item.description, "path": str(item.path)} for item in skills.list()], indent=2))
            return 0
        if args.command == "runs":
            print(json.dumps(state.list_runs(), indent=2))
            return 0
        if args.command in {"plan", "run"}:
            inputs = _inputs(args.input)
            plan = supervisor.plan(args.objective, inputs, args.workflow)
            selected_skills = skills.select(args.objective)
            if args.command == "plan":
                output = plan.to_dict()
                output["selected_skills"] = list(selected_skills)
                print(json.dumps(output, indent=2))
                return 0
            run_id = supervisor.start(args.objective, inputs, args.workflow)
            print(json.dumps({"run": state.get_run(run_id), "tasks": state.list_tasks(run_id), "selected_skills": list(selected_skills)}, indent=2))
            return 0 if state.get_run(run_id)["status"] == "SUCCEEDED" else 2
        if args.command == "status":
            print(json.dumps({"run": state.get_run(args.run_id), "tasks": state.list_tasks(args.run_id), "artifacts": state.list_artifacts(args.run_id)}, indent=2))
            return 0
        if args.command == "trace":
            print(json.dumps(state.trace(args.run_id), indent=2))
            return 0
        if args.command == "resume":
            supervisor.resume(args.run_id)
            print(json.dumps({"run": state.get_run(args.run_id), "tasks": state.list_tasks(args.run_id)}, indent=2))
            return 0 if state.get_run(args.run_id)["status"] == "SUCCEEDED" else 2
        if args.command == "verify":
            result = supervisor.verify(args.run_id)
            print(json.dumps(result, indent=2))
            return 0 if result["valid"] else 3
        raise AssertionError(args.command)
    except KeyboardInterrupt:
        print("interrupted; use zen resume <run-id>", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"zen failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        if memory is not None:
            memory.close()
        if coding_state is not None:
            coding_state.close()
        if state is not None:
            state.close()


if __name__ == "__main__":
    raise SystemExit(main())
