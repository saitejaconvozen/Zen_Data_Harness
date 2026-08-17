from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .models import ToolRisk
from .tools import ToolContext, ToolRegistry, ToolSpec
from .workspace import Workspace, WorkspaceError


MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_OUTPUT_BYTES = 256 * 1024
MAX_RESULTS = 500

OBJECT = {"type": "object"}


def _workspace(context: ToolContext) -> Workspace:
    return Workspace(context.workspace)


def _read(context: ToolContext, inputs: dict[str, Any]) -> dict[str, Any]:
    data, digest = _workspace(context).read_bytes(inputs["path"], max_bytes=inputs.get("max_bytes", MAX_FILE_BYTES))
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorkspaceError("file is not valid UTF-8") from exc
    return {"path": inputs["path"], "content": content, "sha256": digest, "bytes": len(data)}


def _write(context: ToolContext, inputs: dict[str, Any]) -> dict[str, Any]:
    data = inputs["content"].encode("utf-8")
    digest = _workspace(context).atomic_write(
        inputs["path"], data, expected_sha256=inputs.get("expected_sha256"), max_bytes=MAX_FILE_BYTES
    )
    return {"path": inputs["path"], "sha256": digest, "bytes": len(data)}


def _replace(context: ToolContext, inputs: dict[str, Any]) -> dict[str, Any]:
    workspace = _workspace(context)
    data, digest = workspace.read_bytes(inputs["path"], max_bytes=MAX_FILE_BYTES)
    if digest != inputs["expected_sha256"]:
        raise WorkspaceError("file changed since it was read")
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorkspaceError("file is not valid UTF-8") from exc
    occurrences = content.count(inputs["old"])
    requested = inputs.get("count", 1)
    if occurrences < requested:
        raise WorkspaceError(f"expected at least {requested} exact occurrence(s), found {occurrences}")
    if inputs.get("require_unique", True) and occurrences != requested:
        raise WorkspaceError(f"expected exactly {requested} occurrence(s), found {occurrences}")
    updated = content.replace(inputs["old"], inputs["new"], requested)
    new_data = updated.encode("utf-8")
    new_digest = workspace.atomic_write(
        inputs["path"], new_data, expected_sha256=digest, max_bytes=MAX_FILE_BYTES
    )
    return {"path": inputs["path"], "sha256": new_digest, "replacements": requested, "bytes": len(new_data)}


def _list(context: ToolContext, inputs: dict[str, Any]) -> dict[str, Any]:
    workspace = _workspace(context)
    base = workspace.directory(inputs.get("path", "."))
    recursive = inputs.get("recursive", False)
    limit = inputs.get("limit", MAX_RESULTS)
    iterator = base.rglob("*") if recursive else base.iterdir()
    entries: list[dict[str, Any]] = []
    truncated = False
    for path in sorted(iterator, key=lambda value: value.as_posix()):
        try:
            resolved = path.resolve(strict=True)
        except (FileNotFoundError, RuntimeError):
            continue
        if not resolved.is_relative_to(workspace.root):
            continue
        if len(entries) >= limit:
            truncated = True
            break
        kind = "directory" if path.is_dir() else "file" if path.is_file() else "other"
        entries.append({"path": workspace.relative(path), "kind": kind})
    return {"entries": entries, "truncated": truncated}


