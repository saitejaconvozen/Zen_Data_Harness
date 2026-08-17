from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .skills import SkillCatalog


class ContextCompiler:
    def __init__(self, instruction_file: Path, skills: SkillCatalog, max_chars: int = 60_000):
        self.instruction_file = instruction_file
        self.skills = skills
        self.max_chars = max_chars

    def compile(self, objective: str, selected_skills: tuple[str, ...]) -> str:
        sections = [
            "# Repository operating contract\n" + self.instruction_file.read_text(encoding="utf-8"),
            "# Objective\n" + objective,
        ]
        for name in selected_skills:
            sections.append(f"# Selected skill: {name}\n{self.skills.load_body(name)}")
        result = "\n\n".join(sections)
        if len(result) > self.max_chars:
            raise ValueError("compiled context exceeds configured character budget")
        return result


@dataclass(frozen=True, slots=True)
class ContextDocument:
    path: Path
    content: str


class WorkspaceContextCompiler:
    """Compile workspace-scoped repository guidance without partial documents."""

    def __init__(self, workspace_root: Path, *, max_chars: int = 60_000):
        self.workspace_root = workspace_root.resolve()
        self.max_chars = max_chars
        if not self.workspace_root.is_dir():
            raise ValueError(f"workspace root is not a directory: {workspace_root}")
        if max_chars < 1:
            raise ValueError("max_chars must be positive")

    def discover(self, target_paths: tuple[str | Path, ...] = ()) -> tuple[ContextDocument, ...]:
        zen = self._contained(self.workspace_root / "ZEN.md")
        if not zen.is_file():
            raise FileNotFoundError(f"workspace operating contract not found: {zen}")
        paths: list[Path] = [zen]
        targets = target_paths or (Path("."),)
        for raw_target in targets:
            target = Path(raw_target)
            if not target.is_absolute():
                target = self.workspace_root / target
            target = self._contained(target)
            directory = target if target.is_dir() else target.parent
            relative = directory.relative_to(self.workspace_root)
            current = self.workspace_root
            candidate = current / "AGENTS.md"
            if candidate.is_file():
                paths.append(self._contained(candidate))
            for component in relative.parts:
                current = current / component
                candidate = current / "AGENTS.md"
                if candidate.is_file():
                    paths.append(self._contained(candidate))

        seen: set[Path] = set()
        documents: list[ContextDocument] = []
        for path in paths:
            if path in seen:
                continue
            seen.add(path)
            documents.append(ContextDocument(path, path.read_text(encoding="utf-8")))
        return tuple(documents)

    def compile(
        self,
        objective: str,
        target_paths: tuple[str | Path, ...] = (),
        *,
        extra_sections: tuple[tuple[str, str], ...] = (),
    ) -> str:
        if not objective.strip():
            raise ValueError("objective cannot be empty")
        documents = self.discover(target_paths)
        root = documents[0]
        result = "\n\n".join(
            [
                f"# Repository operating contract ({root.path.name})\n{root.content}",
                f"# Objective\n{objective}",
            ]
        )
        if len(result) > self.max_chars:
            raise ValueError("root contract and objective exceed configured character budget")

        omitted: list[str] = []
        optional_sections = [
            (str(document.path.relative_to(self.workspace_root)), document.content)
            for document in documents[1:]
        ] + list(extra_sections)
        for title, content in optional_sections:
            section = f"# Applicable context: {title}\n{content}"
            candidate = result + "\n\n" + section
            if len(candidate) <= self.max_chars:
                result = candidate
            else:
                omitted.append(title)
        if omitted:
            marker = "\n\n# Context budget notice\nOmitted complete sections: " + ", ".join(omitted)
            if len(result) + len(marker) <= self.max_chars:
                result += marker
        return result

    def _contained(self, path: Path) -> Path:
        resolved = path.resolve()
        if not resolved.is_relative_to(self.workspace_root):
            raise ValueError(f"context path escapes workspace: {path}")
        return resolved
