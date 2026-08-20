from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
import json
from pathlib import Path
from typing import Any, Callable

from .agent_manifests import AgentCatalog, AgentManifest
from .agent_protocol import ACTION_SCHEMA, PLAN_SCHEMA, VERDICT_SCHEMA, AgentAction
from .coding_state import CodingStateStore
from .coding_tools import register_coding_tools
from .config import PINNED_MODEL
from .context import WorkspaceContextCompiler
from .hooks import HookConfig, HookResult, HookRunner
from .memory import MemoryStore
from .model_adapter import ModelAdapter
from .models import ToolRisk
from .skills import SkillCatalog
from .data_tools import register_data_tools
from .tools import ToolContext, ToolRegistry
from .workspace import Workspace


@dataclass(frozen=True, slots=True)
class CodingRuntimeLimits:
    max_executor_turns: int = 40
    max_verification_cycles: int = 3
    max_tool_calls: int = 200
    max_delegates: int = 3
    max_delegate_turns: int = 12
    max_prompt_chars: int = 120_000

    def __post_init__(self) -> None:
        values = (
            self.max_executor_turns,
            self.max_verification_cycles,
            self.max_tool_calls,
            self.max_delegates,
            self.max_delegate_turns,
            self.max_prompt_chars,
        )
        if min(values) < 1:
            raise ValueError("coding runtime limits must be positive")


