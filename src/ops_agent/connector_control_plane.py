from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .connections import ConnectionDefinition, LocalSecretStore, SECRET_MASK
from .runtime.connectors import ToolBinding


HYBRID_AGENT_TO_TOOL = {
    "amazon-finance-query": "amazon_finance_query",
    "lingxing-profit-report": "lingxing_profit_query",
    "profit-report-query": "profit_report_query",
    "kingdee-cloud": "kingdee_cloud_query",
}
TOOL_TO_HYBRID = {tool_name: agent_id for agent_id, tool_name in HYBRID_AGENT_TO_TOOL.items()}

DEFAULT_TOOL_BINDINGS = (
    ToolBinding("amazon_finance_query", "analytics", "query_settlements"),
    ToolBinding("profit_report_query", "analytics", "query_profit", "store_names"),
    ToolBinding("lingxing_profit_query", "lingxing", "profit_report", "sids"),
    ToolBinding("kingdee_cloud_query", "kingdee", "execute_bill_query"),
    ToolBinding("dingtalk_send_direct_message", "dingtalk", "send_direct_message", "dingtalk_user_ids"),
    ToolBinding("dingtalk_send_group_message", "dingtalk", "send_group_message", "dingtalk_conversation_ids"),
    ToolBinding("dingtalk_create_todo", "dingtalk", "create_todo", "dingtalk_union_ids"),
    ToolBinding("web_search", "tavily", "search"),
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ToolCapability:
    tool_name: str
    display_name: str = ""
    description: str = ""
    connector_type: str | None = None
    operation: str = ""
    resource_scope: str | None = None
    enabled: bool = True
    system_prompt: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "display_name": self.display_name,
            "description": self.description,
            "connector_type": self.connector_type,
            "operation": self.operation,
            "resource_scope": self.resource_scope,
            "enabled": self.enabled,
            "system_prompt": self.system_prompt,
        }


def default_tool_capabilities() -> list[ToolCapability]:
    from .workflows.amazon_finance.agent import SYSTEM_PROMPT as AMAZON_PROMPT
    from .workflows.kingdee_cloud.agent import SYSTEM_PROMPT as KINGDEE_PROMPT
    from .workflows.lingxing_profit.agent import SYSTEM_PROMPT as LINGXING_PROMPT
    from .workflows.profit_report.agent import SYSTEM_PROMPT as PROFIT_PROMPT

    prompts = {
        "amazon_finance_query": AMAZON_PROMPT,
        "lingxing_profit_query": LINGXING_PROMPT,
        "profit_report_query": PROFIT_PROMPT,
        "kingdee_cloud_query": KINGDEE_PROMPT,
    }
    names = {
        "amazon_finance_query": "Amazon 结算查询",
        "lingxing_profit_query": "领星利润报表",
        "profit_report_query": "利润报表数据库",
        "kingdee_cloud_query": "金蝶云星空查询",
        "dingtalk_send_direct_message": "钉钉单聊",
        "dingtalk_send_group_message": "钉钉群聊",
        "dingtalk_create_todo": "钉钉待办",
        "web_search": "网页搜索",
    }
    enabled = {"kingdee_cloud_query": False}
    items: list[ToolCapability] = []
    for binding in DEFAULT_TOOL_BINDINGS:
        items.append(
            ToolCapability(
                tool_name=binding.tool_name,
                display_name=names.get(binding.tool_name, binding.tool_name),
                connector_type=binding.connector_type,
                operation=binding.operation,
                resource_scope=binding.resource_scope,
                enabled=enabled.get(binding.tool_name, True),
                system_prompt=prompts.get(binding.tool_name, ""),
            )
        )
    return items


class ToolCatalog:
    """In-memory tool capability catalog; Postgres overlay is optional."""

    def __init__(self, items: list[ToolCapability] | None = None) -> None:
        self._items = {item.tool_name: item for item in (items or default_tool_capabilities())}

    def get(self, tool_name: str) -> ToolCapability | None:
        return self._items.get(tool_name)

    def list(self) -> list[ToolCapability]:
        return list(self._items.values())

    def is_enabled(self, tool_name: str) -> bool:
        item = self._items.get(tool_name)
        return True if item is None else item.enabled

    def prompt(self, tool_name: str) -> str:
        item = self._items.get(tool_name)
        return "" if item is None else item.system_prompt

    def upsert(self, item: ToolCapability) -> ToolCapability:
        self._items[item.tool_name] = item
        return item


