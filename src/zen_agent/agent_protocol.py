from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any


# One turn, one or more tool calls.
#
# The schema used to carry a single `tool` name, so an agent gathering evidence
# from four sources spent four model round trips on I/O. With a 20-turn budget
# that is most of the budget spent waiting. Independent calls now go out
# together and their results come back in one observation.
#
# `tool` and `arguments` are retained so a model that emits the old single-call
# shape still works; `from_dict` normalises both into `calls`.
ACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["kind", "reasoning_summary", "calls", "message"],
    "properties": {
        "kind": {"type": "string", "enum": ["tool_call", "delegate", "final", "ask_human"]},
        "reasoning_summary": {"type": "string"},
        "calls": {
            "type": "array",
            "maxItems": 6,
            "description": (
                "Tool calls to run together. Only include calls that do not depend "
                "on each other's results; anything that needs a previous result "
                "belongs in the next turn."
            ),
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["tool", "arguments"],
                "properties": {
                    "tool": {"type": "string"},
                    "arguments": {
                        "type": "string",
                        "description": "A JSON-encoded object of tool arguments.",
                    },
                },
            },
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
class ToolCall:
    """One tool invocation inside a turn."""

    tool: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class AgentAction:
    kind: str
    reasoning_summary: str
    calls: tuple[ToolCall, ...]
    message: str

    @property
    def tool(self) -> str | None:
        """The first tool named, for callers that still think in single calls."""
        return self.calls[0].tool if self.calls else None

    @property
    def arguments(self) -> dict[str, Any]:
        return dict(self.calls[0].arguments) if self.calls else {}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AgentAction":
        kind = value.get("kind")
        if kind not in {"tool_call", "delegate", "final", "ask_human"}:
            raise ValueError(f"invalid action kind: {kind}")

        def decode(raw: Any) -> dict[str, Any]:
            if isinstance(raw, str):
                if not raw.strip():
                    return {}
                try:
                    raw = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise ValueError("action arguments must contain valid JSON") from exc
            if not isinstance(raw, dict):
                raise ValueError("action arguments must encode an object")
            return raw

        calls: list[ToolCall] = []
        for item in value.get("calls") or []:
            if not isinstance(item, dict) or not isinstance(item.get("tool"), str):
                raise ValueError("each call requires a tool name")
            calls.append(ToolCall(item["tool"], decode(item.get("arguments"))))
        # Accept the single-call shape a model may still emit.
        if not calls and isinstance(value.get("tool"), str) and value["tool"]:
            calls.append(ToolCall(value["tool"], decode(value.get("arguments"))))

        if kind == "tool_call" and not calls:
            raise ValueError("tool_call requires at least one call")
        if kind != "tool_call" and calls:
            raise ValueError(f"{kind} must not name a tool")
        for key in ("reasoning_summary", "message"):
            if not isinstance(value.get(key), str):
                raise ValueError(f"{key} must be a string")
        return cls(kind, value["reasoning_summary"], tuple(calls), value["message"])
