from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any


ACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["kind", "reasoning_summary", "tool", "arguments", "message"],
    "properties": {
        "kind": {"type": "string", "enum": ["tool_call", "delegate", "final", "ask_human"]},
        "reasoning_summary": {"type": "string"},
        "tool": {"type": ["string", "null"]},
        "arguments": {
            "type": "string",
            "description": "A JSON-encoded object containing tool or delegation arguments.",
        },
        "message": {"type": "string"},
    },
}


PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "steps", "risks"],
    "properties": {
        "summary": {"type": "string"},
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "description", "verification"],
                "properties": {
                    "id": {"type": "string"},
                    "description": {"type": "string"},
                    "verification": {"type": "string"},
                },
            },
        },
        "risks": {"type": "array", "items": {"type": "string"}},
    },
}


VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "summary", "findings", "recommended_actions"],
    "properties": {
        "verdict": {"type": "string", "enum": ["PASS", "FAIL", "NEEDS_HUMAN"]},
        "summary": {"type": "string"},
        "findings": {"type": "array", "items": {"type": "string"}},
        "recommended_actions": {"type": "array", "items": {"type": "string"}},
    },
}


@dataclass(frozen=True, slots=True)
class AgentAction:
    kind: str
    reasoning_summary: str
    tool: str | None
    arguments: dict[str, Any]
    message: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AgentAction":
        missing = set(ACTION_SCHEMA["required"]) - set(value)
        extra = set(value) - set(ACTION_SCHEMA["properties"])
        if missing or extra:
            raise ValueError(f"invalid action keys; missing={sorted(missing)}, extra={sorted(extra)}")
        kind = value["kind"]
        if kind not in {"tool_call", "delegate", "final", "ask_human"}:
            raise ValueError(f"invalid action kind: {kind}")
        tool = value["tool"]
        arguments = value["arguments"]
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError as exc:
                raise ValueError("action arguments must contain valid JSON") from exc
        if not isinstance(arguments, dict):
            raise ValueError("action arguments must encode an object")
        if kind == "tool_call" and (not isinstance(tool, str) or not tool):
            raise ValueError("tool_call requires a tool name")
        if kind != "tool_call" and tool is not None:
            raise ValueError(f"{kind} must not name a tool")
        for key in ("reasoning_summary", "message"):
            if not isinstance(value[key], str):
                raise ValueError(f"{key} must be a string")
        return cls(kind, value["reasoning_summary"], tool, arguments, value["message"])