def capability_for_query_tool(
    catalog: ToolCatalog | None,
    registry: Any | None,
    tool_name: str,
) -> ToolCapability:
    if catalog is not None:
        item = catalog.get(tool_name)
        if item is not None:
            return item
    defaults = {item.tool_name: item for item in default_tool_capabilities()}
    spec = defaults.get(tool_name) or ToolCapability(tool_name=tool_name)
    agent_id = TOOL_TO_HYBRID.get(tool_name)
    agent = registry.get(agent_id) if registry is not None and agent_id else None
    if agent is None:
        return spec
    return ToolCapability(
        tool_name=spec.tool_name,
        display_name=str(getattr(agent, "name", "") or spec.display_name),
        description=str(getattr(agent, "description", "") or spec.description),
        connector_type=spec.connector_type,
        operation=spec.operation,
        resource_scope=spec.resource_scope,
        enabled=bool(getattr(agent, "enabled", spec.enabled)),
        system_prompt=str(getattr(agent, "system_prompt", "") or spec.system_prompt),
    )


def _pg_connect(dsn: str):
    import psycopg
    from psycopg.rows import dict_row

    return psycopg.connect(dsn, row_factory=dict_row)


class PostgresSecretStore:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self._lock = threading.RLock()

    def put(self, secret_ref: str, values: dict[str, str]) -> None:
        from psycopg.types.json import Jsonb

        with self._lock, _pg_connect(self.dsn) as connection:
            row = connection.execute(
                "SELECT payload_json FROM ops_connection_secrets WHERE secret_ref=%s",
                (secret_ref,),
            ).fetchone()
            current = dict(row["payload_json"] or {}) if row else {}
            merged = {
                **current,
                **{key: value for key, value in values.items() if value != SECRET_MASK},
            }
            connection.execute(
                """INSERT INTO ops_connection_secrets(secret_ref,payload_json,updated_at)
                   VALUES(%s,%s,NOW())
                   ON CONFLICT(secret_ref) DO UPDATE SET
                     payload_json=EXCLUDED.payload_json, updated_at=NOW()""",
                (secret_ref, Jsonb(merged)),
            )
            connection.commit()

    def get(self, secret_ref: str) -> dict[str, str]:
        with self._lock, _pg_connect(self.dsn) as connection:
            row = connection.execute(
                "SELECT payload_json FROM ops_connection_secrets WHERE secret_ref=%s",
                (secret_ref,),
            ).fetchone()
        payload = dict(row["payload_json"] or {}) if row else {}
        return {str(key): str(value) for key, value in payload.items()}

    def delete(self, secret_ref: str) -> None:
        with self._lock, _pg_connect(self.dsn) as connection:
            connection.execute(
                "DELETE FROM ops_connection_secrets WHERE secret_ref=%s",
                (secret_ref,),
            )
            connection.commit()


class PostgresConnectionPersistence:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def load(self) -> list[ConnectionDefinition]:
        with _pg_connect(self.dsn) as connection:
            rows = connection.execute(
                """SELECT id,tenant_id,connector_type,name,enabled,config_json,secret_ref,
                          resource_scopes_json
                   FROM ops_connections ORDER BY tenant_id, connector_type, name"""
            ).fetchall()
        items: list[ConnectionDefinition] = []
        for row in rows:
            items.append(
                ConnectionDefinition(
                    id=row["id"],
                    tenant_id=row["tenant_id"],
                    connector_type=row["connector_type"],
                    name=row["name"],
                    enabled=bool(row["enabled"]),
                    config=dict(row["config_json"] or {}),
                    secret_ref=row["secret_ref"],
                    resource_scopes={
                        str(name): [str(value) for value in values]
                        for name, values in dict(row["resource_scopes_json"] or {}).items()
                    },
                )
            )
        return items

    def save(self, items: list[ConnectionDefinition]) -> None:
        from psycopg.types.json import Jsonb

        with _pg_connect(self.dsn) as connection:
            keep_ids = [item.id for item in items]
            if keep_ids:
                connection.execute(
                    "DELETE FROM ops_connections WHERE NOT (id = ANY(%s))",
                    (keep_ids,),
                )
            else:
                connection.execute("DELETE FROM ops_connections")
            for item in items:
                connection.execute(
                    """INSERT INTO ops_connections(
                         id,tenant_id,connector_type,name,enabled,config_json,secret_ref,
                         resource_scopes_json,updated_at
                       ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                       ON CONFLICT(id) DO UPDATE SET
                         tenant_id=EXCLUDED.tenant_id,
                         connector_type=EXCLUDED.connector_type,
                         name=EXCLUDED.name,
                         enabled=EXCLUDED.enabled,
                         config_json=EXCLUDED.config_json,
                         secret_ref=EXCLUDED.secret_ref,
                         resource_scopes_json=EXCLUDED.resource_scopes_json,
                         updated_at=NOW()""",
                    (
                        item.id,
                        item.tenant_id,
                        item.connector_type,
                        item.name,
                        item.enabled,
                        Jsonb(item.config),
                        item.secret_ref,
                        Jsonb(item.resource_scopes),
                    ),
                )
            connection.commit()

    def count(self) -> int:
        with _pg_connect(self.dsn) as connection:
            row = connection.execute("SELECT COUNT(*) AS n FROM ops_connections").fetchone()
        return int(row["n"] if row else 0)


