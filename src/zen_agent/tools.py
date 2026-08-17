from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .models import ToolRisk
from .schema import validate


class ToolError(RuntimeError):
    pass


class ToolDenied(ToolError):
    pass


@dataclass(frozen=True, slots=True)
class ToolContext:
    run_id: str
    task_id: str
    workspace: Path


ToolHandler = Callable[[ToolContext, dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    version: str
    description: str
    risk: ToolRisk
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    handler: ToolHandler


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"duplicate tool: {spec.name}")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolError(f"unknown tool: {name}") from exc

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))

    def invoke(self, name: str, context: ToolContext, inputs: dict[str, Any]) -> dict[str, Any]:
        spec = self.get(name)
        validate(inputs, spec.input_schema)
        output = spec.handler(context, inputs)
        validate(output, spec.output_schema)
        return output
