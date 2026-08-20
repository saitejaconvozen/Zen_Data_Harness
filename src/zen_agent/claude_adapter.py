"""Use the Claude Code CLI as a structured model transport.

Same contract as `CodexExecAdapter`: a prompt and a schema go in, one validated
JSON object comes out, and the model touches nothing. It is a drop-in for the
`ModelAdapter` protocol so the factory does not know or care which provider
answered.

One real difference shapes this file. `codex exec` takes `--output-schema` and
the provider constrains decoding, so a malformed response is close to
impossible. Claude Code has no equivalent, so the schema has to be enforced
here: state it in the prompt, parse what comes back, validate it, and retry with
the validation error appended. That makes the transport slightly less reliable
and slightly more expensive per call, which is worth knowing before running a
large batch.

**Provenance matters more than convenience here.** A corpus refined half by one
model and half by another has a seam in it that nobody can find afterwards. So
every response carries `_transport` naming the model that produced it, and the
pin in `config.PINNED_MODEL` is deliberately not weakened — a caller has to ask
for this adapter explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import shutil
import subprocess
from tempfile import TemporaryDirectory
from typing import Any


# Claude Code writes session state under the working directory; an empty
# throwaway keeps it away from the harness and its credentials.
_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.S)

DEFAULT_MODEL = "claude-sonnet-5"


def _strip_fence(text: str) -> str:
    match = _FENCE.match(text)
    return match.group(1) if match else text.strip()


def _first_json_object(text: str) -> str:
    """Extract the first balanced {...} block.

    Models occasionally add a sentence before or after the object despite being
    told not to. Rejecting the whole call for that wastes a request; finding the
    object is cheap and deterministic.
    """
    if not isinstance(text, str):
        # Providers return a null content field for a truncated or
        # reasoning-only turn. Passing that straight to the parser raised
        # AttributeError out of the adapter and killed the whole agent run,
        # rather than being retried like any other malformed response.
        raise ValueError(f"response carried no text content (got {type(text).__name__})")
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object in response")
    depth = 0
    in_string = False
    escape = False
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
    """Check the subset of JSON Schema the harness actually uses.

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
            for index, item in enumerate(value):
                errors.extend(validate_against_schema(item, items, f"{path}[{index}]"))
    elif expected == "string":
        if not isinstance(value, str):
            errors.append(f"{path}: expected string")
    elif expected == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            errors.append(f"{path}: expected integer")
    elif expected == "number":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            errors.append(f"{path}: expected number")
    elif expected == "boolean":
        if not isinstance(value, bool):
            errors.append(f"{path}: expected boolean")

    choices = schema.get("enum")
    if choices is not None and value not in choices:
        errors.append(f"{path}: {value!r} is not one of {choices}")
    return errors


@dataclass(slots=True)
class ClaudeCliAdapter:
    """Structured transport backed by the Claude Code CLI in print mode."""

    model: str = DEFAULT_MODEL
    executable: str = "claude"
    timeout_seconds: int = 600
    max_attempts: int = 3
    calls: int = field(default=0, repr=False)

    def __post_init__(self) -> None:
        if self.timeout_seconds < 1:
            raise ValueError("timeout_seconds must be positive")
        if not 1 <= self.max_attempts <= 5:
            raise ValueError("max_attempts must be between 1 and 5")

    def _instruction(self, role: str, prompt: str, schema: dict[str, Any]) -> str:
        return (
            f"You are the {role} inside the Zen agent runtime.\n"
            "Return exactly one JSON object and nothing else: no prose, no "
            "explanation, no markdown code fence.\n"
            "Do not use any tools. Do not read or write files. Do not run "
            "commands. Zen itself owns all tool execution.\n\n"
            "The object must satisfy this JSON Schema:\n"
            f"{json.dumps(schema, indent=2)}\n\n"
            f"{prompt}"
        )

    def generate(self, *, role: str, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        executable = shutil.which(self.executable)
        if executable is None:
            raise RuntimeError(f"model transport not found: {self.executable}")

        instruction = self._instruction(role, prompt, schema)
        last_error = ""
        for attempt in range(1, self.max_attempts + 1):
            text = self._invoke(executable, instruction if attempt == 1 else (
                instruction
                + "\n\nYour previous response was rejected:\n"
                + last_error
                + "\nReturn only the corrected JSON object."
            ))
            try:
                value = json.loads(_first_json_object(_strip_fence(text)))
            except ValueError as exc:
                last_error = f"response was not valid JSON: {exc}"
                continue
            if not isinstance(value, dict):
                last_error = "response must be a JSON object"
                continue
            errors = validate_against_schema(value, schema)
            if errors:
                last_error = "schema violations: " + "; ".join(errors[:8])
                continue
            # Record which model produced this, so a mixed-provider corpus can
            # always be separated after the fact.
            value.setdefault("_transport", {"provider": "claude-cli", "model": self.model})
            return value
        raise RuntimeError(
            f"claude transport failed after {self.max_attempts} attempts: {last_error}"
        )

    def _invoke(self, executable: str, instruction: str) -> str:
        with TemporaryDirectory(prefix="zen-claude-") as directory:
            command = [
                executable,
                "-p",
                "--model", self.model,
                # No tools: the transport must be a pure function, exactly as the
                # codex adapter is. An empty allowlist is the enforcement.
                "--allowed-tools", "",
                "--permission-mode", "default",
            ]
            completed = subprocess.run(
                command,
                input=instruction,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.timeout_seconds,
                cwd=Path(directory),
                check=False,
            )
            self.calls += 1
            if completed.returncode != 0:
                tail = (completed.stderr or completed.stdout)[-4_000:].strip()
                raise RuntimeError(
                    f"claude transport failed ({completed.returncode}): {tail}"
                )
            return completed.stdout
