from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Settings
from .agent_roles import SYSTEM_DEFAULT_TOOL_NAMES


class ToolAssignmentConflict(ValueError):
    """A Tool may belong to only one permission rule within a group."""


@dataclass(frozen=True)
class EffectiveAccess:
    configured: bool
    user_exists: bool
    user_enabled: bool
    allowed_tools: frozenset[str] | None
    group_ids: tuple[str, ...] = ()
    rule_ids: tuple[str, ...] = ()

    def denial_detail(self, tool_name: str | None = None) -> dict[str, Any]:
        if not self.configured:
            return {
                "code": "access_control_not_configured",
                "message": "当前租户尚未启用权限控制。",
            }
        if not self.user_exists:
            return {
                "code": "access_user_not_registered",
                "message": "当前用户尚未加入该租户的权限体系。",
                "hint": "请联系管理员在“用户与权限”页面添加用户并绑定权限组。",
            }
        if not self.user_enabled:
            return {
                "code": "access_user_disabled",
                "message": "当前用户已被停用，无法调用工具。",
                "hint": "请联系管理员重新启用该用户。",
            }
        if not self.group_ids:
            return {
                "code": "permission_group_missing",
                "message": "当前用户尚未绑定权限组，无法调用工具。",
                "hint": "请联系管理员为用户绑定权限组。",
            }
        if not self.rule_ids:
            return {
                "code": "permission_rule_missing",
                "message": "当前用户的权限组尚未绑定权限规则。",
                "hint": "请联系管理员为权限组绑定工具权限规则。",
            }
        return {
            "code": "tool_not_granted",
            "message": f"当前用户没有工具 {tool_name} 的访问权限。"
            if tool_name
            else "当前用户没有可用的工具权限。",
            "hint": "请联系管理员把对应工具加入权限规则。",
            **({"tool_name": tool_name} if tool_name else {}),
        }


