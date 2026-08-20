"""Control-plane persistence for agent definitions and SKILL.md documents."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Settings

_SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def build_skill_markdown(
    *,
    name: str,
    description: str,
    body: str,
    model_invocable: bool = True,
    user_invocable: bool = True,
) -> str:
    body = body.strip()
    if body.startswith("#"):
        content_body = body
    else:
        content_body = body
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        f"model-invocable: {'true' if model_invocable else 'false'}\n"
        f"user-invocable: {'true' if user_invocable else 'false'}\n"
        "---\n\n"
        f"{content_body.rstrip()}\n"
    )


class AgentSkillStore:
    """SQLite store for agents + skills (same file as other control-plane tables)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS agent_definitions(
                    agent_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    role TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    system_prompt TEXT NOT NULL DEFAULT '',
                    allowed_tools_json TEXT NOT NULL DEFAULT '[]',
                    strict_tool_allowlist INTEGER NOT NULL DEFAULT 0,
                    workflow_id TEXT NOT NULL DEFAULT '',
                    builtin INTEGER NOT NULL DEFAULT 1,
                    integration_json TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agent_skills(
                    name TEXT PRIMARY KEY,
                    description TEXT NOT NULL,
                    content TEXT NOT NULL,
                    model_invocable INTEGER NOT NULL DEFAULT 1,
                    user_invocable INTEGER NOT NULL DEFAULT 1,
                    builtin INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    # --- agents ---

    def list_agents(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM agent_definitions ORDER BY agent_id"
            ).fetchall()
        return [self._agent_row(row) for row in rows]

    def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM agent_definitions WHERE agent_id=?",
                (agent_id,),
            ).fetchone()
        return self._agent_row(row) if row else None

    def upsert_agent(self, payload: dict[str, Any]) -> dict[str, Any]:
        agent_id = str(payload["id"])
        now = _utcnow()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO agent_definitions(
                    agent_id,name,role,kind,description,enabled,system_prompt,
                    allowed_tools_json,strict_tool_allowlist,workflow_id,builtin,
                    integration_json,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    name=excluded.name,
                    role=excluded.role,
                    kind=excluded.kind,
                    description=excluded.description,
                    enabled=excluded.enabled,
                    system_prompt=excluded.system_prompt,
                    allowed_tools_json=excluded.allowed_tools_json,
                    strict_tool_allowlist=excluded.strict_tool_allowlist,
                    workflow_id=excluded.workflow_id,
                    builtin=excluded.builtin,
                    integration_json=excluded.integration_json,
                    updated_at=excluded.updated_at
                """,
                (
                    agent_id,
                    str(payload.get("name") or ""),
                    str(payload.get("role") or ""),
                    str(payload.get("kind") or "role"),
                    str(payload.get("description") or ""),
                    1 if payload.get("enabled", True) else 0,
                    str(payload.get("system_prompt") or ""),
                    json.dumps(payload.get("allowed_tools") or [], ensure_ascii=False),
                    1 if payload.get("strict_tool_allowlist") else 0,
                    str(payload.get("workflow_id") or ""),
                    1 if payload.get("builtin", True) else 0,
                    json.dumps(payload.get("integration") or {}, ensure_ascii=False),
                    now,
                ),
            )
        return self.get_agent(agent_id) or payload

    def replace_agents(self, agents: list[dict[str, Any]]) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM agent_definitions")
            now = _utcnow()
            for payload in agents:
                connection.execute(
                    """
                    INSERT INTO agent_definitions(
                        agent_id,name,role,kind,description,enabled,system_prompt,
                        allowed_tools_json,strict_tool_allowlist,workflow_id,builtin,
                        integration_json,updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        str(payload["id"]),
                        str(payload.get("name") or ""),
                        str(payload.get("role") or ""),
                        str(payload.get("kind") or "role"),
                        str(payload.get("description") or ""),
                        1 if payload.get("enabled", True) else 0,
                        str(payload.get("system_prompt") or ""),
                        json.dumps(payload.get("allowed_tools") or [], ensure_ascii=False),
                        1 if payload.get("strict_tool_allowlist") else 0,
                        str(payload.get("workflow_id") or ""),
                        1 if payload.get("builtin", True) else 0,
                        json.dumps(payload.get("integration") or {}, ensure_ascii=False),
                        now,
                    ),
                )

    def agent_count(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS n FROM agent_definitions"
            ).fetchone()
        return int(row["n"] if row else 0)

    @staticmethod
    def _agent_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["agent_id"],
            "name": row["name"],
            "role": row["role"],
            "kind": row["kind"],
            "description": row["description"] or "",
            "enabled": bool(row["enabled"]),
            "system_prompt": row["system_prompt"] or "",
            "allowed_tools": json.loads(row["allowed_tools_json"] or "[]"),
            "strict_tool_allowlist": bool(row["strict_tool_allowlist"]),
            "workflow_id": row["workflow_id"] or "",
            "builtin": bool(row["builtin"]),
            "integration": json.loads(row["integration_json"] or "{}"),
            "updated_at": row["updated_at"],
        }

    # --- skills ---

    def list_skills(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM agent_skills ORDER BY name"
            ).fetchall()
        return [self._skill_row(row) for row in rows]

    def get_skill(self, name: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM agent_skills WHERE name=?",
                (name,),
            ).fetchone()
        return self._skill_row(row) if row else None

    def upsert_skill(self, payload: dict[str, Any]) -> dict[str, Any]:
        name = str(payload["name"]).strip()
        if not _SKILL_NAME_RE.fullmatch(name):
            raise ValueError("skill name must be kebab-case [a-z0-9-]")
        description = str(payload.get("description") or "").strip()
        if not description:
            raise ValueError("skill description is required")
        content = str(payload.get("content") or "").strip()
        if not content:
            body = str(payload.get("body") or "").strip() or f"# {name}\n"
            content = build_skill_markdown(
                name=name,
                description=description,
                body=body,
                model_invocable=bool(payload.get("model_invocable", True)),
                user_invocable=bool(payload.get("user_invocable", True)),
            )
        now = _utcnow()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO agent_skills(
                    name,description,content,model_invocable,user_invocable,builtin,updated_at
                ) VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(name) DO UPDATE SET
                    description=excluded.description,
                    content=excluded.content,
                    model_invocable=excluded.model_invocable,
                    user_invocable=excluded.user_invocable,
                    builtin=excluded.builtin,
                    updated_at=excluded.updated_at
                """,
                (
                    name,
                    description,
                    content,
                    1 if payload.get("model_invocable", True) else 0,
                    1 if payload.get("user_invocable", True) else 0,
                    1 if payload.get("builtin") else 0,
                    now,
                ),
            )
        return self.get_skill(name) or payload

    def delete_skill(self, name: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM agent_skills WHERE name=? AND builtin=0",
                (name,),
            )
            return cursor.rowcount > 0

    def skill_count(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS n FROM agent_skills"
            ).fetchone()
        return int(row["n"] if row else 0)

    @staticmethod
    def _skill_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "name": row["name"],
            "description": row["description"],
            "content": row["content"],
            "model_invocable": bool(row["model_invocable"]),
            "user_invocable": bool(row["user_invocable"]),
            "builtin": bool(row["builtin"]),
            "updated_at": row["updated_at"],
        }


class PostgresAgentSkillStore:
    """Postgres twin of AgentSkillStore."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        import psycopg
        from psycopg.rows import dict_row

        self._psycopg = psycopg
        self._dict_row = dict_row
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS ops_agent_definitions(
                    agent_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    role TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    enabled BOOLEAN NOT NULL DEFAULT TRUE,
                    system_prompt TEXT NOT NULL DEFAULT '',
                    allowed_tools_json TEXT NOT NULL DEFAULT '[]',
                    strict_tool_allowlist BOOLEAN NOT NULL DEFAULT FALSE,
                    workflow_id TEXT NOT NULL DEFAULT '',
                    builtin BOOLEAN NOT NULL DEFAULT TRUE,
                    integration_json TEXT NOT NULL DEFAULT '{}',
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS ops_agent_skills(
                    name TEXT PRIMARY KEY,
                    description TEXT NOT NULL,
                    content TEXT NOT NULL,
                    model_invocable BOOLEAN NOT NULL DEFAULT TRUE,
                    user_invocable BOOLEAN NOT NULL DEFAULT TRUE,
                    builtin BOOLEAN NOT NULL DEFAULT FALSE,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )
            connection.commit()

    def _connect(self):
        return self._psycopg.connect(self.dsn, row_factory=self._dict_row)

    def list_agents(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM ops_agent_definitions ORDER BY agent_id"
            ).fetchall()
        return [self._agent_row(row) for row in rows]

    def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM ops_agent_definitions WHERE agent_id=%s",
                (agent_id,),
            ).fetchone()
        return self._agent_row(row) if row else None

    def upsert_agent(self, payload: dict[str, Any]) -> dict[str, Any]:
        agent_id = str(payload["id"])
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO ops_agent_definitions(
                    agent_id,name,role,kind,description,enabled,system_prompt,
                    allowed_tools_json,strict_tool_allowlist,workflow_id,builtin,
                    integration_json,updated_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                ON CONFLICT(agent_id) DO UPDATE SET
                    name=EXCLUDED.name,
                    role=EXCLUDED.role,
                    kind=EXCLUDED.kind,
                    description=EXCLUDED.description,
                    enabled=EXCLUDED.enabled,
                    system_prompt=EXCLUDED.system_prompt,
                    allowed_tools_json=EXCLUDED.allowed_tools_json,
                    strict_tool_allowlist=EXCLUDED.strict_tool_allowlist,
                    workflow_id=EXCLUDED.workflow_id,
                    builtin=EXCLUDED.builtin,
                    integration_json=EXCLUDED.integration_json,
                    updated_at=NOW()
                """,
                (
                    agent_id,
                    str(payload.get("name") or ""),
                    str(payload.get("role") or ""),
                    str(payload.get("kind") or "role"),
                    str(payload.get("description") or ""),
                    bool(payload.get("enabled", True)),
                    str(payload.get("system_prompt") or ""),
                    json.dumps(payload.get("allowed_tools") or [], ensure_ascii=False),
                    bool(payload.get("strict_tool_allowlist")),
                    str(payload.get("workflow_id") or ""),
                    bool(payload.get("builtin", True)),
                    json.dumps(payload.get("integration") or {}, ensure_ascii=False),
                ),
            )
            connection.commit()
        return self.get_agent(agent_id) or payload

    def replace_agents(self, agents: list[dict[str, Any]]) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM ops_agent_definitions")
            for payload in agents:
                connection.execute(
                    """
                    INSERT INTO ops_agent_definitions(
                        agent_id,name,role,kind,description,enabled,system_prompt,
                        allowed_tools_json,strict_tool_allowlist,workflow_id,builtin,
                        integration_json,updated_at
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                    """,
                    (
                        str(payload["id"]),
                        str(payload.get("name") or ""),
                        str(payload.get("role") or ""),
                        str(payload.get("kind") or "role"),
                        str(payload.get("description") or ""),
                        bool(payload.get("enabled", True)),
                        str(payload.get("system_prompt") or ""),
                        json.dumps(payload.get("allowed_tools") or [], ensure_ascii=False),
                        bool(payload.get("strict_tool_allowlist")),
                        str(payload.get("workflow_id") or ""),
                        bool(payload.get("builtin", True)),
                        json.dumps(payload.get("integration") or {}, ensure_ascii=False),
                    ),
                )
            connection.commit()

    def agent_count(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS n FROM ops_agent_definitions"
            ).fetchone()
        return int(row["n"] if row else 0)

    @staticmethod
    def _agent_row(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["agent_id"],
            "name": row["name"],
            "role": row["role"],
            "kind": row["kind"],
            "description": row.get("description") or "",
            "enabled": bool(row["enabled"]),
            "system_prompt": row.get("system_prompt") or "",
            "allowed_tools": json.loads(row.get("allowed_tools_json") or "[]"),
            "strict_tool_allowlist": bool(row["strict_tool_allowlist"]),
            "workflow_id": row.get("workflow_id") or "",
            "builtin": bool(row["builtin"]),
            "integration": json.loads(row.get("integration_json") or "{}"),
            "updated_at": str(row.get("updated_at") or ""),
        }

    def list_skills(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM ops_agent_skills ORDER BY name"
            ).fetchall()
        return [self._skill_row(row) for row in rows]

    def get_skill(self, name: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM ops_agent_skills WHERE name=%s",
                (name,),
            ).fetchone()
        return self._skill_row(row) if row else None

    def upsert_skill(self, payload: dict[str, Any]) -> dict[str, Any]:
        name = str(payload["name"]).strip()
        if not _SKILL_NAME_RE.fullmatch(name):
            raise ValueError("skill name must be kebab-case [a-z0-9-]")
        description = str(payload.get("description") or "").strip()
        if not description:
            raise ValueError("skill description is required")
        content = str(payload.get("content") or "").strip()
        if not content:
            body = str(payload.get("body") or "").strip() or f"# {name}\n"
            content = build_skill_markdown(
                name=name,
                description=description,
                body=body,
                model_invocable=bool(payload.get("model_invocable", True)),
                user_invocable=bool(payload.get("user_invocable", True)),
            )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO ops_agent_skills(
                    name,description,content,model_invocable,user_invocable,builtin,updated_at
                ) VALUES (%s,%s,%s,%s,%s,%s,NOW())
                ON CONFLICT(name) DO UPDATE SET
                    description=EXCLUDED.description,
                    content=EXCLUDED.content,
                    model_invocable=EXCLUDED.model_invocable,
                    user_invocable=EXCLUDED.user_invocable,
                    builtin=EXCLUDED.builtin,
                    updated_at=NOW()
                """,
                (
                    name,
                    description,
                    content,
                    bool(payload.get("model_invocable", True)),
                    bool(payload.get("user_invocable", True)),
                    bool(payload.get("builtin")),
                ),
            )
            connection.commit()
        return self.get_skill(name) or payload

    def delete_skill(self, name: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM ops_agent_skills WHERE name=%s AND builtin=FALSE",
                (name,),
            )
            connection.commit()
            return cursor.rowcount > 0

    def skill_count(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS n FROM ops_agent_skills"
            ).fetchone()
        return int(row["n"] if row else 0)

    @staticmethod
    def _skill_row(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "name": row["name"],
            "description": row["description"],
            "content": row["content"],
            "model_invocable": bool(row["model_invocable"]),
            "user_invocable": bool(row["user_invocable"]),
            "builtin": bool(row["builtin"]),
            "updated_at": str(row.get("updated_at") or ""),
        }


def create_agent_skill_store(
    settings: Settings,
) -> AgentSkillStore | PostgresAgentSkillStore:
    if settings.control_plane_backend == "postgres":
        return PostgresAgentSkillStore(settings.postgres_dsn)
    return AgentSkillStore(settings.platform_db_path)


def seed_agents_from_defaults(
    store: AgentSkillStore | PostgresAgentSkillStore,
    *,
    legacy_json: Path | None = None,
) -> int:
    """Seed agents when the table is empty. Prefer legacy JSON overrides if present."""
    if store.agent_count() > 0:
        return 0
    from .agent_registry import default_agent_definitions

    defaults = {item.id: item.model_dump(mode="json") for item in default_agent_definitions()}
    if isinstance(store, PostgresAgentSkillStore):
        from .connector_control_plane import HYBRID_AGENT_TO_TOOL

        defaults = {
            agent_id: payload
            for agent_id, payload in defaults.items()
            if agent_id not in HYBRID_AGENT_TO_TOOL and payload.get("kind") != "hybrid"
        }
    if legacy_json and legacy_json.is_file():
        try:
            loaded = json.loads(legacy_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            loaded = {}
        if isinstance(loaded, dict):
            for agent_id, override in loaded.items():
                if agent_id in defaults and isinstance(override, dict):
                    merged = {**defaults[agent_id], **override, "id": agent_id}
                    defaults[agent_id] = merged
    payloads = list(defaults.values())
    store.replace_agents(payloads)
    return len(payloads)


def seed_skills_from_paths(
    store: AgentSkillStore | PostgresAgentSkillStore,
    raw_paths: str,
) -> int:
    """Seed skills when the table is empty by scanning SKILL.md files."""
    if store.skill_count() > 0:
        return 0
    import yaml

    count = 0
    roots = [
        Path(item.strip()).expanduser().resolve()
        for item in raw_paths.split(",")
        if item.strip()
    ]
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted([*root.glob("*/SKILL.md"), *root.glob("*.md")]):
            text = path.read_text(encoding="utf-8")
            if not text.startswith("---\n"):
                continue
            try:
                frontmatter, _body = text[4:].split("\n---\n", 1)
                metadata = yaml.safe_load(frontmatter) or {}
            except (ValueError, yaml.YAMLError):
                continue
            name = str(metadata.get("name", "")).strip()
            description = str(metadata.get("description", "")).strip()
            if not _SKILL_NAME_RE.fullmatch(name) or not description:
                continue
            store.upsert_skill(
                {
                    "name": name,
                    "description": description,
                    "content": text if text.endswith("\n") else text + "\n",
                    "model_invocable": bool(metadata.get("model-invocable", True)),
                    "user_invocable": bool(metadata.get("user-invocable", True)),
                    "builtin": True,
                }
            )
            count += 1
    return count