class PostgresBindingPersistence:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def load(self) -> tuple[dict[str, str], dict[str, dict[str, list[str]]]]:
        with _pg_connect(self.dsn) as connection:
            rows = connection.execute(
                """SELECT tenant_id,tool_name,connection_id,resource_scopes_json
                   FROM ops_tool_bindings"""
            ).fetchall()
        selections: dict[str, str] = {}
        scopes: dict[str, dict[str, list[str]]] = {}
        for row in rows:
            key = f"{row['tenant_id']}:{row['tool_name']}"
            selections[key] = str(row["connection_id"])
            payload = dict(row["resource_scopes_json"] or {})
            if payload:
                scopes[key] = {
                    str(name): [str(value) for value in values]
                    for name, values in payload.items()
                    if isinstance(values, list)
                }
        return selections, scopes

    def save(
        self,
        selections: dict[str, str],
        resource_scopes: dict[str, dict[str, list[str]]],
    ) -> None:
        from psycopg.types.json import Jsonb

        with _pg_connect(self.dsn) as connection:
            connection.execute("DELETE FROM ops_tool_bindings")
            for key, connection_id in selections.items():
                tenant_id, _, tool_name = key.partition(":")
                if not tenant_id or not tool_name:
                    continue
                connection.execute(
                    """INSERT INTO ops_tool_bindings(
                         tenant_id,tool_name,connection_id,resource_scopes_json,updated_at
                       ) VALUES(%s,%s,%s,%s,NOW())""",
                    (
                        tenant_id,
                        tool_name,
                        connection_id,
                        Jsonb(resource_scopes.get(key) or {}),
                    ),
                )
            connection.commit()

    def count(self) -> int:
        with _pg_connect(self.dsn) as connection:
            row = connection.execute("SELECT COUNT(*) AS n FROM ops_tool_bindings").fetchone()
        return int(row["n"] if row else 0)


class PostgresToolCatalog(ToolCatalog):
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        super().__init__([])
        self.reload()

    def reload(self) -> None:
        with _pg_connect(self.dsn) as connection:
            rows = connection.execute(
                """SELECT tool_name,display_name,description,connector_type,operation,
                          resource_scope,enabled,system_prompt
                   FROM ops_tools ORDER BY tool_name"""
            ).fetchall()
        self._items = {
            row["tool_name"]: ToolCapability(
                tool_name=row["tool_name"],
                display_name=row["display_name"] or "",
                description=row["description"] or "",
                connector_type=row["connector_type"],
                operation=row["operation"] or "",
                resource_scope=row["resource_scope"],
                enabled=bool(row["enabled"]),
                system_prompt=row["system_prompt"] or "",
            )
            for row in rows
        }

    def upsert(self, item: ToolCapability) -> ToolCapability:
        from psycopg.types.json import Jsonb  # noqa: F401

        with _pg_connect(self.dsn) as connection:
            connection.execute(
                """INSERT INTO ops_tools(
                     tool_name,display_name,description,connector_type,operation,
                     resource_scope,enabled,system_prompt,updated_at
                   ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                   ON CONFLICT(tool_name) DO UPDATE SET
                     display_name=EXCLUDED.display_name,
                     description=EXCLUDED.description,
                     connector_type=EXCLUDED.connector_type,
                     operation=EXCLUDED.operation,
                     resource_scope=EXCLUDED.resource_scope,
                     enabled=EXCLUDED.enabled,
                     system_prompt=EXCLUDED.system_prompt,
                     updated_at=NOW()""",
                (
                    item.tool_name,
                    item.display_name,
                    item.description,
                    item.connector_type,
                    item.operation,
                    item.resource_scope,
                    item.enabled,
                    item.system_prompt,
                ),
            )
            connection.commit()
        self._items[item.tool_name] = item
        return item

    def count(self) -> int:
        with _pg_connect(self.dsn) as connection:
            row = connection.execute("SELECT COUNT(*) AS n FROM ops_tools").fetchone()
        return int(row["n"] if row else 0)