class AccessControlStore:
    """Tenant-scoped RBAC store.

    A tenant with no managed users remains in compatibility mode. As soon as its
    first user is created, only enabled users with group/rule grants receive tools.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            direct_group_tools_existed = connection.execute(
                """SELECT 1 FROM sqlite_master
                   WHERE type='table' AND name='group_tool_permissions'"""
            ).fetchone() is not None
            connection.executescript(
                """
                PRAGMA foreign_keys=ON;
                CREATE TABLE IF NOT EXISTS access_users(
                    tenant_id TEXT NOT NULL, user_id TEXT NOT NULL,
                    name TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY(tenant_id,user_id)
                );
                CREATE TABLE IF NOT EXISTS permission_groups(
                    tenant_id TEXT NOT NULL, group_id TEXT NOT NULL,
                    name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(tenant_id,group_id)
                );
                CREATE TABLE IF NOT EXISTS permission_rules(
                    tenant_id TEXT NOT NULL, rule_id TEXT NOT NULL,
                    group_id TEXT,
                    name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
                    tool_names_json TEXT NOT NULL DEFAULT '[]',
                    PRIMARY KEY(tenant_id,rule_id),
                    FOREIGN KEY(tenant_id,group_id) REFERENCES permission_groups(tenant_id,group_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS user_permission_groups(
                    tenant_id TEXT NOT NULL, user_id TEXT NOT NULL, group_id TEXT NOT NULL,
                    PRIMARY KEY(tenant_id,user_id,group_id),
                    FOREIGN KEY(tenant_id,user_id) REFERENCES access_users(tenant_id,user_id) ON DELETE CASCADE,
                    FOREIGN KEY(tenant_id,group_id) REFERENCES permission_groups(tenant_id,group_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS permission_rule_tools(
                    tenant_id TEXT NOT NULL, tool_name TEXT NOT NULL, rule_id TEXT NOT NULL,
                    PRIMARY KEY(tenant_id,tool_name),
                    FOREIGN KEY(tenant_id,rule_id) REFERENCES permission_rules(tenant_id,rule_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS permission_group_tools(
                    tenant_id TEXT NOT NULL, group_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL, rule_id TEXT NOT NULL,
                    PRIMARY KEY(tenant_id,group_id,tool_name),
                    FOREIGN KEY(tenant_id,group_id) REFERENCES permission_groups(tenant_id,group_id) ON DELETE CASCADE,
                    FOREIGN KEY(tenant_id,rule_id) REFERENCES permission_rules(tenant_id,rule_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS group_tool_permissions(
                    tenant_id TEXT NOT NULL, group_id TEXT NOT NULL, tool_name TEXT NOT NULL,
                    PRIMARY KEY(tenant_id,group_id,tool_name),
                    FOREIGN KEY(tenant_id,group_id) REFERENCES permission_groups(tenant_id,group_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS group_permission_rules(
                    tenant_id TEXT NOT NULL, group_id TEXT NOT NULL, rule_id TEXT NOT NULL,
                    PRIMARY KEY(tenant_id,group_id,rule_id),
                    FOREIGN KEY(tenant_id,group_id) REFERENCES permission_groups(tenant_id,group_id) ON DELETE CASCADE,
                    FOREIGN KEY(tenant_id,rule_id) REFERENCES permission_rules(tenant_id,rule_id) ON DELETE CASCADE
                );
                """
            )
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(permission_rules)")
            }
            if "group_id" not in columns:
                connection.execute("ALTER TABLE permission_rules ADD COLUMN group_id TEXT")
            connection.execute(
                """UPDATE permission_rules SET group_id=(
                    SELECT MIN(group_id) FROM group_permission_rules legacy
                    WHERE legacy.tenant_id=permission_rules.tenant_id
                      AND legacy.rule_id=permission_rules.rule_id)
                   WHERE group_id IS NULL AND EXISTS(
                    SELECT 1 FROM group_permission_rules legacy
                    WHERE legacy.tenant_id=permission_rules.tenant_id
                      AND legacy.rule_id=permission_rules.rule_id)"""
            )
            connection.execute("DROP TABLE group_permission_rules")
            connection.execute("DELETE FROM permission_group_tools")
            for row in connection.execute(
                """SELECT tenant_id,rule_id,group_id,tool_names_json
                   FROM permission_rules ORDER BY tenant_id,group_id,rule_id"""
            ).fetchall():
                original_tools = set(json.loads(row["tool_names_json"] or "[]"))
                configured_tools = sorted(
                    original_tools - SYSTEM_DEFAULT_TOOL_NAMES
                )
                if original_tools and not configured_tools:
                    connection.execute(
                        "DELETE FROM permission_rules WHERE tenant_id=? AND rule_id=?",
                        (row["tenant_id"], row["rule_id"]),
                    )
                    continue
                owned_tools = []
                for tool_name in configured_tools:
                    if not row["group_id"]:
                        owned_tools.append(tool_name)
                        continue
                    inserted = connection.execute(
                        """INSERT OR IGNORE INTO permission_group_tools(
                           tenant_id,group_id,tool_name,rule_id) VALUES(?,?,?,?)""",
                        (row["tenant_id"], row["group_id"], tool_name, row["rule_id"]),
                    )
                    if inserted.rowcount:
                        owned_tools.append(tool_name)
                connection.execute(
                    """UPDATE permission_rules SET tool_names_json=?
                       WHERE tenant_id=? AND rule_id=?""",
                    (json.dumps(owned_tools), row["tenant_id"], row["rule_id"]),
                )
            if not direct_group_tools_existed:
                connection.execute(
                    """INSERT OR IGNORE INTO group_tool_permissions(
                       tenant_id,group_id,tool_name)
                       SELECT tenant_id,group_id,tool_name FROM permission_group_tools"""
                )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @staticmethod
    def new_id(prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex[:12]}"

    def put_user(
        self, tenant_id: str, user_id: str, name: str, enabled: bool = True
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO access_users(tenant_id,user_id,name,enabled) VALUES(?,?,?,?)
                ON CONFLICT(tenant_id,user_id) DO UPDATE SET name=excluded.name,enabled=excluded.enabled""",
                (tenant_id, user_id, name, int(enabled)),
            )
        return self.get_user(tenant_id, user_id) or {}

    def get_user(self, tenant_id: str, user_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM access_users WHERE tenant_id=? AND user_id=?", (tenant_id, user_id)
            ).fetchone()
            if not row:
                return None
            groups = connection.execute(
                "SELECT group_id FROM user_permission_groups WHERE tenant_id=? AND user_id=? ORDER BY group_id",
                (tenant_id, user_id),
            ).fetchall()
        return {
            "id": row["user_id"],
            "name": row["name"],
            "enabled": bool(row["enabled"]),
            "group_ids": [item[0] for item in groups],
        }

    def list_users(self, tenant_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            ids = [
                row[0]
                for row in connection.execute(
                    "SELECT user_id FROM access_users WHERE tenant_id=? ORDER BY name,user_id",
                    (tenant_id,),
                ).fetchall()
            ]
        return [item for user_id in ids if (item := self.get_user(tenant_id, user_id))]

    def delete_user(self, tenant_id: str, user_id: str) -> bool:
        with self._connect() as connection:
            return (
                connection.execute(
                    "DELETE FROM access_users WHERE tenant_id=? AND user_id=?", (tenant_id, user_id)
                ).rowcount
                > 0
            )

    def put_group(
        self, tenant_id: str, group_id: str, name: str, description: str = ""
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO permission_groups(tenant_id,group_id,name,description) VALUES(?,?,?,?)
                ON CONFLICT(tenant_id,group_id) DO UPDATE SET name=excluded.name,description=excluded.description""",
                (tenant_id, group_id, name, description),
            )
        return self.get_group(tenant_id, group_id) or {}

    def get_group(self, tenant_id: str, group_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM permission_groups WHERE tenant_id=? AND group_id=?",
                (tenant_id, group_id),
            ).fetchone()
            if not row:
                return None
            rules = connection.execute(
                "SELECT rule_id FROM permission_rules WHERE tenant_id=? AND group_id=? ORDER BY rule_id",
                (tenant_id, group_id),
            ).fetchall()
            tools = connection.execute(
                """SELECT tool_name FROM group_tool_permissions
                   WHERE tenant_id=? AND group_id=? ORDER BY tool_name""",
                (tenant_id, group_id),
            ).fetchall()
        return {
            "id": row["group_id"],
            "name": row["name"],
            "description": row["description"],
            "rule_ids": [item[0] for item in rules],
            "tool_names": [item[0] for item in tools],
        }

    def set_group_tools(
        self, tenant_id: str, group_id: str, tool_names: list[str]
    ) -> dict[str, Any]:
        normalized = sorted(set(tool_names) - SYSTEM_DEFAULT_TOOL_NAMES)
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM group_tool_permissions WHERE tenant_id=? AND group_id=?",
                (tenant_id, group_id),
            )
            connection.executemany(
                """INSERT INTO group_tool_permissions(tenant_id,group_id,tool_name)
                   VALUES(?,?,?)""",
                [(tenant_id, group_id, tool_name) for tool_name in normalized],
            )
        return self.get_group(tenant_id, group_id) or {}

    def list_groups(self, tenant_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            ids = [
                row[0]
                for row in connection.execute(
                    "SELECT group_id FROM permission_groups WHERE tenant_id=? ORDER BY name,group_id",
                    (tenant_id,),
                ).fetchall()
            ]
        return [item for group_id in ids if (item := self.get_group(tenant_id, group_id))]

    def delete_group(self, tenant_id: str, group_id: str) -> bool:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM permission_rules WHERE tenant_id=? AND group_id=?",
                (tenant_id, group_id),
            )
            return (
                connection.execute(
                    "DELETE FROM permission_groups WHERE tenant_id=? AND group_id=?",
                    (tenant_id, group_id),
                ).rowcount
                > 0
            )

    def put_rule(
        self, tenant_id: str, rule_id: str, name: str, tool_names: list[str],
        description: str = "", group_id: str | None = None,
    ) -> dict[str, Any]:
        normalized = sorted(set(tool_names) - SYSTEM_DEFAULT_TOOL_NAMES)
        try:
            with self._connect() as connection:
                existing = connection.execute(
                    "SELECT group_id FROM permission_rules WHERE tenant_id=? AND rule_id=?",
                    (tenant_id, rule_id),
                ).fetchone()
                effective_group_id = group_id or (existing["group_id"] if existing else None)
                connection.execute(
                    """INSERT INTO permission_rules(tenant_id,rule_id,group_id,name,description,tool_names_json) VALUES(?,?,?,?,?,?)
                    ON CONFLICT(tenant_id,rule_id) DO UPDATE SET group_id=COALESCE(excluded.group_id,permission_rules.group_id),name=excluded.name,description=excluded.description,tool_names_json=excluded.tool_names_json""",
                    (tenant_id, rule_id, group_id, name, description, json.dumps(normalized)),
                )
                connection.execute(
                    "DELETE FROM permission_group_tools WHERE tenant_id=? AND rule_id=?",
                    (tenant_id, rule_id),
                )
                if effective_group_id:
                    connection.executemany(
                        """INSERT INTO permission_group_tools(
                           tenant_id,group_id,tool_name,rule_id) VALUES(?,?,?,?)""",
                        [
                            (tenant_id, effective_group_id, tool_name, rule_id)
                            for tool_name in normalized
                        ],
                    )
                    connection.executemany(
                        """INSERT OR IGNORE INTO group_tool_permissions(
                           tenant_id,group_id,tool_name) VALUES(?,?,?)""",
                        [
                            (tenant_id, effective_group_id, tool_name)
                            for tool_name in normalized
                        ],
                    )
        except sqlite3.IntegrityError as exc:
            raise ToolAssignmentConflict("该权限组内已有规则包含此 Tool") from exc
        return self.get_rule(tenant_id, rule_id) or {}

    def get_rule(self, tenant_id: str, rule_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM permission_rules WHERE tenant_id=? AND rule_id=?",
                (tenant_id, rule_id),
            ).fetchone()
        if not row:
            return None
        return {
            "id": row["rule_id"],
            "group_id": row["group_id"],
            "name": row["name"],
            "description": row["description"],
            "tool_names": json.loads(row["tool_names_json"]),
        }

    def list_rules(self, tenant_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            ids = [
                row[0]
                for row in connection.execute(
                    "SELECT rule_id FROM permission_rules WHERE tenant_id=? ORDER BY name,rule_id",
                    (tenant_id,),
                ).fetchall()
            ]
        return [item for rule_id in ids if (item := self.get_rule(tenant_id, rule_id))]

    def delete_rule(self, tenant_id: str, rule_id: str) -> bool:
        with self._connect() as connection:
            return (
                connection.execute(
                    "DELETE FROM permission_rules WHERE tenant_id=? AND rule_id=?",
                    (tenant_id, rule_id),
                ).rowcount
                > 0
            )

    def bind_user_group(self, tenant_id: str, user_id: str, group_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO user_permission_groups(tenant_id,user_id,group_id) VALUES(?,?,?)",
                (tenant_id, user_id, group_id),
            )

    def unbind_user_group(self, tenant_id: str, user_id: str, group_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM user_permission_groups WHERE tenant_id=? AND user_id=? AND group_id=?",
                (tenant_id, user_id, group_id),
            )

    def bind_group_rule(self, tenant_id: str, group_id: str, rule_id: str) -> None:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    """SELECT tool_names_json FROM permission_rules
                       WHERE tenant_id=? AND rule_id=?""",
                    (tenant_id, rule_id),
                ).fetchone()
                if not row:
                    return
                connection.execute(
                    "DELETE FROM permission_group_tools WHERE tenant_id=? AND rule_id=?",
                    (tenant_id, rule_id),
                )
                connection.execute(
                    "UPDATE permission_rules SET group_id=? WHERE tenant_id=? AND rule_id=?",
                    (group_id, tenant_id, rule_id),
                )
                connection.executemany(
                    """INSERT INTO permission_group_tools(
                       tenant_id,group_id,tool_name,rule_id) VALUES(?,?,?,?)""",
                    [
                        (tenant_id, group_id, tool_name, rule_id)
                        for tool_name in json.loads(row["tool_names_json"] or "[]")
                    ],
                )
                connection.executemany(
                    """INSERT OR IGNORE INTO group_tool_permissions(
                       tenant_id,group_id,tool_name) VALUES(?,?,?)""",
                    [
                        (tenant_id, group_id, tool_name)
                        for tool_name in json.loads(row["tool_names_json"] or "[]")
                    ],
                )
        except sqlite3.IntegrityError as exc:
            raise ToolAssignmentConflict("目标权限组内已有规则包含此 Tool") from exc

    def unbind_group_rule(self, tenant_id: str, group_id: str, rule_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM permission_group_tools WHERE tenant_id=? AND rule_id=?",
                (tenant_id, rule_id),
            )
            connection.execute(
                "UPDATE permission_rules SET group_id=NULL WHERE tenant_id=? AND group_id=? AND rule_id=?",
                (tenant_id, group_id, rule_id),
            )

    def effective_access(
        self, tenant_id: str, user_id: str, role: str | None = None
    ) -> EffectiveAccess:
        with self._connect() as connection:
            configured = (
                connection.execute(
                    "SELECT 1 FROM access_users WHERE tenant_id=? LIMIT 1", (tenant_id,)
                ).fetchone()
                is not None
            )
            if not configured:
                return EffectiveAccess(False, False, True, None)
            user = connection.execute(
                "SELECT enabled FROM access_users WHERE tenant_id=? AND user_id=?",
                (tenant_id, user_id),
            ).fetchone()
            if role == "admin":
                return EffectiveAccess(True, bool(user), True, None)
            if not user or not bool(user[0]):
                return EffectiveAccess(True, bool(user), False, frozenset())
            rows = connection.execute(
                """SELECT DISTINCT g.group_id,permissions.tool_name
                FROM user_permission_groups ug
                JOIN permission_groups g ON g.tenant_id=ug.tenant_id AND g.group_id=ug.group_id
                LEFT JOIN group_tool_permissions permissions
                  ON permissions.tenant_id=g.tenant_id AND permissions.group_id=g.group_id
                WHERE ug.tenant_id=? AND ug.user_id=?""",
                (tenant_id, user_id),
            ).fetchall()
        groups, rules, tools = set(), set(), set(SYSTEM_DEFAULT_TOOL_NAMES)
        for row in rows:
            groups.add(row["group_id"])
            if row["tool_name"]:
                rules.add(row["tool_name"])
                tools.add(row["tool_name"])
        return EffectiveAccess(
            True, True, True, frozenset(tools), tuple(sorted(groups)), tuple(sorted(rules))
        )

    def snapshot(self, tenant_id: str) -> dict[str, Any]:
        return {
            "configured": bool(self.list_users(tenant_id)),
            "users": self.list_users(tenant_id),
            "groups": self.list_groups(tenant_id),
            "rules": self.list_rules(tenant_id),
        }


class PostgresAccessControlStore:
    """Multi-replica RBAC store using the control-plane PostgreSQL database."""

    new_id = staticmethod(AccessControlStore.new_id)

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        statements = (
            """CREATE TABLE IF NOT EXISTS ops_access_users(
                tenant_id TEXT NOT NULL,user_id TEXT NOT NULL,name TEXT NOT NULL,
                enabled BOOLEAN NOT NULL DEFAULT TRUE,PRIMARY KEY(tenant_id,user_id))""",
            """CREATE TABLE IF NOT EXISTS ops_permission_groups(
                tenant_id TEXT NOT NULL,group_id TEXT NOT NULL,name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',PRIMARY KEY(tenant_id,group_id))""",
            """CREATE TABLE IF NOT EXISTS ops_permission_rules(
                tenant_id TEXT NOT NULL,rule_id TEXT NOT NULL,group_id TEXT,name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',tool_names_json JSONB NOT NULL DEFAULT '[]'::jsonb,
                PRIMARY KEY(tenant_id,rule_id),
                FOREIGN KEY(tenant_id,group_id) REFERENCES ops_permission_groups(tenant_id,group_id) ON DELETE CASCADE)""",
            """CREATE TABLE IF NOT EXISTS ops_user_permission_groups(
                tenant_id TEXT NOT NULL,user_id TEXT NOT NULL,group_id TEXT NOT NULL,
                PRIMARY KEY(tenant_id,user_id,group_id),
                FOREIGN KEY(tenant_id,user_id) REFERENCES ops_access_users(tenant_id,user_id) ON DELETE CASCADE,
                FOREIGN KEY(tenant_id,group_id) REFERENCES ops_permission_groups(tenant_id,group_id) ON DELETE CASCADE)""",
            """CREATE TABLE IF NOT EXISTS ops_permission_rule_tools(
                tenant_id TEXT NOT NULL,tool_name TEXT NOT NULL,rule_id TEXT NOT NULL,
                PRIMARY KEY(tenant_id,tool_name),
                FOREIGN KEY(tenant_id,rule_id) REFERENCES ops_permission_rules(tenant_id,rule_id) ON DELETE CASCADE)""",
            """CREATE TABLE IF NOT EXISTS ops_permission_group_tools(
                tenant_id TEXT NOT NULL,group_id TEXT NOT NULL,tool_name TEXT NOT NULL,rule_id TEXT NOT NULL,
                PRIMARY KEY(tenant_id,group_id,tool_name),
                FOREIGN KEY(tenant_id,group_id) REFERENCES ops_permission_groups(tenant_id,group_id) ON DELETE CASCADE,
                FOREIGN KEY(tenant_id,rule_id) REFERENCES ops_permission_rules(tenant_id,rule_id) ON DELETE CASCADE)""",
            """CREATE TABLE IF NOT EXISTS ops_group_tool_permissions(
                tenant_id TEXT NOT NULL,group_id TEXT NOT NULL,tool_name TEXT NOT NULL,
                PRIMARY KEY(tenant_id,group_id,tool_name),
                FOREIGN KEY(tenant_id,group_id) REFERENCES ops_permission_groups(tenant_id,group_id) ON DELETE CASCADE)""",
            """CREATE TABLE IF NOT EXISTS ops_group_permission_rules(
                tenant_id TEXT NOT NULL,group_id TEXT NOT NULL,rule_id TEXT NOT NULL,
                PRIMARY KEY(tenant_id,group_id,rule_id),
                FOREIGN KEY(tenant_id,group_id) REFERENCES ops_permission_groups(tenant_id,group_id) ON DELETE CASCADE,
                FOREIGN KEY(tenant_id,rule_id) REFERENCES ops_permission_rules(tenant_id,rule_id) ON DELETE CASCADE)""",
        )
        with self._connect() as connection:
            direct_group_tools_existed = connection.execute(
                "SELECT to_regclass('ops_group_tool_permissions') IS NOT NULL AS present"
            ).fetchone()["present"]
            for statement in statements:
                connection.execute(statement)
            connection.execute(
                "ALTER TABLE ops_permission_rules ADD COLUMN IF NOT EXISTS group_id TEXT"
            )
            connection.execute(
                """UPDATE ops_permission_rules rules SET group_id=legacy.group_id
                   FROM (SELECT tenant_id,rule_id,MIN(group_id) AS group_id
                         FROM ops_group_permission_rules GROUP BY tenant_id,rule_id) legacy
                   WHERE rules.tenant_id=legacy.tenant_id AND rules.rule_id=legacy.rule_id
                     AND rules.group_id IS NULL"""
            )
            connection.execute(
                """INSERT INTO ops_permission_rule_tools(tenant_id,tool_name,rule_id)
                   SELECT tenant_id,tool_name,rule_id FROM (
                     SELECT rules.tenant_id,tools.tool_name,rules.rule_id,
                            ROW_NUMBER() OVER(
                              PARTITION BY rules.tenant_id,tools.tool_name ORDER BY rules.rule_id
                            ) AS owner_order
                     FROM ops_permission_rules rules
                     CROSS JOIN LATERAL jsonb_array_elements_text(
                       rules.tool_names_json
                     ) AS tools(tool_name)
                   ) assignments WHERE owner_order=1 ON CONFLICT DO NOTHING"""
            )
            connection.execute(
                """DELETE FROM ops_permission_rules rules
                   WHERE jsonb_array_length(rules.tool_names_json)>0
                     AND NOT EXISTS(
                       SELECT 1 FROM jsonb_array_elements_text(rules.tool_names_json)
                         AS tools(tool_name)
                       WHERE NOT (tools.tool_name = ANY(%s)))""",
                (list(SYSTEM_DEFAULT_TOOL_NAMES),),
            )
            connection.execute(
                """UPDATE ops_permission_rules rules SET tool_names_json=COALESCE((
                     SELECT jsonb_agg(tool_name ORDER BY tool_name)
                     FROM jsonb_array_elements_text(rules.tool_names_json)
                       AS tools(tool_name)
                     WHERE NOT (tools.tool_name = ANY(%s))), '[]'::jsonb)""",
                (list(SYSTEM_DEFAULT_TOOL_NAMES),),
            )
            connection.execute(
                "DELETE FROM ops_permission_rule_tools WHERE tool_name = ANY(%s)",
                (list(SYSTEM_DEFAULT_TOOL_NAMES),),
            )
            connection.execute("DELETE FROM ops_permission_group_tools")
            connection.execute(
                """INSERT INTO ops_permission_group_tools(tenant_id,group_id,tool_name,rule_id)
                   SELECT tenant_id,group_id,tool_name,rule_id FROM (
                     SELECT rules.tenant_id,rules.group_id,tools.tool_name,rules.rule_id,
                            ROW_NUMBER() OVER(
                              PARTITION BY rules.tenant_id,rules.group_id,tools.tool_name
                              ORDER BY rules.rule_id
                            ) AS owner_order
                     FROM ops_permission_rules rules
                     CROSS JOIN LATERAL jsonb_array_elements_text(
                       rules.tool_names_json
                     ) AS tools(tool_name)
                     WHERE rules.group_id IS NOT NULL
                   ) assignments WHERE owner_order=1"""
            )
            connection.execute(
                """UPDATE ops_permission_rules rules SET tool_names_json=COALESCE((
                     SELECT jsonb_agg(assignments.tool_name ORDER BY assignments.tool_name)
                     FROM ops_permission_group_tools assignments
                     WHERE assignments.tenant_id=rules.tenant_id
                       AND assignments.rule_id=rules.rule_id), '[]'::jsonb)
                   WHERE rules.group_id IS NOT NULL"""
            )
            if not direct_group_tools_existed:
                connection.execute(
                    """INSERT INTO ops_group_tool_permissions(
                       tenant_id,group_id,tool_name)
                       SELECT tenant_id,group_id,tool_name
                       FROM ops_permission_group_tools ON CONFLICT DO NOTHING"""
                )

    def _connect(self):
        import psycopg
        from psycopg.rows import dict_row

        return psycopg.connect(self.dsn, row_factory=dict_row)

    def put_user(
        self, tenant_id: str, user_id: str, name: str, enabled: bool = True
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO ops_access_users(tenant_id,user_id,name,enabled) VALUES(%s,%s,%s,%s)
                ON CONFLICT(tenant_id,user_id) DO UPDATE SET name=EXCLUDED.name,enabled=EXCLUDED.enabled""",
                (tenant_id, user_id, name, enabled),
            )
        return self.get_user(tenant_id, user_id) or {}

    def get_user(self, tenant_id: str, user_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM ops_access_users WHERE tenant_id=%s AND user_id=%s",
                (tenant_id, user_id),
            ).fetchone()
            if not row:
                return None
            groups = connection.execute(
                "SELECT group_id FROM ops_user_permission_groups WHERE tenant_id=%s AND user_id=%s ORDER BY group_id",
                (tenant_id, user_id),
            ).fetchall()
        return {
            "id": row["user_id"],
            "name": row["name"],
            "enabled": row["enabled"],
            "group_ids": [item["group_id"] for item in groups],
        }

    def list_users(self, tenant_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT user_id FROM ops_access_users WHERE tenant_id=%s ORDER BY name,user_id",
                (tenant_id,),
            ).fetchall()
        return [self.get_user(tenant_id, row["user_id"]) for row in rows]

    def delete_user(self, tenant_id: str, user_id: str) -> bool:
        with self._connect() as connection:
            return (
                connection.execute(
                    "DELETE FROM ops_access_users WHERE tenant_id=%s AND user_id=%s",
                    (tenant_id, user_id),
                ).rowcount
                > 0
            )

    def put_group(
        self, tenant_id: str, group_id: str, name: str, description: str = ""
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO ops_permission_groups(tenant_id,group_id,name,description) VALUES(%s,%s,%s,%s)
                ON CONFLICT(tenant_id,group_id) DO UPDATE SET name=EXCLUDED.name,description=EXCLUDED.description""",
                (tenant_id, group_id, name, description),
            )
        return self.get_group(tenant_id, group_id) or {}

    def get_group(self, tenant_id: str, group_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM ops_permission_groups WHERE tenant_id=%s AND group_id=%s",
                (tenant_id, group_id),
            ).fetchone()
            if not row:
                return None
            rules = connection.execute(
                "SELECT rule_id FROM ops_permission_rules WHERE tenant_id=%s AND group_id=%s ORDER BY rule_id",
                (tenant_id, group_id),
            ).fetchall()
            tools = connection.execute(
                """SELECT tool_name FROM ops_group_tool_permissions
                   WHERE tenant_id=%s AND group_id=%s ORDER BY tool_name""",
                (tenant_id, group_id),
            ).fetchall()
        return {
            "id": row["group_id"],
            "name": row["name"],
            "description": row["description"],
            "rule_ids": [item["rule_id"] for item in rules],
            "tool_names": [item["tool_name"] for item in tools],
        }

    def set_group_tools(
        self, tenant_id: str, group_id: str, tool_names: list[str]
    ) -> dict[str, Any]:
        normalized = sorted(set(tool_names) - SYSTEM_DEFAULT_TOOL_NAMES)
        with self._connect() as connection:
            connection.execute(
                """DELETE FROM ops_group_tool_permissions
                   WHERE tenant_id=%s AND group_id=%s""",
                (tenant_id, group_id),
            )
            connection.cursor().executemany(
                """INSERT INTO ops_group_tool_permissions(tenant_id,group_id,tool_name)
                   VALUES(%s,%s,%s)""",
                [(tenant_id, group_id, tool_name) for tool_name in normalized],
            )
        return self.get_group(tenant_id, group_id) or {}

    def list_groups(self, tenant_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT group_id FROM ops_permission_groups WHERE tenant_id=%s ORDER BY name,group_id",
                (tenant_id,),
            ).fetchall()
        return [self.get_group(tenant_id, row["group_id"]) for row in rows]

    def delete_group(self, tenant_id: str, group_id: str) -> bool:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM ops_permission_rules WHERE tenant_id=%s AND group_id=%s",
                (tenant_id, group_id),
            )
            return (
                connection.execute(
                    "DELETE FROM ops_permission_groups WHERE tenant_id=%s AND group_id=%s",
                    (tenant_id, group_id),
                ).rowcount
                > 0
            )

    def put_rule(
        self,
        tenant_id: str,
        rule_id: str,
        name: str,
        tool_names: list[str],
        description: str = "",
        group_id: str | None = None,
    ) -> dict[str, Any]:
        from psycopg.types.json import Jsonb

        import psycopg

        try:
            with self._connect() as connection:
                existing = connection.execute(
                    """SELECT group_id FROM ops_permission_rules
                       WHERE tenant_id=%s AND rule_id=%s""",
                    (tenant_id, rule_id),
                ).fetchone()
                effective_group_id = group_id or (
                    existing["group_id"] if existing else None
                )
                connection.execute(
                    """INSERT INTO ops_permission_rules(tenant_id,rule_id,group_id,name,description,tool_names_json) VALUES(%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(tenant_id,rule_id) DO UPDATE SET group_id=COALESCE(EXCLUDED.group_id,ops_permission_rules.group_id),name=EXCLUDED.name,description=EXCLUDED.description,tool_names_json=EXCLUDED.tool_names_json""",
                    (
                        tenant_id, rule_id, group_id, name, description,
                        Jsonb(sorted(set(tool_names) - SYSTEM_DEFAULT_TOOL_NAMES)),
                    ),
                )
                connection.execute(
                    "DELETE FROM ops_permission_group_tools WHERE tenant_id=%s AND rule_id=%s",
                    (tenant_id, rule_id),
                )
                if effective_group_id:
                    connection.cursor().executemany(
                        """INSERT INTO ops_permission_group_tools(
                           tenant_id,group_id,tool_name,rule_id) VALUES(%s,%s,%s,%s)""",
                        [
                            (tenant_id, effective_group_id, tool_name, rule_id)
                            for tool_name in sorted(
                                set(tool_names) - SYSTEM_DEFAULT_TOOL_NAMES
                            )
                        ],
                    )
                    connection.cursor().executemany(
                        """INSERT INTO ops_group_tool_permissions(
                           tenant_id,group_id,tool_name) VALUES(%s,%s,%s)
                           ON CONFLICT DO NOTHING""",
                        [
                            (tenant_id, effective_group_id, tool_name)
                            for tool_name in sorted(
                                set(tool_names) - SYSTEM_DEFAULT_TOOL_NAMES
                            )
                        ],
                    )
        except psycopg.errors.UniqueViolation as exc:
            raise ToolAssignmentConflict("该权限组内已有规则包含此 Tool") from exc
        return self.get_rule(tenant_id, rule_id) or {}

    def get_rule(self, tenant_id: str, rule_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM ops_permission_rules WHERE tenant_id=%s AND rule_id=%s",
                (tenant_id, rule_id),
            ).fetchone()
        return (
            {
                "id": row["rule_id"],
                "group_id": row["group_id"],
                "name": row["name"],
                "description": row["description"],
                "tool_names": row["tool_names_json"],
            }
            if row
            else None
        )

    def list_rules(self, tenant_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT rule_id FROM ops_permission_rules WHERE tenant_id=%s ORDER BY name,rule_id",
                (tenant_id,),
            ).fetchall()
        return [self.get_rule(tenant_id, row["rule_id"]) for row in rows]

    def delete_rule(self, tenant_id: str, rule_id: str) -> bool:
        with self._connect() as connection:
            return (
                connection.execute(
                    "DELETE FROM ops_permission_rules WHERE tenant_id=%s AND rule_id=%s",
                    (tenant_id, rule_id),
                ).rowcount
                > 0
            )

    def bind_user_group(self, tenant_id: str, user_id: str, group_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO ops_user_permission_groups VALUES(%s,%s,%s) ON CONFLICT DO NOTHING",
                (tenant_id, user_id, group_id),
            )

    def unbind_user_group(self, tenant_id: str, user_id: str, group_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM ops_user_permission_groups WHERE tenant_id=%s AND user_id=%s AND group_id=%s",
                (tenant_id, user_id, group_id),
            )

    def bind_group_rule(self, tenant_id: str, group_id: str, rule_id: str) -> None:
        import psycopg

        try:
            with self._connect() as connection:
                row = connection.execute(
                    """SELECT tool_names_json FROM ops_permission_rules
                       WHERE tenant_id=%s AND rule_id=%s""",
                    (tenant_id, rule_id),
                ).fetchone()
                if not row:
                    return
                connection.execute(
                    "DELETE FROM ops_permission_group_tools WHERE tenant_id=%s AND rule_id=%s",
                    (tenant_id, rule_id),
                )
                connection.execute(
                    "UPDATE ops_permission_rules SET group_id=%s WHERE tenant_id=%s AND rule_id=%s",
                    (group_id, tenant_id, rule_id),
                )
                connection.cursor().executemany(
                    """INSERT INTO ops_permission_group_tools(
                       tenant_id,group_id,tool_name,rule_id) VALUES(%s,%s,%s,%s)""",
                    [
                        (tenant_id, group_id, tool_name, rule_id)
                        for tool_name in row["tool_names_json"]
                    ],
                )
                connection.cursor().executemany(
                    """INSERT INTO ops_group_tool_permissions(
                       tenant_id,group_id,tool_name) VALUES(%s,%s,%s)
                       ON CONFLICT DO NOTHING""",
                    [
                        (tenant_id, group_id, tool_name)
                        for tool_name in row["tool_names_json"]
                    ],
                )
        except psycopg.errors.UniqueViolation as exc:
            raise ToolAssignmentConflict("目标权限组内已有规则包含此 Tool") from exc

    def unbind_group_rule(self, tenant_id: str, group_id: str, rule_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM ops_permission_group_tools WHERE tenant_id=%s AND rule_id=%s",
                (tenant_id, rule_id),
            )
            connection.execute(
                "UPDATE ops_permission_rules SET group_id=NULL WHERE tenant_id=%s AND group_id=%s AND rule_id=%s",
                (tenant_id, group_id, rule_id),
            )

    def effective_access(
        self, tenant_id: str, user_id: str, role: str | None = None
    ) -> EffectiveAccess:
        with self._connect() as connection:
            configured = connection.execute(
                "SELECT 1 FROM ops_access_users WHERE tenant_id=%s LIMIT 1", (tenant_id,)
            ).fetchone()
            if not configured:
                return EffectiveAccess(False, False, True, None)
            user = connection.execute(
                "SELECT enabled FROM ops_access_users WHERE tenant_id=%s AND user_id=%s",
                (tenant_id, user_id),
            ).fetchone()
            if role == "admin":
                return EffectiveAccess(True, bool(user), True, None)
            if not user or not user["enabled"]:
                return EffectiveAccess(True, bool(user), False, frozenset())
            rows = connection.execute(
                """SELECT DISTINCT g.group_id,permissions.tool_name
                FROM ops_user_permission_groups ug
                JOIN ops_permission_groups g ON g.tenant_id=ug.tenant_id AND g.group_id=ug.group_id
                LEFT JOIN ops_group_tool_permissions permissions
                  ON permissions.tenant_id=g.tenant_id AND permissions.group_id=g.group_id
                WHERE ug.tenant_id=%s AND ug.user_id=%s""",
                (tenant_id, user_id),
            ).fetchall()
        groups, rules, tools = set(), set(), set(SYSTEM_DEFAULT_TOOL_NAMES)
        for row in rows:
            groups.add(row["group_id"])
            if row["tool_name"]:
                rules.add(row["tool_name"])
                tools.add(row["tool_name"])
        return EffectiveAccess(
            True, True, True, frozenset(tools), tuple(sorted(groups)), tuple(sorted(rules))
        )

    def snapshot(self, tenant_id: str) -> dict[str, Any]:
        return {
            "configured": bool(self.list_users(tenant_id)),
            "users": self.list_users(tenant_id),
            "groups": self.list_groups(tenant_id),
            "rules": self.list_rules(tenant_id),
        }


def create_access_control_store(
    settings: Settings,
) -> AccessControlStore | PostgresAccessControlStore:
    if settings.control_plane_backend == "postgres":
        return PostgresAccessControlStore(settings.postgres_dsn)
    return AccessControlStore(settings.platform_db_path)
