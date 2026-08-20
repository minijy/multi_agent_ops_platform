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
    model_invocable: bool = True
    user_invocable: bool = True
    builtin: bool = False
    content: str = ""
    updated_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "model_invocable": self.model_invocable,
            "user_invocable": self.user_invocable,
            "builtin": self.builtin,
            "updated_at": self.updated_at,
        }

    def as_detail(self) -> dict[str, Any]:
        payload = self.as_dict()
        payload["content"] = self.content
        return payload


class SkillRegistry:
    """Loads skills from DB store (preferred) or filesystem roots (legacy/tests)."""

    def __init__(
        self,
        roots: list[Path] | None = None,
        *,
        store: Any | None = None,
    ) -> None:
        self.roots = roots or []
        self.store = store
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

    @classmethod
    def from_store(cls, store: Any) -> "SkillRegistry":
        registry = cls(store=store)
        registry.scan()
        return registry

    @staticmethod
    def _parse_file(path: Path) -> SkillSummary | None:
        text = path.read_text(encoding="utf-8")
        return SkillRegistry._parse_text(text)

    @staticmethod
    def _parse_text(text: str, *, builtin: bool = False, updated_at: str = "") -> SkillSummary | None:
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
            model_invocable=bool(metadata.get("model-invocable", True)),
            user_invocable=bool(metadata.get("user-invocable", True)),
            builtin=builtin,
            content=text if text.endswith("\n") else text + "\n",
            updated_at=updated_at,
        )

    def scan(self) -> list[SkillSummary]:
        discovered: dict[str, SkillSummary] = {}
        if self.store is not None:
            for item in self.store.list_skills():
                summary = self._parse_text(
                    str(item.get("content") or ""),
                    builtin=bool(item.get("builtin")),
                    updated_at=str(item.get("updated_at") or ""),
                )
                if summary is None:
                    # Fall back to row metadata if content parse fails.
                    name = str(item.get("name") or "").strip()
                    description = str(item.get("description") or "").strip()
                    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) or not description:
                        continue
                    summary = SkillSummary(
                        name=name,
                        description=description,
                        model_invocable=bool(item.get("model_invocable", True)),
                        user_invocable=bool(item.get("user_invocable", True)),
                        builtin=bool(item.get("builtin")),
                        content=str(item.get("content") or ""),
                        updated_at=str(item.get("updated_at") or ""),
                    )
                discovered[summary.name] = summary
            self._skills = discovered
            return self.list()

        for root in reversed(self.roots):
            if not root.is_dir():
                continue
            candidates = [*root.glob("*/SKILL.md"), *root.glob("*.md")]
            for path in sorted(candidates):
                summary = self._parse_file(path)
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
        if skill.content:
            return skill.content
        if self.store is not None:
            row = self.store.get_skill(name)
            if row and row.get("content"):
                return str(row["content"])
        raise KeyError(f"unknown skill: {name}")

    def get_detail(self, name: str) -> SkillSummary:
        try:
            return self._skills[name]
        except KeyError as exc:
            raise KeyError(f"unknown skill: {name}") from exc

    def upsert(
        self,
        *,
        name: str,
        description: str,
        content: str,
        model_invocable: bool = True,
        user_invocable: bool = True,
        builtin: bool | None = None,
    ) -> SkillSummary:
        if self.store is None:
            raise RuntimeError("skill registry is read-only without a database store")
        existing = self.store.get_skill(name)
        payload = {
            "name": name,
            "description": description,
            "content": content,
            "model_invocable": model_invocable,
            "user_invocable": user_invocable,
            "builtin": existing.get("builtin", False) if existing else bool(builtin),
        }
        if builtin is not None and existing is None:
            payload["builtin"] = builtin
        row = self.store.upsert_skill(payload)
        self.scan()
        return self.get_detail(row["name"])

    def delete(self, name: str) -> bool:
        if self.store is None:
            raise RuntimeError("skill registry is read-only without a database store")
        existing = self.store.get_skill(name)
        if existing and existing.get("builtin"):
            raise PermissionError("builtin skills cannot be deleted")
        deleted = self.store.delete_skill(name)
        self.scan()
        return deleted

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
    tools: ToolRegistry, skills: SkillRegistry, *, replace: bool = False
) -> None:
    names = ", ".join(item.name for item in skills.list(model_only=True)) or "none"

    def load_skill(
        arguments: LoadSkillArguments, _context: ToolExecutionContext
    ) -> dict[str, str]:
        return {"name": arguments.name, "content": skills.get(arguments.name)}

    definition = ToolDefinition(
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
    if replace:
        tools.replace(definition)
    else:
        tools.register(definition)
