from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
import json
import os
from pathlib import Path
import subprocess
from typing import Any


LIFECYCLE_EVENTS = frozenset(
    {
        "SessionStart",
        "SessionEnd",
        "AgentStart",
        "AgentStop",
        "PreToolUse",
        "PostToolUse",
        "PostToolFailure",
        "BeforeComplete",
    }
)


@dataclass(frozen=True, slots=True)
class HookSpec:
    command: tuple[str, ...]
    timeout_seconds: float = 10.0
    matcher: str = "*"
    cwd: str = "."


@dataclass(frozen=True, slots=True)
class HookExecution:
    command: tuple[str, ...]
    returncode: int | None
    decision: str
    feedback: str
    stdout: str
    stderr: str


@dataclass(frozen=True, slots=True)
class HookResult:
    allowed: bool
    feedback: tuple[str, ...]
    executions: tuple[HookExecution, ...]


class HookConfig:
    def __init__(self, hooks: dict[str, tuple[HookSpec, ...]]):
        self.hooks = dict(hooks)

    @classmethod
    def empty(cls) -> "HookConfig":
        return cls({})

    @classmethod
    def load(cls, path: Path) -> "HookConfig":
        if not path.exists():
            return cls.empty()
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid hooks JSON: {path}: {exc}") from exc
        if not isinstance(document, dict) or set(document) != {"hooks"}:
            raise ValueError("hooks config must contain only a 'hooks' object")
        raw_hooks = document["hooks"]
        if not isinstance(raw_hooks, dict):
            raise ValueError("'hooks' must be an object")
        parsed: dict[str, tuple[HookSpec, ...]] = {}
        for event, raw_specs in raw_hooks.items():
            if event not in LIFECYCLE_EVENTS:
                raise ValueError(f"unknown lifecycle event: {event}")
            if not isinstance(raw_specs, list):
                raise ValueError(f"hooks for {event} must be a list")
            parsed[event] = tuple(_parse_spec(item, event) for item in raw_specs)
        return cls(parsed)


class HookRunner:
    """Run trusted lifecycle commands deterministically, without invoking a shell."""

    def __init__(
        self,
        config: HookConfig,
        workspace_root: Path,
        *,
        max_output_chars: int = 16_000,
    ):
        self.config = config
        self.workspace_root = workspace_root.resolve()
        self.max_output_chars = max_output_chars
        if max_output_chars < 1:
            raise ValueError("max_output_chars must be positive")

    def emit(
        self,
        event: str,
        payload: dict[str, Any],
        *,
        subject: str = "",
    ) -> HookResult:
        if event not in LIFECYCLE_EVENTS:
            raise ValueError(f"unknown lifecycle event: {event}")
        envelope = json.dumps(
            {"event": event, "subject": subject, "payload": payload},
            sort_keys=True,
            separators=(",", ":"),
        )
        executions: list[HookExecution] = []
        feedback: list[str] = []
        allowed = True
        for spec in self.config.hooks.get(event, ()):
            if not fnmatchcase(subject, spec.matcher):
                continue
            execution = self._run(spec, event, envelope)
            executions.append(execution)
            if execution.feedback:
                feedback.append(execution.feedback)
            if execution.decision == "block":
                allowed = False
        return HookResult(allowed, tuple(feedback), tuple(executions))

    def _run(self, spec: HookSpec, event: str, envelope: str) -> HookExecution:
        cwd = (self.workspace_root / spec.cwd).resolve()
        if not cwd.is_relative_to(self.workspace_root):
            return HookExecution(
                spec.command,
                None,
                "block",
                "hook working directory escapes the workspace",
                "",
                "",
            )
        if not cwd.is_dir():
            return HookExecution(spec.command, None, "block", "hook working directory does not exist", "", "")
        environment = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
            "ZEN_HOOK_EVENT": event,
        }
        try:
            completed = subprocess.run(
                spec.command,
                input=envelope,
                text=True,
                capture_output=True,
                cwd=cwd,
                env=environment,
                timeout=spec.timeout_seconds,
                shell=False,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return HookExecution(
                spec.command,
                None,
                "block",
                f"hook timed out after {spec.timeout_seconds:g} seconds",
                _bounded(exc.stdout or "", self.max_output_chars),
                _bounded(exc.stderr or "", self.max_output_chars),
            )
        except OSError as exc:
            return HookExecution(spec.command, None, "block", f"hook could not start: {exc}", "", "")

        stdout = _bounded(completed.stdout, self.max_output_chars)
        stderr = _bounded(completed.stderr, self.max_output_chars)
        if completed.returncode != 0:
            message = stderr.strip() or stdout.strip() or f"hook exited with status {completed.returncode}"
            return HookExecution(spec.command, completed.returncode, "block", message, stdout, stderr)
        if not stdout.strip():
            return HookExecution(spec.command, completed.returncode, "allow", "", stdout, stderr)
        try:
            response = json.loads(stdout)
        except json.JSONDecodeError:
            return HookExecution(
                spec.command,
                completed.returncode,
                "block",
                "hook stdout must be empty or one JSON object",
                stdout,
                stderr,
            )
        if not isinstance(response, dict) or set(response) - {"decision", "feedback"}:
            return HookExecution(
                spec.command,
                completed.returncode,
                "block",
                "hook response may contain only decision and feedback",
                stdout,
                stderr,
            )
        decision = response.get("decision", "allow")
        response_feedback = response.get("feedback", "")
        if decision not in {"allow", "block"} or not isinstance(response_feedback, str):
            return HookExecution(
                spec.command,
                completed.returncode,
                "block",
                "hook response has an invalid decision or feedback",
                stdout,
                stderr,
            )
        return HookExecution(
            spec.command,
            completed.returncode,
            decision,
            _bounded(response_feedback, self.max_output_chars),
            stdout,
            stderr,
        )


def _parse_spec(raw: object, event: str) -> HookSpec:
    if not isinstance(raw, dict):
        raise ValueError(f"each {event} hook must be an object")
    unknown = set(raw) - {"command", "timeout_seconds", "matcher", "cwd"}
    if unknown:
        raise ValueError(f"unknown {event} hook fields: {sorted(unknown)}")
    command = raw.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(part, str) and part for part in command):
        raise ValueError(f"{event} hook command must be a non-empty string list")
    timeout = raw.get("timeout_seconds", 10.0)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 60:
        raise ValueError(f"{event} hook timeout_seconds must be in (0, 60]")
    matcher = raw.get("matcher", "*")
    cwd = raw.get("cwd", ".")
    if not isinstance(matcher, str) or not matcher:
        raise ValueError(f"{event} hook matcher must be a non-empty string")
    if not isinstance(cwd, str) or not cwd:
        raise ValueError(f"{event} hook cwd must be a non-empty string")
    return HookSpec(tuple(command), float(timeout), matcher, cwd)


def _bounded(value: str | bytes, limit: int) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if len(value) <= limit:
        return value
    return value[:limit] + "\n...[hook output truncated]"
