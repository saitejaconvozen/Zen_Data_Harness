from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any

from ..config import PINNED_MODEL
from ..schema import validate


class CodexExecWorker:
    """Invoke Codex CLI without embedding a model-provider SDK."""

    def __init__(self, workspace: Path, model: str = PINNED_MODEL):
        if model != PINNED_MODEL:
            raise ValueError(f"Codex worker model must be {PINNED_MODEL}")
        self.workspace = workspace.resolve()
        self.model = model

    def command(self, schema_path: Path, output_path: Path, prompt: str) -> list[str]:
        return [
            "codex",
            "exec",
            "--model",
            self.model,
            "--sandbox",
            "workspace-write",
            "--json",
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(output_path),
            prompt,
        ]

    def execute(self, prompt: str, output_schema: dict[str, Any]) -> dict[str, Any]:
        if shutil.which("codex") is None:
            raise RuntimeError("codex CLI is not installed or not on PATH")
        with tempfile.TemporaryDirectory(prefix="zen-codex-") as directory:
            temporary = Path(directory)
            schema_path = temporary / "schema.json"
            output_path = temporary / "output.json"
            schema_path.write_text(json.dumps(output_schema), encoding="utf-8")
            completed = subprocess.run(
                self.command(schema_path, output_path, prompt),
                cwd=self.workspace,
                text=True,
                capture_output=True,
                check=False,
            )
            if completed.returncode != 0:
                raise RuntimeError(
                    f"codex exec failed with code {completed.returncode}: {completed.stderr[-2000:]}"
                )
            result = json.loads(output_path.read_text(encoding="utf-8"))
            validate(result, output_schema)
            return result
