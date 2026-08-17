from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RunStatus(str, Enum):
    PLANNED = "PLANNED"
    RUNNING = "RUNNING"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    QUARANTINED = "QUARANTINED"


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    QUARANTINED = "QUARANTINED"
    SKIPPED = "SKIPPED"


class ToolRisk(str, Enum):
    READ_ONLY = "read_only"
    WORKSPACE_WRITE = "workspace_write"
    EXTERNAL_WRITE = "external_write"
    PRODUCTION_WRITE = "production_write"
    DESTRUCTIVE = "destructive"


@dataclass(frozen=True, slots=True)
class TaskSpec:
    key: str
    name: str
    tool: str
    inputs: dict[str, Any]
    depends_on: tuple[str, ...] = ()
    max_attempts: int = 2
    required: bool = True

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["depends_on"] = list(self.depends_on)
        return value


@dataclass(frozen=True, slots=True)
class Plan:
    workflow: str
    objective: str
    tasks: tuple[TaskSpec, ...]
    explanation: str
    inputs: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow": self.workflow,
            "objective": self.objective,
            "explanation": self.explanation,
            "inputs": self.inputs,
            "tasks": [task.to_dict() for task in self.tasks],
        }


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    sha256: str
    relative_path: str
    bytes: int
    media_type: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