def _search_python(workspace: Workspace, base: Path, pattern: str, limit: int) -> tuple[list[dict[str, Any]], bool]:
    matches: list[dict[str, Any]] = []
    for path in sorted(base.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            relative = workspace.relative(path)
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
            data = path.read_bytes()
            content = data.decode("utf-8")
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        for number, line in enumerate(content.splitlines(), 1):
            if pattern in line:
                if len(matches) >= limit:
                    return matches, True
                matches.append({"path": relative, "line": number, "text": line[:2000]})
    return matches, False


def _search(context: ToolContext, inputs: dict[str, Any]) -> dict[str, Any]:
    workspace = _workspace(context)
    base = workspace.directory(inputs.get("path", "."))
    pattern = inputs["pattern"]
    limit = inputs.get("limit", MAX_RESULTS)
    rg = shutil.which("rg")
    if rg is None:
        matches, truncated = _search_python(workspace, base, pattern, limit)
        return {"matches": matches, "truncated": truncated, "engine": "python"}
    command = [rg, "--fixed-strings", "--line-number", "--no-heading", "--color", "never", "--", pattern, "."]
    completed = subprocess.run(command, cwd=base, capture_output=True, timeout=30, check=False)
    if completed.returncode not in (0, 1):
        raise WorkspaceError(completed.stderr.decode("utf-8", "replace")[:4000] or "rg search failed")
    lines = completed.stdout.decode("utf-8", "replace").splitlines()
    lines.sort(key=lambda raw: (raw.split(":", 2)[0], int(raw.split(":", 2)[1])))
    matches: list[dict[str, Any]] = []
    for raw in lines[:limit]:
        name, number, text = raw.split(":", 2)
        resolved = (base / name).resolve(strict=True)
        if not resolved.is_relative_to(workspace.root):
            continue
        matches.append({"path": workspace.relative(resolved), "line": int(number), "text": text[:2000]})
    return {"matches": matches, "truncated": len(lines) > limit, "engine": "rg"}


def _run(context: ToolContext, inputs: dict[str, Any]) -> dict[str, Any]:
    workspace = _workspace(context)
    cwd = workspace.directory(inputs.get("cwd", "."))
    argv = inputs["argv"]
    if not argv or any("\x00" in part for part in argv):
        raise WorkspaceError("argv must contain non-NUL command arguments")
    timeout = inputs.get("timeout_seconds", 60)
    cap = inputs.get("max_output_bytes", MAX_OUTPUT_BYTES)
    environment = {"PATH": os.environ.get("PATH", ""), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
    environment.update(inputs.get("env", {}))
    try:
        completed = subprocess.run(
            argv, cwd=cwd, env=environment, stdin=subprocess.DEVNULL, capture_output=True,
            timeout=timeout, check=False, shell=False,
        )
        timed_out = False
        return_code = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        return_code = None
        stdout = exc.stdout or b""
        stderr = exc.stderr or b""
    combined_size = len(stdout) + len(stderr)
    if combined_size > cap:
        stdout_cap = min(len(stdout), cap // 2)
        stderr_cap = max(0, cap - stdout_cap)
        stdout, stderr = stdout[:stdout_cap], stderr[:stderr_cap]
    return {
        "argv": argv,
        "cwd": workspace.relative(cwd),
        "returncode": return_code,
        "stdout": stdout.decode("utf-8", "replace"),
        "stderr": stderr.decode("utf-8", "replace"),
        "timed_out": timed_out,
        "truncated": combined_size > cap,
    }


def _git(context: ToolContext, inputs: dict[str, Any], operation: str) -> dict[str, Any]:
    workspace = _workspace(context)
    cwd = workspace.directory(inputs.get("cwd", "."))
    probe = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], cwd=cwd, capture_output=True, timeout=30, check=False, shell=False
    )
    if probe.returncode != 0:
        raise WorkspaceError(probe.stderr.decode("utf-8", "replace")[:4000] or "not a Git repository")
    repository_root = Path(probe.stdout.decode("utf-8", "replace").strip()).resolve(strict=True)
    if not repository_root.is_relative_to(workspace.root):
        raise WorkspaceError("Git repository root escapes workspace")
    argv = ["git", "status", "--short"] if operation == "status" else ["git", "diff", "--no-ext-diff", "--"]
    if operation == "diff" and inputs.get("staged", False):
        argv.insert(2, "--cached")
    completed = subprocess.run(argv, cwd=cwd, capture_output=True, timeout=30, check=False, shell=False)
    if completed.returncode != 0:
        raise WorkspaceError(completed.stderr.decode("utf-8", "replace")[:4000] or "git command failed")
    output = completed.stdout[:MAX_OUTPUT_BYTES]
    key = "status" if operation == "status" else "diff"
    return {key: output.decode("utf-8", "replace"), "truncated": len(completed.stdout) > MAX_OUTPUT_BYTES}


def _object(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or [], "additionalProperties": False}


def coding_tool_specs() -> tuple[ToolSpec, ...]:
    path = {"type": "string", "minLength": 1}
    sha = {"type": "string", "pattern": "[0-9a-f]{64}"}
    # An omitted/null value means "create"; the handler enforces overwrite rules.
    nullable_sha: dict[str, Any] = {}
    return (
        ToolSpec("fs.read", "1", "Read a UTF-8 workspace file with its content hash.", ToolRisk.READ_ONLY,
                 _object({"path": path, "max_bytes": {"type": "integer", "minimum": 1, "maximum": MAX_FILE_BYTES}}, ["path"]), OBJECT, _read),
        ToolSpec("fs.write", "1", "Atomically create or overwrite a UTF-8 file; overwrites require its prior SHA-256.", ToolRisk.WORKSPACE_WRITE,
                 _object({"path": path, "content": {"type": "string"}, "expected_sha256": nullable_sha}, ["path", "content"]), OBJECT, _write),
        ToolSpec("fs.replace", "1", "Atomically replace exact text after an optimistic-concurrency hash check.", ToolRisk.WORKSPACE_WRITE,
                 _object({"path": path, "old": {"type": "string", "minLength": 1}, "new": {"type": "string"}, "expected_sha256": sha, "count": {"type": "integer", "minimum": 1, "maximum": 1000}, "require_unique": {"type": "boolean"}}, ["path", "old", "new", "expected_sha256"]), OBJECT, _replace),
        ToolSpec("fs.list", "1", "List contained workspace entries.", ToolRisk.READ_ONLY,
                 _object({"path": path, "recursive": {"type": "boolean"}, "limit": {"type": "integer", "minimum": 1, "maximum": MAX_RESULTS}}), OBJECT, _list),
        ToolSpec("fs.search", "1", "Search literal text in workspace files using rg with a safe fallback.", ToolRisk.READ_ONLY,
                 _object({"pattern": {"type": "string", "minLength": 1}, "path": path, "limit": {"type": "integer", "minimum": 1, "maximum": MAX_RESULTS}}, ["pattern"]), OBJECT, _search),
        ToolSpec("process.run", "1", "Run a bounded non-shell subprocess inside the workspace.", ToolRisk.WORKSPACE_WRITE,
                 _object({"argv": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 128}, "cwd": path, "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 600}, "max_output_bytes": {"type": "integer", "minimum": 1024, "maximum": MAX_OUTPUT_BYTES}, "env": {"type": "object"}}, ["argv"]), OBJECT, _run),
        ToolSpec("git.status", "1", "Show concise Git working-tree status.", ToolRisk.READ_ONLY,
                 _object({"cwd": path}), OBJECT, lambda c, i: _git(c, i, "status")),
        ToolSpec("git.diff", "1", "Show the bounded Git working-tree diff.", ToolRisk.READ_ONLY,
                 _object({"cwd": path, "staged": {"type": "boolean"}}), OBJECT, lambda c, i: _git(c, i, "diff")),
    )


def register_coding_tools(registry: ToolRegistry) -> None:
    for specification in coding_tool_specs():
        registry.register(specification)


def coding_tool_catalog() -> list[dict[str, Any]]:
    """Return the stable, JSON-serializable contract exposed to model runtimes."""

    return [
        {
            "name": specification.name,
            "version": specification.version,
            "description": specification.description,
            "risk": specification.risk.value,
            "input_schema": specification.input_schema,
            "output_schema": specification.output_schema,
        }
        for specification in coding_tool_specs()
    ]
