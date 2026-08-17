from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from .artifacts import ArtifactStore
from .config import HarnessConfig
from .models import ArtifactRecord, Plan, RunStatus, TaskStatus
from .plugins import PluginRegistry
from .policy import PolicyEngine
from .state import EventStore
from .tools import ToolContext


class Supervisor:
    def __init__(
        self,
        config: HarnessConfig,
        registry: PluginRegistry,
        state: EventStore,
        artifacts: ArtifactStore,
    ):
        self.config = config
        self.registry = registry
        self.state = state
        self.artifacts = artifacts
        self.policy = PolicyEngine(config)

    def plan(
        self, objective: str, inputs: dict[str, Any], workflow: str | None = None
    ) -> Plan:
        selected = self.registry.choose_workflow(objective, workflow)
        plan = selected.planner(objective, inputs, self.config.limits.max_attempts_per_task)
        if plan.workflow != selected.name:
            raise ValueError("planner returned a mismatched workflow id")
        if not plan.tasks:
            raise ValueError("a plan must contain at least one task")
        if len(plan.tasks) > self.config.limits.max_tasks_per_run:
            raise ValueError("plan exceeds max_tasks_per_run")
        keys = [task.key for task in plan.tasks]
        if len(keys) != len(set(keys)):
            raise ValueError("task keys must be unique")
        known = set(keys)
        for task in plan.tasks:
            if not set(task.depends_on) <= known:
                raise ValueError(f"task {task.key} has unknown dependency")
            if task.key in task.depends_on:
                raise ValueError(f"task {task.key} depends on itself")
            self.registry.tools.get(task.tool)
        return plan

    def start(
        self, objective: str, inputs: dict[str, Any], workflow: str | None = None
    ) -> str:
        plan = self.plan(objective, inputs, workflow)
        run_id = self.state.create_run(plan, self.config.fingerprint)
        self.execute(run_id)
        return run_id

    def execute(self, run_id: str) -> None:
        run = self.state.get_run(run_id)
        if run["config_fingerprint"] != self.config.fingerprint:
            self.state.set_run_status(run_id, RunStatus.BLOCKED, "configuration changed")
            return
        self.state.set_run_status(run_id, RunStatus.RUNNING)
        tool_calls = 0
        while True:
            tasks = self.state.list_tasks(run_id)
            required = [task for task in tasks if task["required"]]
            if required and all(task["status"] == TaskStatus.SUCCEEDED.value for task in required):
                manifest = self._completion_manifest(run_id, tasks)
                record = self.artifacts.put_json(manifest)
                self.state.register_artifact(run_id, None, record)
                self.state.set_run_status(run_id, RunStatus.SUCCEEDED, "completion predicate passed")
                return

            exhausted = [
                task
                for task in required
                if task["status"] == TaskStatus.FAILED.value
                and task["attempts"] >= task["max_attempts"]
            ]
            if exhausted:
                self.state.set_run_status(
                    run_id,
                    RunStatus.FAILED,
                    "required task exhausted attempts: " + ", ".join(item["task_key"] for item in exhausted),
                )
                return

            by_key = {task["task_key"]: task for task in tasks}
            ready = [
                task
                for task in tasks
                if task["status"] == TaskStatus.PENDING.value
                and all(by_key[key]["status"] == TaskStatus.SUCCEEDED.value for key in task["depends_on"])
            ]
            if not ready:
                self.state.set_run_status(run_id, RunStatus.BLOCKED, "no runnable task remains")
                return
            if tool_calls >= self.config.limits.max_tool_calls_per_run:
                self.state.set_run_status(run_id, RunStatus.BLOCKED, "tool-call budget exhausted")
                return

            task = self.state.start_task(ready[0]["id"])
            tool = self.registry.tools.get(task["tool"])
            decision = self.policy.evaluate(tool.name, tool.risk)
            self.state.event(
                run_id,
                "policy.evaluated",
                {"tool": tool.name, "risk": tool.risk.value, "effect": decision.effect, "reason": decision.reason},
                task["id"],
            )
            if decision.effect == "needs_approval":
                self.state.set_task_status(task["id"], TaskStatus.BLOCKED, error=decision.reason)
                self.state.set_run_status(run_id, RunStatus.NEEDS_HUMAN, decision.reason)
                return
            if decision.effect != "allow":
                self.state.set_task_status(task["id"], TaskStatus.BLOCKED, error=decision.reason)
                self.state.set_run_status(run_id, RunStatus.BLOCKED, decision.reason)
                return

            tool_calls += 1
            self.state.event(
                run_id, "tool.requested", {"tool": tool.name, "version": tool.version}, task["id"]
            )
            try:
                output = self.registry.tools.invoke(
                    tool.name,
                    ToolContext(run_id, task["id"], self.config.root),
                    task["inputs"],
                )
                record = self.artifacts.put_json(
                    {"tool": tool.name, "version": tool.version, "result": output}
                )
                self.state.register_artifact(run_id, task["id"], record)
                self.state.set_task_status(
                    task["id"], TaskStatus.SUCCEEDED, output_sha256=record.sha256
                )
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                self.state.event(run_id, "tool.failed", {"tool": tool.name, "error": message}, task["id"])
                status = (
                    TaskStatus.PENDING
                    if task["attempts"] < task["max_attempts"]
                    else TaskStatus.FAILED
                )
                self.state.set_task_status(task["id"], status, error=message)

    def resume(self, run_id: str) -> None:
        self.state.prepare_resume(run_id)
        self.execute(run_id)

    def verify(self, run_id: str) -> dict[str, Any]:
        run = self.state.get_run(run_id)
        tasks = self.state.list_tasks(run_id)
        checks = []
        for item in self.state.list_artifacts(run_id):
            record = ArtifactRecord(
                sha256=item["sha256"],
                relative_path=item["relative_path"],
                bytes=item["bytes"],
                media_type=item["media_type"],
            )
            checks.append({"sha256": record.sha256, "valid": self.artifacts.verify(record)})
        required_complete = all(
            task["status"] == TaskStatus.SUCCEEDED.value
            for task in tasks
            if task["required"]
        )
        valid = bool(checks) and all(item["valid"] for item in checks)
        return {
            "run_id": run_id,
            "recorded_status": run["status"],
            "required_tasks_complete": required_complete,
            "artifacts": checks,
            "valid": valid and required_complete and run["status"] == RunStatus.SUCCEEDED.value,
        }

    def _completion_manifest(self, run_id: str, tasks: list[dict[str, Any]]) -> dict[str, Any]:
        run = self.state.get_run(run_id)
        return {
            "schema_version": "1.0",
            "run_id": run_id,
            "objective": run["objective"],
            "workflow": run["workflow"],
            "config_fingerprint": run["config_fingerprint"],
            "completion_predicate": "all required tasks succeeded",
            "tasks": [
                {
                    "key": task["task_key"],
                    "tool": task["tool"],
                    "status": task["status"],
                    "attempts": task["attempts"],
                    "output_sha256": task["output_sha256"],
                }
                for task in tasks
            ],
        }
