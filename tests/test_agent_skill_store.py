from __future__ import annotations

from pathlib import Path

from ops_agent.agent_registry import AgentUpdateRequest, create_agent_registry, default_agent_definitions
from ops_agent.agent_skill_store import (
    AgentSkillStore,
    seed_agents_from_defaults,
    seed_skills_from_paths,
)
from ops_agent.runtime.skills import SkillRegistry


def test_agent_and_skill_store_roundtrip(tmp_path: Path):
    store = AgentSkillStore(tmp_path / "platform.sqlite3")
    seeded = seed_agents_from_defaults(store)
    assert seeded == len(default_agent_definitions())
    assert store.agent_count() == seeded

    registry = create_agent_registry(store=store)
    updated = registry.update(
        "function-calling-runtime",
        AgentUpdateRequest(name="协调器改名"),
    )
    assert updated.name == "协调器改名"
    reloaded = create_agent_registry(store=store)
    assert reloaded.get("function-calling-runtime").name == "协调器改名"

    skills_root = tmp_path / "skills" / "demo-skill"
    skills_root.mkdir(parents=True)
    (skills_root / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: Demo skill for tests\n"
        "model-invocable: true\nuser-invocable: true\n---\n\n# Demo\n",
        encoding="utf-8",
    )
    assert seed_skills_from_paths(store, str(tmp_path / "skills")) == 1
    skill_registry = SkillRegistry.from_store(store)
    assert skill_registry.get("demo-skill").startswith("---\n")
    skill_registry.upsert(
        name="demo-skill",
        description="Updated demo",
        content=(
            "---\nname: demo-skill\ndescription: Updated demo\n"
            "model-invocable: true\nuser-invocable: true\n---\n\n# Updated\n"
        ),
    )
    assert "Updated demo" in skill_registry.get("demo-skill")
