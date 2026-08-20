"""Structured transport over an OpenAI-compatible endpoint.

Implements the same `ModelAdapter` protocol as `CodexExecAdapter`, so the agent
runtime does not know or care which provider answered. That matters more than it
sounds: the agent loop was pinned to Codex, and when that workspace ran out of
credits the entire agentic layer became unrunnable while a capable model sat
behind a proxy on the same machine.

The one real difference from the Codex adapter is schema enforcement. `codex
exec --output-schema` constrains decoding, so a malformed response is close to
impossible. An OpenAI-compatible endpoint may or may not pass constrained
decoding through to the upstream model, so the schema is requested *and*
verified here, with the validation error fed back on retry. Verifying either way
means a provider that quietly ignores `response_format` cannot corrupt a run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from typing import Any
import urllib.error
import urllib.request


DEFAULT_BASE_URL = "http://127.0.0.1:4000"


def _first_json_object(text: str) -> str:
    """Extract the first balanced {...} block, ignoring any surrounding prose."""
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object in response")
    depth, in_string, escape = 0, False, False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise ValueError("unbalanced JSON object in response")


def validate_against_schema(value: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """Check the JSON Schema subset the agent contracts actually use.

    Deliberately not a full validator: the roles use object/array/enum/type and
    required, and a small checker that is obviously correct beats a dependency
    whose failure modes nobody here understands.
    """
    errors: list[str] = []
    expected = schema.get("type")
    if expected == "object":
        if not isinstance(value, dict):
            return [f"{path}: expected object, got {type(value).__name__}"]
        for name in schema.get("required", []):
            if name not in value:
                errors.append(f"{path}.{name}: required property missing")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            for name in value:
                if name not in properties:
                    errors.append(f"{path}.{name}: unexpected property")
        for name, sub in properties.items():
            if name in value:
                errors.extend(validate_against_schema(value[name], sub, f"{path}.{name}"))
    elif expected == "array":
        if not isinstance(value, list):
            return [f"{path}: expected array, got {type(value).__name__}"]
        items = schema.get("items")
        if isinstance(items, dict):
            for index, item in enumerate(value[:40]):
                errors.extend(validate_against_schema(item, items, f"{path}[{index}]"))
    elif expected == "string" and not isinstance(value, str):
        errors.append(f"{path}: expected string")
    elif expected == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
        errors.append(f"{path}: expected integer")
    elif expected == "number" and (
        not isinstance(value, (int, float)) or isinstance(value, bool)
    ):
        errors.append(f"{path}: expected number")
    elif expected == "boolean" and not isinstance(value, bool):
        errors.append(f"{path}: expected boolean")

    choices = schema.get("enum")
    if choices is not None and value not in choices:
        errors.append(f"{path}: {value!r} is not one of {choices}")
    return errors[:12]


@dataclass(slots=True)
class LiteLLMAdapter:
    """Model transport for the agent runtime, over a LiteLLM proxy."""

    model: str = ""
    base_url: str = ""
    api_key: str = ""
    timeout_seconds: int = 600
    max_attempts: int = 3
    reasoning_effort: str = ""
    calls: int = field(default=0, repr=False)

    def __post_init__(self) -> None:
        self.model = self.model or os.environ.get("ZEN_LITELLM_MODEL", "gemini-3.7-flash")
        self.base_url = (
            self.base_url or os.environ.get("ZEN_LITELLM_BASE_URL", DEFAULT_BASE_URL)
        ).rstrip("/")
        self.api_key = self.api_key or os.environ.get("ZEN_LITELLM_API_KEY", "")
        if not 1 <= self.max_attempts <= 5:
            raise ValueError("max_attempts must be between 1 and 5")

    def generate(self, *, role: str, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        system = (
            f"You are the {role} inside the Zen agent runtime. "
            "Return exactly one JSON object and nothing else: no prose, no "
            "explanation, no markdown fence. It must satisfy this JSON Schema:\n"
            + json.dumps(schema)
        )
        url = f"{self.base_url}/v1/chat/completions"
        last_error = ""

        for attempt in range(1, self.max_attempts + 1):
            user = prompt if attempt == 1 else (
                f"{prompt}\n\nYour previous response was rejected:\n{last_error}\n"
                "Return only the corrected JSON object."
            )
            body: dict[str, Any] = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0,
            }
            if self.reasoning_effort:
                body["reasoning_effort"] = self.reasoning_effort

            request = urllib.request.Request(
                url, data=json.dumps(body).encode("utf-8"), method="POST",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.api_key}",
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                last_error = f"HTTP {exc.code}: {exc.read()[:600].decode('utf-8', 'replace')}"
                continue
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                raise RuntimeError(f"model proxy unreachable at {url}: {exc}") from exc

            self.calls += 1
            try:
                content = payload["choices"][0]["message"]["content"]
                value = json.loads(_first_json_object(content))
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                last_error = f"response was not usable JSON: {exc}"
                continue
            if not isinstance(value, dict):
                last_error = "response must be a JSON object"
                continue
            errors = validate_against_schema(value, schema)
            if errors:
                last_error = "schema violations: " + "; ".join(errors)
                continue
            return value

        raise RuntimeError(
            f"model transport failed after {self.max_attempts} attempts: {last_error}"
        )
