from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re


_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


@dataclass(frozen=True, slots=True)
class SkillMetadata:
    name: str
    description: str
    path: Path


class SkillCatalog:
    """Discover metadata eagerly and load skill bodies only when selected."""

    def __init__(self, skills: dict[str, SkillMetadata]):
        self._skills = skills

    @classmethod
    def discover(cls, roots: list[Path]) -> "SkillCatalog":
        skills: dict[str, SkillMetadata] = {}
        for root in roots:
            if not root.is_dir():
                continue
            for path in sorted(root.glob("*/SKILL.md")):
                text = path.read_text(encoding="utf-8")
                match = _FRONTMATTER.match(text)
                if not match:
                    raise ValueError(f"skill has no YAML frontmatter: {path}")
                fields: dict[str, str] = {}
                for line in match.group(1).splitlines():
                    if ":" not in line:
                        raise ValueError(f"invalid skill frontmatter line: {path}")
                    key, value = line.split(":", 1)
                    fields[key.strip()] = value.strip().strip('"').strip("'")
                if set(fields) != {"name", "description"}:
                    raise ValueError(f"skill frontmatter must contain only name and description: {path}")
                name = fields["name"]
                if name in skills:
                    raise ValueError(f"duplicate skill: {name}")
                skills[name] = SkillMetadata(name, fields["description"], path)
        return cls(skills)

    def list(self) -> list[SkillMetadata]:
        return [self._skills[name] for name in sorted(self._skills)]

    def load_body(self, name: str) -> str:
        metadata = self._skills[name]
        text = metadata.path.read_text(encoding="utf-8")
        match = _FRONTMATTER.match(text)
        if not match:
            raise ValueError(f"invalid skill: {name}")
        return text[match.end() :]

    def select(self, objective: str) -> tuple[str, ...]:
        lowered = objective.casefold()
        selected = []
        for skill in self.list():
            terms = set(re.findall(r"[a-z0-9]+", f"{skill.name} {skill.description}".casefold()))
            score = sum(1 for term in terms if len(term) >= 5 and term in lowered)
            if score:
                selected.append((score, skill.name))
        return tuple(name for _, name in sorted(selected, reverse=True))