class CodingRuntime:
    """Persistent planner/executor/verifier loop whose tools are owned by Zen."""

    def __init__(
        self,
        *,
        harness_root: Path,
        workspace: Path,
        state: CodingStateStore,
        model: ModelAdapter,
        limits: CodingRuntimeLimits | None = None,
        agents: AgentCatalog | None = None,
        skills: SkillCatalog | None = None,
        hooks: HookRunner | None = None,
        memory: MemoryStore | None = None,
        progress: Callable[[str, dict[str, Any]], None] | None = None,
        executor_agent: str = "executor",
    ):
        self.harness_root = harness_root.resolve()
        self.workspace = Workspace(workspace).root
        self.state = state
        self.model = model
        self.limits = limits or CodingRuntimeLimits()
        self.agents = agents or AgentCatalog.discover([self.harness_root / "agents"])
        self.skills = skills or SkillCatalog.discover([self.harness_root / ".agents" / "skills"])
        self.hooks = hooks or HookRunner(
            HookConfig.load(self.workspace / ".zen" / "hooks.json"), self.workspace
        )
        self.memory = memory
        self.progress = progress
        # Which manifest does the executing work. The default writes code; the
        # `data-engineer` manifest investigates the conversation factory instead.
        # Both run the same loop — only the tool allowlist and instructions differ.
        self.executor_agent = executor_agent
        self.tools = ToolRegistry()
        register_coding_tools(self.tools)
        # The agent kernel and the data factory were built as separate systems
        # with separate registries, so an agent could read files and run git but
        # could not see a single conversation the factory had processed. These
        # give it that domain: the factory's own ledgers, decisions and
        # contracts. Which of them any agent may actually call is still bounded
        # by its manifest's `tools:` allowlist.
        register_data_tools(self.tools)

    def tool_catalog(self, names: set[str] | None = None) -> list[dict[str, Any]]:
        """Describe the registered tools a model is allowed to choose from.

        Built from the live registry rather than a fixed list. The delegate path
        used a coding-only catalog, so an agent whose manifest allowed `data.*`
        was still shown nothing but `fs.*` — and a model can only pick what it
        is shown. It searched the filesystem for conversation data that lives in
        SQLite.
        """
        catalog = []
        for name in sorted(self.tools.names()):
            if names is not None and name not in names:
                continue
            spec = self.tools.get(name)
            catalog.append({
                "name": spec.name,
                "description": spec.description,
                "risk": spec.risk.value,
                "input_schema": spec.input_schema,
            })
        return catalog

    def start(self, objective: str) -> str:
        identifier = self.state.create_session(
            objective,
            self.workspace,
            model=PINNED_MODEL,
            agent_name="coordinator",
            metadata={"runtime": "zen-coding-v1"},
        )
        self._notify(
            "session.created",
            {"session_id": identifier, "objective": objective, "workspace": str(self.workspace)},
        )
        self.execute(identifier)
        return identifier

    def resume(self, session_id: str) -> None:
        session = self.state.get_session(session_id)
        if Path(session["workspace"]).resolve() != self.workspace:
            raise ValueError("session workspace does not match this runtime")
        if session["status"] == "CANCELLED":
            raise ValueError("cancelled sessions cannot be resumed")
        self.state.clear_cancel_request(session_id)
        self.execute(session_id)

    def execute(self, session_id: str) -> None:
        session = self.state.get_session(session_id)
        objective = session["objective"]
        session_result = self._emit(session_id, "SessionStart", {"objective": objective})
        if not session_result.allowed:
            self.state.update_session_status(
                session_id, "WAITING_FOR_HUMAN", reason="SessionStart hook blocked execution"
            )
            return
        try:
            self.state.update_session_status(session_id, "RUNNING")
            self._notify("session.status", {"session_id": session_id, "status": "RUNNING"})
            context = self._context(objective)
            plan = self._existing_plan(session_id) or self._plan(session_id, objective, context)
            feedback = self._pending_feedback(session_id)
            for cycle in range(1, self.limits.max_verification_cycles + 1):
                if self._cancelled(session_id):
                    return
                self.state.append_event(session_id, "iteration.started", {"cycle": cycle})
                self._notify(
                    "iteration.started",
                    {
                        "session_id": session_id,
                        "cycle": cycle,
                        "maximum": self.limits.max_verification_cycles,
                    },
                )
                claim = self._execute_worker(
                    session_id, objective, context, plan, cycle=cycle, feedback=feedback
                )
                if claim is None:
                    return
                self.state.update_session_status(session_id, "VERIFYING")
                verdict = self._verify(session_id, objective, context, plan, claim, cycle)
                self.state.add_turn(
                    session_id, "assistant", verdict, agent_name="verifier"
                )
                self.state.append_event(session_id, "verification.completed", verdict)
                if verdict["verdict"] == "NEEDS_HUMAN":
                    self.state.update_session_status(
                        session_id, "WAITING_FOR_HUMAN", reason=verdict["summary"]
                    )
                    self._notify(
                        "session.status",
                        {
                            "session_id": session_id,
                            "status": "WAITING_FOR_HUMAN",
                            "reason": verdict["summary"],
                        },
                    )
                    return
                if verdict["verdict"] == "PASS":
                    gate = self._emit(
                        session_id,
                        "BeforeComplete",
                        {"cycle": cycle, "verdict": verdict, "claim": claim},
                    )
                    if gate.allowed:
                        self.state.update_session_status(
                            session_id, "SUCCEEDED", reason=verdict["summary"]
                        )
                        self._notify(
                            "session.status",
                            {
                                "session_id": session_id,
                                "status": "SUCCEEDED",
                                "reason": verdict["summary"],
                            },
                        )
                        self._remember(session_id, objective, verdict)
                        return
                    feedback = list(gate.feedback) or ["BeforeComplete hook blocked completion"]
                else:
                    feedback = verdict["findings"] + verdict["recommended_actions"]
                self.state.update_session_status(
                    session_id, "RUNNING", reason=f"verification cycle {cycle} requested repair"
                )
                self.state.append_event(
                    session_id, "replan.requested", {"cycle": cycle, "feedback": feedback}
                )
                self._notify(
                    "replan.requested",
                    {"session_id": session_id, "cycle": cycle, "feedback": feedback},
                )
            self.state.update_session_status(
                session_id,
                "FAILED",
                reason=f"verification did not pass after {self.limits.max_verification_cycles} cycles",
            )
            self._notify(
                "session.status",
                {
                    "session_id": session_id,
                    "status": "FAILED",
                    "reason": "verification cycle budget exhausted",
                },
            )
        except Exception as exc:
            current = self.state.get_session(session_id)
            if current["status"] not in {"CANCELLED", "WAITING_FOR_HUMAN"}:
                self.state.append_event(
                    session_id,
                    "runtime.failed",
                    {"type": type(exc).__name__, "message": str(exc)},
                )
                self.state.update_session_status(
                    session_id, "FAILED", reason=f"{type(exc).__name__}: {exc}"
                )
            raise
        finally:
            final = self.state.get_session(session_id)
            self._emit(
                session_id,
                "SessionEnd",
                {"status": final["status"], "reason": final["terminal_reason"]},
            )

    def _plan(self, session_id: str, objective: str, context: str) -> dict[str, Any]:
        manifest = self._agent("planner")
        self._agent_event(session_id, "AgentStart", manifest, {})
        prompt = self._bounded(
            context
            + "\n\n# Planner manifest instructions\n"
            + manifest.instructions
            + "\n\n# Planner responsibility\nCreate a small executable plan. "
            + "Every step must name observable verification. Do not call tools."
        )
        plan = self._generate(
            session_id, manifest.name, "plan", prompt, PLAN_SCHEMA
        )
        self._validate_plan(plan)
        self.state.add_turn(session_id, "assistant", plan, agent_name=manifest.name)
        self._agent_event(session_id, "AgentStop", manifest, {"outcome": "plan_created"})
        return plan

    def _execute_worker(
        self,
        session_id: str,
        objective: str,
        context: str,
        plan: dict[str, Any],
        *,
        cycle: int,
        feedback: list[str],
    ) -> str | None:
        manifest = self._agent(self.executor_agent)
        allowed_tools = self._allowed_tools(manifest)
        context = context + "\n\n# Executor manifest instructions\n" + manifest.instructions
        self._agent_event(session_id, "AgentStart", manifest, {"cycle": cycle})
        observations: list[dict[str, Any]] = []
        for turn_number in range(1, min(manifest.max_turns, self.limits.max_executor_turns) + 1):
            if self._cancelled(session_id):
                return None
            prompt = self._executor_prompt(
                objective, context, plan, allowed_tools, observations, feedback, cycle, turn_number
            )
            raw = self._generate(
                session_id,
                manifest.name,
                f"execute turn {turn_number}",
                prompt,
                ACTION_SCHEMA,
            )
            action = AgentAction.from_dict(raw)
            self._notify(
                "agent.action",
                {
                    "session_id": session_id,
                    "agent": manifest.name,
                    "turn": turn_number,
                    "kind": action.kind,
                    "tool": action.tool,
                    "summary": action.reasoning_summary,
                },
            )
            turn = self.state.add_turn(
                session_id, "assistant", raw, agent_name=manifest.name
            )
            if action.kind == "ask_human":
                self.state.add_feedback(
                    session_id, action.message, author=manifest.name, kind="feedback"
                )
                self.state.update_session_status(
                    session_id, "WAITING_FOR_HUMAN", reason=action.message
                )
                self._notify(
                    "session.status",
                    {
                        "session_id": session_id,
                        "status": "WAITING_FOR_HUMAN",
                        "reason": action.message,
                    },
                )
                self._agent_event(
                    session_id, "AgentStop", manifest, {"outcome": "needs_human"}
                )
                return None
            if action.kind == "final":
                gate = self._agent_event(
                    session_id,
                    "AgentStop",
                    manifest,
                    {"outcome": "claimed_complete", "message": action.message},
                )
                if not gate.allowed:
                    observations.append({"hook_feedback": list(gate.feedback)})
                    continue
                return action.message
            if action.kind == "delegate":
                result = self._delegate(session_id, action.arguments, context)
                observation = {"delegation": result}
            else:
                assert action.tool is not None
                observation = self._invoke_tool(
                    session_id,
                    turn["id"],
                    action.tool,
                    action.arguments,
                    allowed_tools=allowed_tools,
                )
            observations.append(observation)
            self.state.add_turn(
                session_id, "tool", observation, agent_name="zen-tool-broker"
            )
        self._agent_event(
            session_id, "AgentStop", manifest, {"outcome": "turn_budget_exhausted"}
        )
        self.state.update_session_status(
            session_id, "FAILED", reason="executor turn budget exhausted"
        )
        return None

    def _delegate(
        self, parent_session_id: str, arguments: dict[str, Any], context: str
    ) -> list[dict[str, Any]]:
        tasks = arguments.get("tasks")
        if not isinstance(tasks, list) or not tasks or len(tasks) > self.limits.max_delegates:
            return [{"error": f"delegate requires 1-{self.limits.max_delegates} tasks"}]
        normalized: list[dict[str, str]] = []
        for item in tasks:
            if not isinstance(item, dict) or not isinstance(item.get("objective"), str):
                return [{"error": "each delegated task requires an objective"}]
            normalized.append(
                {"objective": item["objective"].strip(), "agent": str(item.get("agent", "investigator"))}
            )
        self.state.append_event(
            parent_session_id, "delegation.started", {"tasks": normalized}
        )
        with ThreadPoolExecutor(max_workers=min(self.limits.max_delegates, len(normalized))) as pool:
            futures = [
                pool.submit(self._run_delegate, parent_session_id, item, context)
                for item in normalized
            ]
            results = [future.result() for future in futures]
        self.state.append_event(
            parent_session_id, "delegation.completed", {"results": results}
        )
        return results

    def _run_delegate(
        self, parent_session_id: str, item: dict[str, str], context: str
    ) -> dict[str, Any]:
        manifest = self._agent(item["agent"])
        context = context + "\n\n# Delegate manifest instructions\n" + manifest.instructions
        allowed = {
            name
            for name in self._allowed_tools(manifest)
            if self.tools.get(name).risk == ToolRisk.READ_ONLY
        }
        child = self.state.create_session(
            item["objective"],
            self.workspace,
            model=PINNED_MODEL,
            agent_name=manifest.name,
            parent_session_id=parent_session_id,
        )
        self.state.update_session_status(child, "RUNNING")
        observations: list[dict[str, Any]] = []
        for _ in range(min(manifest.max_turns, self.limits.max_delegate_turns)):
            prompt = self._bounded(
                context
                + "\n\n# Delegated investigation\n"
                + item["objective"]
                + "\nUse only these read-only tools:\n"
                + json.dumps(self.tool_catalog(allowed))
                + "\nObservations:\n"
                + json.dumps(observations[-8:])
                + "\nReturn final when you have evidence. Do not request edits."
            )
            raw = self._generate(
                child, manifest.name, "delegated investigation", prompt, ACTION_SCHEMA
            )
            action = AgentAction.from_dict(raw)
            turn = self.state.add_turn(child, "assistant", raw, agent_name=manifest.name)
            if action.kind == "final":
                self.state.update_session_status(child, "SUCCEEDED", reason=action.message)
                return {"session_id": child, "objective": item["objective"], "result": action.message}
            if action.kind != "tool_call" or action.tool not in allowed:
                observations.append({"error": "delegate may only call an allowed read-only tool"})
                continue
            observation = self._invoke_tool(
                child, turn["id"], action.tool, action.arguments, allowed_tools=allowed
            )
            observations.append(observation)
            self.state.add_turn(child, "tool", observation, agent_name="zen-tool-broker")
        self.state.update_session_status(child, "FAILED", reason="delegate turn budget exhausted")
        return {"session_id": child, "objective": item["objective"], "error": "turn budget exhausted"}

    def _verify(
        self,
        session_id: str,
        objective: str,
        context: str,
        plan: dict[str, Any],
        claim: str,
        cycle: int,
    ) -> dict[str, Any]:
        manifest = self._agent("verifier")
        context = context + "\n\n# Verifier manifest instructions\n" + manifest.instructions
        self._agent_event(session_id, "AgentStart", manifest, {"cycle": cycle})
        status = self._automatic_observation(session_id, "git.status", {})
        diff = self._automatic_observation(session_id, "git.diff", {})
        calls = self.state.list_tool_calls(session_id)
        evidence = [
            {
                "tool": call["tool_name"],
                "arguments": call["arguments"],
                "status": call["status"],
                "result": call["result"],
                "error": call["error"],
            }
            for call in calls[-40:]
        ]
        prompt = self._bounded(
            context
            + "\n\n# Independent verification\n"
            + "Judge the objective from evidence, not the executor's claim. "
            + "FAIL when required tests or direct evidence are missing. Do not edit anything.\n"
            + json.dumps(
                {
                    "objective": objective,
                    "plan": plan,
                    "executor_claim": claim,
                    "git_status": status,
                    "git_diff": diff,
                    "tool_evidence": evidence,
                },
                ensure_ascii=False,
            )
        )
        verdict = self._generate(
            session_id, manifest.name, "independent verification", prompt, VERDICT_SCHEMA
        )
        self._validate_verdict(verdict)
        self._notify(
            "verification.completed",
            {
                "session_id": session_id,
                "cycle": cycle,
                "verdict": verdict["verdict"],
                "summary": verdict["summary"],
            },
        )
        self._agent_event(
            session_id, "AgentStop", manifest, {"outcome": verdict["verdict"]}
        )
        return verdict

    def _invoke_tool(
        self,
        session_id: str,
        turn_id: str | None,
        name: str,
        arguments: dict[str, Any],
        *,
        allowed_tools: set[str],
    ) -> dict[str, Any]:
        if len(self.state.list_tool_calls(session_id)) >= self.limits.max_tool_calls:
            return {"tool": name, "status": "DENIED", "error": "tool-call budget exhausted"}
        try:
            spec = self.tools.get(name)
        except Exception as exc:
            return {"tool": name, "status": "DENIED", "error": str(exc)}
        call_id = self.state.start_tool_call(
            session_id, name, arguments, turn_id=turn_id
        )
        self._notify(
            "tool.started",
            {"session_id": session_id, "tool": name, "call_id": call_id},
        )
        if name not in allowed_tools:
            self.state.finish_tool_call(
                call_id, "DENIED", error="agent manifest does not allow this tool"
            )
            self._notify(
                "tool.finished",
                {"session_id": session_id, "tool": name, "status": "DENIED"},
            )
            return {"tool": name, "status": "DENIED", "error": "tool not allowed"}
        pre = self._emit(
            session_id, "PreToolUse", {"tool": name, "arguments": arguments}, subject=name
        )
        if not pre.allowed:
            error = "; ".join(pre.feedback) or "PreToolUse hook blocked tool"
            self.state.finish_tool_call(call_id, "DENIED", error=error)
            self._notify(
                "tool.finished",
                {"session_id": session_id, "tool": name, "status": "DENIED"},
            )
            return {"tool": name, "status": "DENIED", "error": error}
        try:
            result = self.tools.invoke(
                name, ToolContext(session_id, turn_id or "", self.workspace), arguments
            )
            self.state.finish_tool_call(call_id, "SUCCEEDED", result=result)
            self._notify(
                "tool.finished",
                {"session_id": session_id, "tool": name, "status": "SUCCEEDED"},
            )
            post = self._emit(
                session_id,
                "PostToolUse",
                {"tool": name, "arguments": arguments, "result": result},
                subject=name,
            )
            return {
                "tool": name,
                "status": "SUCCEEDED",
                "result": result,
                "hook_feedback": list(post.feedback),
            }
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            self.state.finish_tool_call(call_id, "FAILED", error=error)
            self._notify(
                "tool.finished",
                {
                    "session_id": session_id,
                    "tool": name,
                    "status": "FAILED",
                    "error": error,
                },
            )
            post = self._emit(
                session_id,
                "PostToolFailure",
                {"tool": name, "arguments": arguments, "error": error},
                subject=name,
            )
            return {
                "tool": name,
                "status": "FAILED",
                "error": error,
                "hook_feedback": list(post.feedback),
            }

    def _automatic_observation(
        self, session_id: str, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        return self._invoke_tool(
            session_id, None, name, arguments, allowed_tools={name}
        )

    def _context(self, objective: str) -> str:
        extras: list[tuple[str, str]] = []
        selected = set(self.skills.select(objective))
        selected.add("execute-coding-task")
        for name in sorted(selected):
            try:
                extras.append((f"skill/{name}", self.skills.load_body(name)))
            except KeyError:
                continue
        if self.memory is not None:
            curated = self.memory.read_curated()
            if curated:
                extras.append(("approved project memory", curated))
            records = self.memory.query(objective, scope="project", limit=5)
            if records:
                extras.append(
                    ("retrieved project memory", "\n\n".join(item.content for item in records))
                )
        if (self.workspace / "ZEN.md").is_file():
            return WorkspaceContextCompiler(
                self.workspace, max_chars=min(60_000, self.limits.max_prompt_chars)
            ).compile(objective, extra_sections=tuple(extras))
        default = (
            "# Zen default workspace contract\n"
            "Stay inside the workspace. Preserve unrelated changes. Use Zen tools for all "
            "inspection and edits. Verify observable requirements before completion.\n\n"
            f"# Objective\n{objective}"
        )
        return self._bounded(
            default
            + "".join(f"\n\n# Applicable context: {title}\n{body}" for title, body in extras)
        )

    def _executor_prompt(
        self,
        objective: str,
        context: str,
        plan: dict[str, Any],
        allowed_tools: set[str],
        observations: list[dict[str, Any]],
        feedback: list[str],
        cycle: int,
        turn_number: int,
    ) -> str:
        catalog = self.tool_catalog(set(allowed_tools))
        return self._bounded(
            context
            + "\n\n# Execution state\n"
            + json.dumps(
                {
                    "objective": objective,
                    "plan": plan,
                    "verification_cycle": cycle,
                    "turn": turn_number,
                    "verifier_or_human_feedback": feedback,
                    "available_tools": catalog,
                    "recent_observations": observations[-12:],
                },
                ensure_ascii=False,
            )
            + "\nChoose one tool_call, one bounded delegate batch for independent read-only "
            + "investigation, ask_human only for a genuine blocker, or final only after "
            + "proportionate verification. The arguments field must always be a JSON-encoded "
            + "object string. For delegate, encode "
            + '{"tasks":[{"objective":"...","agent":"investigator"}]} in that string.'
        )

    def _agent(self, role: str) -> AgentManifest:
        if role in self.agents:
            manifest = self.agents.get(role)
        else:
            defaults = {
                "planner": ((), "read-only", 1),
                "executor": (tuple(self.tools.names()), "workspace-write", 40),
                "investigator": (
                    ("fs.list", "fs.read", "fs.search", "git.status", "git.diff"),
                    "read-only",
                    12,
                ),
                "verifier": ((), "read-only", 1),
            }
            tools, sandbox, turns = defaults[role]
            manifest = AgentManifest(
                name=role,
                description=f"Built-in {role}",
                role=role,
                tools=tools,
                skills=(),
                model=PINNED_MODEL,
                max_turns=turns,
                sandbox=sandbox,
                memory_scope="none",
                path=self.harness_root / "agents" / f"{role}.md",
                instructions="",
            )
        if manifest.model != PINNED_MODEL:
            raise ValueError(f"agent {manifest.name} must use {PINNED_MODEL}")
        return manifest

    def _allowed_tools(self, manifest: AgentManifest) -> set[str]:
        allowed = set(manifest.tools or self.tools.names())
        unknown = allowed - set(self.tools.names())
        if unknown:
            raise ValueError(f"agent {manifest.name} names unknown tools: {sorted(unknown)}")
        if manifest.sandbox == "read-only":
            allowed = {
                name for name in allowed if self.tools.get(name).risk == ToolRisk.READ_ONLY
            }
        return allowed

    def _pending_feedback(self, session_id: str) -> list[str]:
        items = self.state.list_feedback(session_id, pending_only=True)
        for item in items:
            self.state.mark_feedback_handled(item["id"])
        return [item["message"] for item in items]

    def _cancelled(self, session_id: str) -> bool:
        if not self.state.get_session(session_id)["cancel_requested"]:
            return False
        self.state.update_session_status(
            session_id, "CANCELLED", reason="cancel requested"
        )
        return True

    def _existing_plan(self, session_id: str) -> dict[str, Any] | None:
        for turn in self.state.list_turns(session_id):
            if turn["agent_name"] == "planner" and isinstance(turn["content"], dict):
                self._validate_plan(turn["content"])
                return turn["content"]
        return None

    def _validate_plan(self, value: dict[str, Any]) -> None:
        if set(value) != {"summary", "steps", "risks"}:
            raise ValueError("planner returned invalid keys")
        if not isinstance(value["summary"], str) or not isinstance(value["risks"], list):
            raise ValueError("planner returned invalid summary or risks")
        steps = value["steps"]
        if not isinstance(steps, list) or not steps:
            raise ValueError("planner must return at least one step")
        identifiers = []
        for step in steps:
            if not isinstance(step, dict) or set(step) != {"id", "description", "verification"}:
                raise ValueError("planner returned an invalid step")
            if not all(isinstance(step[key], str) and step[key].strip() for key in step):
                raise ValueError("plan step values must be non-empty strings")
            identifiers.append(step["id"])
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("plan step identifiers must be unique")

    @staticmethod
    def _validate_verdict(value: dict[str, Any]) -> None:
        if set(value) != {"verdict", "summary", "findings", "recommended_actions"}:
            raise ValueError("verifier returned invalid keys")
        if value["verdict"] not in {"PASS", "FAIL", "NEEDS_HUMAN"}:
            raise ValueError("verifier returned invalid verdict")
        if not isinstance(value["summary"], str):
            raise ValueError("verifier summary must be a string")
        if not all(isinstance(value[key], list) for key in ("findings", "recommended_actions")):
            raise ValueError("verifier findings and actions must be lists")

    def _agent_event(
        self,
        session_id: str,
        event: str,
        manifest: AgentManifest,
        payload: dict[str, Any],
    ) -> HookResult:
        return self._emit(
            session_id,
            event,
            {"agent": manifest.name, "role": manifest.role, **payload},
            subject=manifest.name,
        )

    def _emit(
        self,
        session_id: str,
        event: str,
        payload: dict[str, Any],
        *,
        subject: str = "",
    ) -> HookResult:
        result = self.hooks.emit(event, payload, subject=subject)
        self.state.append_event(
            session_id,
            f"hook.{event}",
            {
                "subject": subject,
                "allowed": result.allowed,
                "feedback": list(result.feedback),
                "executions": [asdict(item) for item in result.executions],
            },
        )
        return result

    def _remember(
        self, session_id: str, objective: str, verdict: dict[str, Any]
    ) -> None:
        if self.memory is None:
            return
        self.memory.append_episode(
            session_id,
            f"Objective: {objective}\nVerdict: {verdict['summary']}",
            actor="coordinator",
            metadata={"verdict": verdict["verdict"]},
        )

    def _generate(
        self,
        session_id: str,
        agent: str,
        phase: str,
        prompt: str,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        payload = {"agent": agent, "phase": phase}
        self.state.append_event(session_id, "model.requested", payload)
        self._notify("model.requested", {"session_id": session_id, **payload})
        response = self.model.generate(role=agent, prompt=prompt, schema=schema)
        self.state.append_event(
            session_id,
            "model.responded",
            {"agent": agent, "phase": phase},
        )
        self._notify(
            "model.responded",
            {"session_id": session_id, "agent": agent, "phase": phase},
        )
        return response

    def _notify(self, event: str, payload: dict[str, Any]) -> None:
        if self.progress is not None:
            self.progress(event, payload)

    def _bounded(self, value: str) -> str:
        if len(value) <= self.limits.max_prompt_chars:
            return value
        return value[: self.limits.max_prompt_chars] + "\n...[context truncated by Zen]"
