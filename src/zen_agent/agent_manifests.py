from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re


_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_NAME = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")
_ALLOWED_FIELDS = {
    "name",
    "description",
    "role",
    "tools",
    "skills",
    "model",
    "max_turns",
    "sandbox",
    "memory_scope",
}
_REQUIRED_FIELDS = {"name", "description", "role"}
_SANDBOXES = {"read-only", "workspace-write"}
_MEMORY_SCOPES = {"none", "project", "episodic", "all"}


@dataclass(frozen=True, slots=True)
class AgentManifest:
    name: str
    description: str
    role: str
    tools: tuple[str, ...]
    skills: tuple[str, ...]
    model: str
    max_turns: int
    sandbox: str
    memory_scope: str
    path: Path
    instructions: str


class AgentCatalog:
    """Discover strictly validated, progressively loaded agent manifests."""

    def __init__(self, manifests: dict[str, AgentManifest]):
        self._manifests = dict(manifests)

    @classmethod
    def discover(cls, roots: list[Path]) -> "AgentCatalog":
        manifests: dict[str, AgentManifest] = {}
        for root in roots:
            if not root.is_dir():
                continue
            resolved_root = root.resolve()
            for path in sorted(root.glob("*.md")):
                if path.name.casefold() == "readme.md":
                    continue
                resolved = path.resolve()
                if not resolved.is_relative_to(resolved_root):
                    raise ValueError(f"agent manifest escapes its root: {path}")
                manifest = _parse_manifest(path)
                if manifest.name in manifests:
                    raise ValueError(f"duplicate agent manifest: {manifest.name}")
                manifests[manifest.name] = manifest
        return cls(manifests)

    def list(self) -> list[AgentManifest]:
        return [self._manifests[name] for name in sorted(self._manifests)]

    def get(self, name: str) -> AgentManifest:
        try:
            return self._manifests[name]
        except KeyError as exc:
            raise KeyError(f"unknown agent: {name}") from exc

    def __contains__(self, name: str) -> bool:
        return name in self._manifests


def _parse_manifest(path: Path) -> AgentManifest:
    text = path.read_text(encoding="utf-8")
    match = _FRONTMATTER.match(text)
    if not match:
        raise ValueError(f"agent manifest has no frontmatter: {path}")
    fields = _parse_frontmatter(match.group(1), path)
    missing = _REQUIRED_FIELDS - fields.keys()
    unknown = fields.keys() - _ALLOWED_FIELDS
    if missing:
        raise ValueError(f"agent manifest is missing {sorted(missing)}: {path}")
    if unknown:
        raise ValueError(f"agent manifest has unknown fields {sorted(unknown)}: {path}")

    name = _scalar(fields, "name", path)
    role = _scalar(fields, "role", path)
    description = _scalar(fields, "description", path)
    model = _scalar(fields, "model", path, default="gpt-5.6-sol")
    sandbox = _scalar(fields, "sandbox", path, default="read-only")
    memory_scope = _scalar(fields, "memory_scope", path, default="none")
    if not _NAME.fullmatch(name):
        raise ValueError(f"invalid agent name {name!r}: {path}")
    if not _NAME.fullmatch(role):
        raise ValueError(f"invalid agent role {role!r}: {path}")
    if not description.strip():
        raise ValueError(f"agent description cannot be empty: {path}")
    if not model.strip():
        raise ValueError(f"agent model cannot be empty: {path}")
    if sandbox not in _SANDBOXES:
        raise ValueError(f"invalid sandbox {sandbox!r}: {path}")
    if memory_scope not in _MEMORY_SCOPES:
        raise ValueError(f"invalid memory_scope {memory_scope!r}: {path}")

    max_turns_raw = fields.get("max_turns", "20")
    if isinstance(max_turns_raw, list):
        raise ValueError(f"max_turns must be an integer: {path}")
    try:
        max_turns = int(_unquote(max_turns_raw))
    except ValueError as exc:
        raise ValueError(f"max_turns must be an integer: {path}") from exc
    if not 1 <= max_turns <= 500:
        raise ValueError(f"max_turns must be between 1 and 500: {path}")

    return AgentManifest(
        name=name,
        description=description,
        role=role,
        tools=_string_list(fields, "tools", path),
        skills=_string_list(fields, "skills", path),
        model=model,
        max_turns=max_turns,
        sandbox=sandbox,
        memory_scope=memory_scope,
        path=path,
        instructions=text[match.end() :].strip(),
    )


def _parse_frontmatter(source: str, path: Path) -> dict[str, str | list[str]]:
    fields: dict[str, str | list[str]] = {}
    current_list: str | None = None
    for number, raw in enumerate(source.splitlines(), start=2):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if raw.startswith((" ", "\t")):
            stripped = raw.strip()
            if current_list is None or not stripped.startswith("- "):
                raise ValueError(f"invalid frontmatter line {number}: {path}")
            value = _unquote(stripped[2:].strip())
            if not value:
                raise ValueError(f"empty list item on line {number}: {path}")
            assert isinstance(fields[current_list], list)
            fields[current_list].append(value)
            continue
        current_list = None
        if ":" not in raw:
            raise ValueError(f"invalid frontmatter line {number}: {path}")
        key, raw_value = raw.split(":", 1)
        key = key.strip()
        if key in fields:
            raise ValueError(f"duplicate frontmatter field {key!r}: {path}")
        raw_value = raw_value.strip()
        if not raw_value:
            fields[key] = []
            current_list = key
        elif raw_value.startswith("[") and raw_value.endswith("]"):
            inner = raw_value[1:-1].strip()
            fields[key] = [] if not inner else [_unquote(item.strip()) for item in inner.split(",")]
        else:
            fields[key] = raw_value
    return fields


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _scalar(
    fields: dict[str, str | list[str]], key: str, path: Path, *, default: str | None = None
) -> str:
    value = fields.get(key, default)
    if value is None or isinstance(value, list):
        raise ValueError(f"{key} must be a scalar: {path}")
    return _unquote(value).strip()


def _string_list(fields: dict[str, str | list[str]], key: str, path: Path) -> tuple[str, ...]:
    value = fields.get(key, [])
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a list: {path}")
    if any(not item.strip() for item in value):
        raise ValueError(f"{key} contains an empty item: {path}")
    if len(value) != len(set(value)):
        raise ValueError(f"{key} contains duplicate items: {path}")
    return tuple(value)
