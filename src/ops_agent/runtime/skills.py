from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from .tools import ToolDefinition, ToolExecutionContext, ToolRegistry


@dataclass(frozen=True)
class SkillSummary:
    name: str
    description: str
    path: Path
    model_invocable: bool = True
    user_invocable: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "model_invocable": self.model_invocable,
            "user_invocable": self.user_invocable,
        }


class SkillRegistry:
    """Discovers metadata eagerly and reads SKILL.md bodies only on demand."""

    def __init__(self, roots: list[Path]) -> None:
        self.roots = roots
        self._skills: dict[str, SkillSummary] = {}

    @classmethod
    def from_paths(cls, raw_paths: str) -> "SkillRegistry":
        roots = [
            Path(item.strip()).expanduser().resolve()
            for item in raw_paths.split(",")
            if item.strip()
        ]
        registry = cls(roots)
        registry.scan()
        return registry

    @staticmethod
    def _parse(path: Path) -> SkillSummary | None:
        text = path.read_text(encoding="utf-8")
        if not text.startswith("---\n"):
            return None
        try:
            frontmatter, _body = text[4:].split("\n---\n", 1)
            metadata = yaml.safe_load(frontmatter) or {}
        except (ValueError, yaml.YAMLError):
            return None
        name = str(metadata.get("name", "")).strip()
        description = str(metadata.get("description", "")).strip()
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) or not description:
            return None
        return SkillSummary(
            name=name,
            description=description,
            path=path,
            model_invocable=bool(metadata.get("model-invocable", True)),
            user_invocable=bool(metadata.get("user-invocable", True)),
        )

    def scan(self) -> list[SkillSummary]:
        discovered: dict[str, SkillSummary] = {}
        # Later roots have lower priority; the nearest/first configured root wins.
        for root in reversed(self.roots):
            if not root.is_dir():
                continue
            candidates = [*root.glob("*/SKILL.md"), *root.glob("*.md")]
            for path in sorted(candidates):
                summary = self._parse(path)
                if summary:
                    discovered[summary.name] = summary
        self._skills = discovered
        return self.list()

    def list(self, *, model_only: bool = False) -> list[SkillSummary]:
        values = sorted(self._skills.values(), key=lambda item: item.name)
        return [item for item in values if not model_only or item.model_invocable]

    def get(self, name: str, *, for_model: bool = True) -> str:
        try:
            skill = self._skills[name]
        except KeyError as exc:
            raise KeyError(f"unknown skill: {name}") from exc
        if for_model and not skill.model_invocable:
            raise PermissionError(f"skill is not model-invocable: {name}")
        return skill.path.read_text(encoding="utf-8")

    def catalog_prompt(
        self,
        *,
        include_names: set[str] | None = None,
    ) -> str:
        skills = self.list(model_only=True)
        if include_names is not None:
            skills = [item for item in skills if item.name in include_names]
        if not skills:
            return ""
        lines = [
            "\n可按需调用 load_skill 加载以下技能；不要在未加载正文时猜测步骤：",
            "<available_skills>",
        ]
        lines.extend(
            f'- name: {item.name}\n  description: {item.description}'
            for item in skills
        )
        lines.append("</available_skills>")
        return "\n".join(lines)


class LoadSkillArguments(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def register_skill_tool(
    tools: ToolRegistry, skills: SkillRegistry
) -> None:
    names = ", ".join(item.name for item in skills.list(model_only=True)) or "none"

    def load_skill(
        arguments: LoadSkillArguments, _context: ToolExecutionContext
    ) -> dict[str, str]:
        return {"name": arguments.name, "content": skills.get(arguments.name)}

    tools.register(
        ToolDefinition(
            name="load_skill",
            description=(
                "按需加载一个 SKILL.md 的完整操作说明。先根据目录选择技能，再调用本工具。"
                f"当前技能：{names}"
            ),
            arguments_model=LoadSkillArguments,
            handler=load_skill,
            source="skill",
            builtin=True,
        )
    )
