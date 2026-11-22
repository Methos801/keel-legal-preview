"""Skills discovery and loader (§11).

Discovers skills in project (.keel/skills/), global (~/.keel/skills/), and built-ins.
Parses SKILL.md frontmatter (compatible with Anthropic/Pi layout).
Generates deterministic one-line index for system prompt prefix.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
import yaml

logger = logging.getLogger("keel.skills")


def _normalize_modes(raw_modes: Any) -> list[str]:
    if raw_modes is None:
        return ["all"]
    if isinstance(raw_modes, str):
        val = raw_modes.strip().lower()
        return [val] if val else ["all"]
    if isinstance(raw_modes, (list, tuple, set)):
        cleaned = [str(m).strip().lower() for m in raw_modes if str(m).strip()]
        return cleaned if cleaned else ["all"]
    return ["all"]


def _allowed_modes_for_practice(practice: str) -> set[str]:
    p = practice.strip().lower()
    if not p or p == "dev":
        return {"all", "dev"}
    if p == "legal":
        return {"all", "legal"}
    if p == "ga-ca":
        return {"all", "legal", "ga-ca"}
    return {"all", p}


class Skill:
    def __init__(
        self,
        name: str,
        description: str,
        body: str,
        path: Path,
        raw_meta: dict[str, Any],
        modes: list[str] | None = None,
    ) -> None:
        self.name = name
        self.description = description
        self.body = body
        self.path = path
        self.raw_meta = raw_meta
        self.modes: list[str] = _normalize_modes(modes if modes is not None else raw_meta.get("modes"))

    def matches_practice(self, practice: str | None) -> bool:
        if practice is None:
            return True
        allowed = _allowed_modes_for_practice(practice)
        return any(m in allowed for m in self.modes)

    def to_index_line(self) -> str:
        desc = " ".join(self.description.split()[:14])
        return f"{self.name} — {desc}"


class SkillLoader:
    def __init__(self, workspace: Path | str | None = None) -> None:
        self.workspace = Path(workspace).resolve() if workspace else None
        self._roots: list[Path] = []
        if self.workspace:
            self._roots.append(self.workspace / ".keel" / "skills")
        # This preview never reads a user profile.
        self._roots.append(Path(__file__).parent / "builtin")

    def list_skills(self, practice: str | None = None) -> list[Skill]:
        found: dict[str, Skill] = {}
        for root in self._roots:
            if not root.is_dir():
                continue
            for item in sorted(root.iterdir()):
                if item.is_dir():
                    skill_md = item / "SKILL.md"
                    if skill_md.is_file() and item.name not in found:
                        s = self._parse_skill_file(skill_md, default_name=item.name)
                        if s and s.matches_practice(practice):
                            found[s.name] = s
        return sorted(found.values(), key=lambda x: x.name)

    def get_skill(self, name: str, practice: str | None = None) -> Skill | None:
        skills = self.list_skills(practice=practice)
        for s in skills:
            if s.name == name:
                return s
        return None

    def get_prefix_index(self, practice: str | None = None) -> list[str]:
        return [s.to_index_line() for s in self.list_skills(practice=practice)]

    @staticmethod
    def _parse_skill_file(path: Path, default_name: str) -> Skill | None:
        try:
            content = path.read_text(encoding="utf-8")
            if content.startswith("---"):
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    frontmatter = yaml.safe_load(parts[1]) or {}
                    body = parts[2].strip()
                    name = str(frontmatter.get("name", default_name))
                    desc = str(frontmatter.get("description", ""))
                    modes = _normalize_modes(frontmatter.get("modes"))
                    return Skill(name, desc, body, path, frontmatter, modes=modes)
            return Skill(default_name, "", content.strip(), path, {}, modes=["all"])
        except Exception as exc:
            logger.warning("Failed to parse skill at %s: %s", path, exc)
            return None
