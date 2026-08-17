from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import tomllib

from .models import ToolRisk


PINNED_MODEL = "gpt-5.6-sol"


@dataclass(frozen=True, slots=True)
class RuntimeLimits:
    max_tasks_per_run: int
    max_attempts_per_task: int
    max_tool_calls_per_run: int


@dataclass(frozen=True, slots=True)
class HarnessConfig:
    root: Path
    state_directory: Path
    instruction_file: Path
    plugin_paths: tuple[Path, ...]
    allowed_models: tuple[str, ...]
    default_model: str
    execution_adapter: str
    allowed_risks: frozenset[ToolRisk]
    approval_risks: frozenset[ToolRisk]
    default_tool_effect: str
    limits: RuntimeLimits
    raw: dict

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(self.raw, sort_keys=True, separators=(",", ":"))
        return sha256(payload.encode("utf-8")).hexdigest()

    def validate(self) -> None:
        if self.allowed_models != (PINNED_MODEL,) or self.default_model != PINNED_MODEL:
            raise ValueError(f"model policy must allow only {PINNED_MODEL}")
        if self.execution_adapter != "codex_exec":
            raise ValueError("execution_adapter must be codex_exec")
        if self.default_tool_effect not in {"allow", "deny"}:
            raise ValueError("default_tool_effect must be allow or deny")
        if min(
            self.limits.max_tasks_per_run,
            self.limits.max_attempts_per_task,
            self.limits.max_tool_calls_per_run,
        ) < 1:
            raise ValueError("runtime limits must be positive")
        if not self.instruction_file.is_file():
            raise FileNotFoundError(self.instruction_file)
        for plugin_path in self.plugin_paths:
            if not plugin_path.is_dir():
                raise FileNotFoundError(plugin_path)


def load_config(root: Path) -> HarnessConfig:
    root = root.resolve()
    config_path = root / "zen.toml"
    with config_path.open("rb") as handle:
        raw = tomllib.load(handle)
    runtime = raw["runtime"]
    model = raw["models"]
    policy = raw["policy"]
    config = HarnessConfig(
        root=root,
        state_directory=(root / runtime["state_directory"]).resolve(),
        instruction_file=(root / raw["project"]["instruction_file"]).resolve(),
        plugin_paths=tuple((root / item).resolve() for item in raw["plugins"]["paths"]),
        allowed_models=tuple(model["allowed"]),
        default_model=model["default"],
        execution_adapter=model["execution_adapter"],
        allowed_risks=frozenset(ToolRisk(item) for item in policy["allowed_risks"]),
        approval_risks=frozenset(ToolRisk(item) for item in policy["approval_risks"]),
        default_tool_effect=policy["default_tool_effect"],
        limits=RuntimeLimits(
            max_tasks_per_run=int(runtime["max_tasks_per_run"]),
            max_attempts_per_task=int(runtime["max_attempts_per_task"]),
            max_tool_calls_per_run=int(runtime["max_tool_calls_per_run"]),
        ),
        raw=raw,
    )
    config.validate()
    return config
