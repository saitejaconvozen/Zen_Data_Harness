from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
from typing import Any, Protocol

from .config import PINNED_MODEL


class ModelAdapter(Protocol):
    def generate(self, *, role: str, prompt: str, schema: dict[str, Any]) -> dict[str, Any]: ...


@dataclass(slots=True)
class CodexExecAdapter:
    """Use Codex only as a structured model transport.

    The process runs in a fresh empty, read-only directory. It cannot inspect or
    mutate the target workspace; all workspace interaction goes through Zen tools.
    """

    model: str = PINNED_MODEL
    executable: str = "codex"
    timeout_seconds: int = 300

    def __post_init__(self) -> None:
        if self.model != PINNED_MODEL:
            raise ValueError(f"Zen coding tasks are pinned to {PINNED_MODEL}")
        if self.timeout_seconds < 1:
            raise ValueError("timeout_seconds must be positive")

    def generate(self, *, role: str, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        executable = shutil.which(self.executable)
        if executable is None:
            raise RuntimeError(f"model transport not found: {self.executable}")
        with TemporaryDirectory(prefix="zen-model-") as directory:
            root = Path(directory)
            schema_path = root / "response.schema.json"
            output_path = root / "response.json"
            schema_path.write_text(json.dumps(schema), encoding="utf-8")
            instruction = (
                f"You are the {role} inside the Zen agent runtime. "
                "Return exactly one JSON object matching the supplied schema. "
                "Do not execute tools, inspect the local directory, or modify files. "
                "Zen itself owns all tool execution.\n\n" + prompt
            )
            command = [
                executable,
                "exec",
                "--ephemeral",
                "--ignore-user-config",
                "--ignore-rules",
                "--skip-git-repo-check",
                "--model",
                self.model,
                "--sandbox",
                "read-only",
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(output_path),
                "--cd",
                str(root),
                "-",
            ]
            completed = subprocess.run(
                command,
                input=instruction,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.timeout_seconds,
                check=False,
            )
            if completed.returncode != 0:
                tail = completed.stderr[-4_000:].strip()
                raise RuntimeError(f"model transport failed ({completed.returncode}): {tail}")
            try:
                value = json.loads(output_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError("model returned no valid structured response") from exc
            if not isinstance(value, dict):
                raise RuntimeError("model response must be a JSON object")
            return value


class ScriptedModelAdapter:
    """Deterministic adapter for tests and reproducible dry runs."""

    def __init__(self, responses: list[dict[str, Any]]):
        self.responses = list(responses)
        self.calls: list[dict[str, str]] = []

    def generate(self, *, role: str, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        self.calls.append({"role": role, "prompt": prompt})
        if not self.responses:
            raise RuntimeError("scripted model has no response remaining")
        return self.responses.pop(0)
