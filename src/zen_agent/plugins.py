from __future__ import annotations

from dataclasses import dataclass
import importlib.util
import json
from pathlib import Path
from typing import Any, Callable

from .models import Plan, TaskSpec
from .tools import ToolRegistry


Planner = Callable[[str, dict[str, Any], int], Plan]


@dataclass(frozen=True, slots=True)
class WorkflowSpec:
    name: str
    description: str
    triggers: tuple[str, ...]
    planner: Planner


class PluginRegistry:
    def __init__(self) -> None:
        self.tools = ToolRegistry()
        self.workflows: dict[str, WorkflowSpec] = {}
        self.manifests: dict[str, dict[str, Any]] = {}

    def register_workflow(self, workflow: WorkflowSpec) -> None:
        if workflow.name in self.workflows:
            raise ValueError(f"duplicate workflow: {workflow.name}")
        self.workflows[workflow.name] = workflow

    def choose_workflow(self, objective: str, explicit: str | None = None) -> WorkflowSpec:
        if explicit:
            try:
                return self.workflows[explicit]
            except KeyError as exc:
                raise ValueError(f"unknown workflow: {explicit}") from exc
        lowered = objective.casefold()
        scored = [
            (sum(1 for trigger in item.triggers if trigger.casefold() in lowered), item.name, item)
            for item in self.workflows.values()
        ]
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        if not scored or scored[0][0] == 0:
            available = ", ".join(sorted(self.workflows))
            raise ValueError(f"objective matches no workflow; choose one of: {available}")
        return scored[0][2]


def _validate_manifest(manifest: dict[str, Any], path: Path) -> None:
    required = {"id", "version", "entrypoint"}
    missing = required - set(manifest)
    if missing:
        raise ValueError(f"{path}: missing manifest fields {sorted(missing)}")
    if set(manifest) - {"id", "version", "entrypoint", "description"}:
        raise ValueError(f"{path}: manifest has unknown fields")
    if not isinstance(manifest["id"], str) or not manifest["id"]:
        raise ValueError(f"{path}: invalid plugin id")


def load_plugins(paths: tuple[Path, ...]) -> PluginRegistry:
    registry = PluginRegistry()
    for root in paths:
        for manifest_path in sorted(root.glob("*/plugin.json")):
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            _validate_manifest(manifest, manifest_path)
            plugin_id = manifest["id"]
            if plugin_id in registry.manifests:
                raise ValueError(f"duplicate plugin id: {plugin_id}")
            module_path = (manifest_path.parent / manifest["entrypoint"]).resolve()
            if manifest_path.parent.resolve() not in module_path.parents:
                raise ValueError(f"plugin entrypoint escapes its directory: {plugin_id}")
            if not module_path.is_file():
                raise FileNotFoundError(module_path)
            spec = importlib.util.spec_from_file_location(
                f"zen_plugin_{plugin_id.replace('-', '_')}", module_path
            )
            if spec is None or spec.loader is None:
                raise ImportError(f"cannot load plugin: {plugin_id}")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            register = getattr(module, "register", None)
            if not callable(register):
                raise TypeError(f"plugin {plugin_id} has no callable register")
            register(registry)
            registry.manifests[plugin_id] = manifest
    return registry