def import_json_connections(
    persistence: PostgresConnectionPersistence,
    secrets: PostgresSecretStore,
    definitions_path: Path,
    secrets_path: Path,
) -> int:
    if persistence.count() > 0:
        return 0
    if not definitions_path.is_file():
        return 0
    try:
        loaded = json.loads(definitions_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return 0
    items = []
    for raw in loaded if isinstance(loaded, list) else []:
        if not isinstance(raw, dict):
            continue
        items.append(ConnectionDefinition.model_validate(raw))
    if not items:
        return 0
    local_secrets = LocalSecretStore(secrets_path)
    persistence.save(items)
    for item in items:
        payload = local_secrets.get(item.secret_ref)
        if payload:
            secrets.put(item.secret_ref, payload)
    return len(items)


def import_json_bindings(
    persistence: PostgresBindingPersistence,
    bindings_path: Path,
) -> int:
    if persistence.count() > 0 or not bindings_path.is_file():
        return 0
    try:
        loaded = json.loads(bindings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return 0
    if isinstance(loaded, dict) and "selections" in loaded:
        selections = {str(key): str(value) for key, value in (loaded.get("selections") or {}).items()}
        resource_scopes = {
            str(key): {
                str(name): sorted({str(item) for item in values})
                for name, values in scopes.items()
                if isinstance(values, list)
            }
            for key, scopes in (loaded.get("resource_scopes") or {}).items()
            if isinstance(scopes, dict)
        }
    elif isinstance(loaded, dict):
        selections = {str(key): str(value) for key, value in loaded.items()}
        resource_scopes = {}
    else:
        return 0
    if not selections:
        return 0
    persistence.save(selections, resource_scopes)
    return len(selections)


def seed_default_bindings(
    persistence: PostgresBindingPersistence,
    connections: list[ConnectionDefinition],
) -> int:
    if persistence.count() > 0 or not connections:
        return 0
    defaults: dict[tuple[str, str], ConnectionDefinition] = {}
    for item in connections:
        key = (item.tenant_id, item.connector_type)
        defaults.setdefault(key, item)
    selections: dict[str, str] = {}
    scopes: dict[str, dict[str, list[str]]] = {}
    for binding in DEFAULT_TOOL_BINDINGS:
        for (tenant_id, connector_type), connection in defaults.items():
            if connector_type != binding.connector_type:
                continue
            key = f"{tenant_id}:{binding.tool_name}"
            selections[key] = connection.id
            allowed = list(connection.resource_scopes.get(binding.resource_scope or "", []))
            if binding.resource_scope and allowed:
                scopes[key] = {binding.resource_scope: allowed}
    if not selections:
        return 0
    persistence.save(selections, scopes)
    return len(selections)


def seed_tool_catalog(
    catalog: PostgresToolCatalog,
    agent_rows: list[dict[str, Any]] | None = None,
) -> int:
    defaults = {item.tool_name: item for item in default_tool_capabilities()}
    hybrid_by_id = {
        row.get("id") or row.get("agent_id"): row
        for row in (agent_rows or [])
        if (row.get("id") or row.get("agent_id")) in HYBRID_AGENT_TO_TOOL
    }
    if catalog.count() == 0:
        for tool_name, spec in defaults.items():
            agent_id = next(
                (key for key, value in HYBRID_AGENT_TO_TOOL.items() if value == tool_name),
                None,
            )
            row = hybrid_by_id.get(agent_id) if agent_id else None
            if row is not None:
                spec = ToolCapability(
                    tool_name=spec.tool_name,
                    display_name=str(row.get("name") or spec.display_name),
                    description=str(row.get("description") or spec.description),
                    connector_type=spec.connector_type,
                    operation=spec.operation,
                    resource_scope=spec.resource_scope,
                    enabled=bool(row.get("enabled", spec.enabled)),
                    system_prompt=str(row.get("system_prompt") or spec.system_prompt),
                )
            catalog.upsert(spec)
        return len(defaults)
    # Keep existing rows, only fill missing tools.
    created = 0
    for spec in defaults.values():
        if catalog.get(spec.tool_name) is None:
            catalog.upsert(spec)
            created += 1
    return created


def delete_hybrid_agents(store: Any) -> int:
    rows = store.list_agents()
    remaining = [
        row
        for row in rows
        if str(row.get("kind") or "") != "hybrid"
        and str(row.get("id") or "") not in HYBRID_AGENT_TO_TOOL
    ]
    removed = len(rows) - len(remaining)
    if removed:
        store.replace_agents(remaining)
    return removed
