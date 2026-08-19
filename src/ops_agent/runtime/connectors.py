from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol, TypeVar

from pydantic import BaseModel, Field

from ..connections import (
    ConnectionDefinition,
    ConnectionRegistry,
    ConnectorType,
    normalize_analytics_database_type,
    validate_analytics_dsn,
)
from ..integrations.kingdee.client import KingdeeClient, KingdeeCredentials
from ..integrations.lingxing.client import LingXingClient
from ..integrations.dingtalk.client import DingTalkClient
from ..vector_connections import MilvusVectorClient, QdrantVectorClient
from ..workflows.kingdee_cloud.domain import KingdeeIntegrationConfig
from ..workflows.lingxing_profit.domain import LingXingIntegrationConfig


T = TypeVar("T")


@dataclass(frozen=True)
class ToolBinding:
    tool_name: str
    connector_type: ConnectorType
    operation: str
    resource_scope: str | None = None


class ToolConnectionBindingRequest(BaseModel):
    connection_id: str = Field(min_length=1, max_length=200)
    resource_scopes: dict[str, list[str]] = Field(default_factory=dict)


class ToolBindingRegistry:
    def __init__(self, path: Path | None = None) -> None:
        self._bindings: dict[str, ToolBinding] = {}
        self.path = path.expanduser().resolve() if path is not None else None
        self._lock = threading.RLock()
        self._selections: dict[str, str] = {}
        self._resource_scopes: dict[str, dict[str, list[str]]] = {}
        self.reload()

    @staticmethod
    def _selection_key(tenant_id: str, tool_name: str) -> str:
        return f"{tenant_id}:{tool_name}"

    def reload(self) -> None:
        if self.path is None or not self.path.is_file():
            self._selections = {}
            self._resource_scopes = {}
            return
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            loaded = {}
        if isinstance(loaded, dict) and "selections" in loaded:
            self._selections = {
                str(key): str(value)
                for key, value in (loaded.get("selections") or {}).items()
            }
            self._resource_scopes = {
                str(key): {
                    str(name): sorted({str(item) for item in values})
                    for name, values in scopes.items()
                    if isinstance(values, list)
                }
                for key, scopes in (loaded.get("resource_scopes") or {}).items()
                if isinstance(scopes, dict)
            }
        else:
            # Backward-compatible with the original {tenant:tool: connection_id} file.
            self._selections = (
                {str(key): str(value) for key, value in loaded.items()}
                if isinstance(loaded, dict)
                else {}
            )
            self._resource_scopes = {}

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {
                    "selections": self._selections,
                    "resource_scopes": self._resource_scopes,
                },
                ensure_ascii=False,
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )

    def register(self, binding: ToolBinding) -> None:
        if binding.tool_name in self._bindings:
            raise ValueError(f"tool binding already registered: {binding.tool_name}")
        self._bindings[binding.tool_name] = binding

    def get(self, tool_name: str) -> ToolBinding:
        try:
            return self._bindings[tool_name]
        except KeyError as exc:
            raise KeyError(f"tool has no connector binding: {tool_name}") from exc

    def find(self, tool_name: str) -> ToolBinding | None:
        return self._bindings.get(tool_name)

    def select(
        self,
        tenant_id: str,
        tool_name: str,
        connection_id: str,
        connections: ConnectionRegistry,
        resource_scopes: dict[str, list[str]] | None = None,
    ) -> ConnectionDefinition:
        binding = self.get(tool_name)
        connection = connections.require_id(
            connection_id, tenant_id, binding.connector_type
        )
        requested = resource_scopes or {}
        allowed_names = {binding.resource_scope} if binding.resource_scope else set()
        unknown_names = set(requested) - allowed_names
        if unknown_names:
            raise ValueError(
                f"tool does not support resource scopes: {sorted(unknown_names)}"
            )
        normalized: dict[str, list[str]] = {}
        if binding.resource_scope:
            maximum = {
                str(item)
                for item in connection.resource_scopes.get(binding.resource_scope, [])
            }
            values = {str(item) for item in requested.get(binding.resource_scope, [])}
            if not values:
                values = maximum
            if "*" not in maximum and ("*" in values or not values <= maximum):
                raise ValueError(
                    f"resource scope exceeds connection boundary: {binding.resource_scope}"
                )
            normalized[binding.resource_scope] = sorted(values)
        key = self._selection_key(tenant_id, tool_name)
        with self._lock:
            self._selections[key] = connection.id
            if normalized:
                self._resource_scopes[key] = normalized
            else:
                self._resource_scopes.pop(key, None)
            self._save()
        return connection

    def selected_connection_id(self, tenant_id: str, tool_name: str) -> str | None:
        return self._selections.get(self._selection_key(tenant_id, tool_name))

    def selected_resource_scopes(
        self, tenant_id: str, tool_name: str, connections: ConnectionRegistry
    ) -> dict[str, list[str]]:
        binding = self.get(tool_name)
        if not binding.resource_scope:
            return {}
        key = self._selection_key(tenant_id, tool_name)
        stored = self._resource_scopes.get(key)
        if stored is not None:
            return {name: list(values) for name, values in stored.items()}
        try:
            connection = self.resolve_connection(tenant_id, tool_name, connections)
        except (KeyError, PermissionError, ValueError):
            return {}
        return {
            binding.resource_scope: list(
                connection.resource_scopes.get(binding.resource_scope, [])
            )
        }

    def execution_scope(
        self,
        tenant_id: str,
        tool_names: set[str] | None,
        connections: ConnectionRegistry,
    ) -> tuple[list[str], dict[str, list[str]]]:
        selected_ids: set[str] = set()
        scopes: dict[str, set[str]] = {}
        for tool_name in self._bindings:
            if tool_names is not None and tool_name not in tool_names:
                continue
            try:
                connection = self.resolve_connection(tenant_id, tool_name, connections)
            except (KeyError, PermissionError, ValueError):
                continue
            selected_ids.add(connection.id)
            for name, values in self.selected_resource_scopes(
                tenant_id, tool_name, connections
            ).items():
                scopes.setdefault(name, set()).update(values)
        return sorted(selected_ids), {
            name: sorted(values) for name, values in scopes.items()
        }

    def resolve_connection(
        self,
        tenant_id: str,
        tool_name: str,
        connections: ConnectionRegistry,
    ) -> ConnectionDefinition:
        binding = self.get(tool_name)
        selected = self.selected_connection_id(tenant_id, tool_name)
        if selected:
            return connections.require_id(
                selected, tenant_id, binding.connector_type
            )
        return connections.require(tenant_id, binding.connector_type)

    def tools_for_connection(self, tenant_id: str, connection_id: str) -> list[str]:
        prefix = f"{tenant_id}:"
        return sorted(
            key[len(prefix):]
            for key, selected in self._selections.items()
            if key.startswith(prefix) and selected == connection_id
        )

    def catalog(
        self,
        tenant_id: str | None = None,
        connections: ConnectionRegistry | None = None,
    ) -> list[dict[str, Any]]:
        items = []
        for item in self._bindings.values():
            payload = {
                "tool_name": item.tool_name,
                "connector_type": item.connector_type,
                "operation": item.operation,
                "resource_scope": item.resource_scope,
            }
            if tenant_id is not None and connections is not None:
                selected = self.selected_connection_id(tenant_id, item.tool_name)
                candidates = [
                    connection
                    for connection in connections.list_for_tenant(tenant_id)
                    if connection.connector_type == item.connector_type
                ]
                payload["connection_id"] = selected or (
                    connections.get_default(tenant_id, item.connector_type).id
                    if connections.get_default(tenant_id, item.connector_type)
                    else None
                )
                payload["resource_scopes"] = self.selected_resource_scopes(
                    tenant_id, item.tool_name, connections
                )
                payload["connections"] = [
                    {
                        "id": connection.id,
                        "name": connection.name,
                        "enabled": connection.enabled,
                    }
                    for connection in candidates
                ]
            items.append(payload)
        return items


class ConnectorProvider(Protocol):
    connector_type: ConnectorType
    min_interval_seconds: float

    def create_client(self, values: dict[str, Any]) -> Any: ...


@dataclass(frozen=True)
class AnalyticsConnectorProvider:
    connector_type: ConnectorType = "analytics"
    min_interval_seconds: float = 0.0

    def create_client(self, values: dict[str, Any]) -> dict[str, str]:
        dsn = str(values.get("dsn") or "").strip()
        if not dsn:
            raise ValueError("analytics connection has no dsn")
        database_type = normalize_analytics_database_type(
            values.get("database_type")
        )
        validate_analytics_dsn(dsn, database_type)
        return {"dsn": dsn, "engine": database_type}


@dataclass(frozen=True)
class LingXingConnectorProvider:
    timeout_seconds: float = 30.0
    connector_type: ConnectorType = "lingxing"
    min_interval_seconds: float = 0.05

    def create_client(self, values: dict[str, Any]) -> LingXingClient:
        config = LingXingIntegrationConfig.model_validate(values)
        return LingXingClient(
            config.app_id,
            config.app_secret,
            base_url=config.base_url,
            timeout_seconds=self.timeout_seconds,
        )


@dataclass(frozen=True)
class KingdeeConnectorProvider:
    timeout_seconds: float = 45.0
    connector_type: ConnectorType = "kingdee"
    min_interval_seconds: float = 0.05

    def create_client(self, values: dict[str, Any]) -> KingdeeClient:
        config = KingdeeIntegrationConfig.model_validate(values)
        return KingdeeClient(
            KingdeeCredentials(
                server_url=config.server_url.strip(),
                acct_id=config.acct_id.strip(),
                app_id=config.app_id.strip(),
                app_secret=config.app_secret.strip(),
                username=config.username.strip(),
                lcid=config.lcid,
            ),
            timeout_seconds=self.timeout_seconds,
        )


@dataclass(frozen=True)
class DingTalkConnectorProvider:
    timeout_seconds: float = 20.0
    connector_type: ConnectorType = "dingtalk"
    min_interval_seconds: float = 0.05

    def create_client(self, values: dict[str, Any]) -> DingTalkClient:
        return DingTalkClient(
            str(values.get("app_key") or ""),
            str(values.get("app_secret") or ""),
            str(values.get("robot_code") or values.get("app_key") or ""),
            base_url=str(values.get("base_url") or "https://api.dingtalk.com"),
            timeout_seconds=self.timeout_seconds,
        )


@dataclass(frozen=True)
class QdrantConnectorProvider:
    timeout_seconds: float = 10.0
    connector_type: ConnectorType = "qdrant"
    min_interval_seconds: float = 0.0

    def create_client(self, values: dict[str, Any]) -> QdrantVectorClient:
        return QdrantVectorClient(
            str(values.get("url") or ""),
            str(values.get("api_key") or ""),
            self.timeout_seconds,
        )


@dataclass(frozen=True)
class MilvusConnectorProvider:
    timeout_seconds: float = 10.0
    connector_type: ConnectorType = "milvus"
    min_interval_seconds: float = 0.0

    def create_client(self, values: dict[str, Any]) -> MilvusVectorClient:
        return MilvusVectorClient(
            str(values.get("uri") or ""),
            str(values.get("token") or ""),
            str(values.get("db_name") or "default"),
            self.timeout_seconds,
        )


@dataclass
class _ConnectionState:
    failures: int = 0
    circuit_open_until: float = 0.0
    last_error: str | None = None
    last_success_at: str | None = None
    last_failure_at: str | None = None
    next_allowed_at: float = 0.0
    lock: threading.RLock = field(default_factory=threading.RLock)


class ConnectorRuntime:
    """Connection-scoped clients, throttling, retries, circuit breaking and health."""

    def __init__(
        self,
        connections: ConnectionRegistry,
        providers: list[ConnectorProvider],
        *,
        bindings: ToolBindingRegistry | None = None,
        max_retries: int = 1,
        failure_threshold: int = 3,
        circuit_cooldown_seconds: float = 30.0,
    ) -> None:
        self.connections = connections
        self.bindings = bindings
        self.providers = {item.connector_type: item for item in providers}
        self.max_retries = max_retries
        self.failure_threshold = failure_threshold
        self.circuit_cooldown_seconds = circuit_cooldown_seconds
        self._clients: dict[str, tuple[str, Any]] = {}
        self._states: dict[str, _ConnectionState] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _fingerprint(values: dict[str, Any]) -> str:
        encoded = json.dumps(
            values, sort_keys=True, ensure_ascii=False, default=str
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _state(self, connection_id: str) -> _ConnectionState:
        with self._lock:
            return self._states.setdefault(connection_id, _ConnectionState())

    def connection(
        self, tenant_id: str, connector_type: ConnectorType
    ) -> ConnectionDefinition:
        return self.connections.require(tenant_id, connector_type)

    def connection_for_tool(
        self, tenant_id: str, tool_name: str
    ) -> ConnectionDefinition:
        if self.bindings is None:
            binding_error = "connector runtime has no tool binding registry"
            raise RuntimeError(binding_error)
        return self.bindings.resolve_connection(
            tenant_id, tool_name, self.connections
        )

    def scoped_resources(
        self,
        connection: ConnectionDefinition,
        scope_name: str,
        delegated_scope: dict[str, tuple[str, ...]],
    ) -> list[str]:
        current = set(connection.resource_scopes.get(scope_name, []))
        delegated_raw = delegated_scope.get(scope_name)
        if delegated_raw is None:
            return sorted(current)
        delegated = set(delegated_raw)
        if "*" in current:
            effective = delegated
        elif "*" in delegated:
            effective = current
        else:
            effective = current & delegated
        return sorted(effective)

    def scoped_tool_resources(
        self,
        connection: ConnectionDefinition,
        tool_name: str,
        scope_name: str,
        delegated_scope: dict[str, tuple[str, ...]],
    ) -> list[str]:
        current = set(connection.resource_scopes.get(scope_name, []))
        if self.bindings is not None:
            bound = set(
                self.bindings.selected_resource_scopes(
                    connection.tenant_id, tool_name, self.connections
                ).get(scope_name, [])
            )
            if "*" not in current:
                current &= bound
            else:
                current = bound
        delegated = set(delegated_scope.get(scope_name, current))
        if "*" in current:
            effective = delegated
        elif "*" in delegated:
            effective = current
        else:
            effective = current & delegated
        return sorted(effective)

    def resolve_tool_resource(
        self,
        connection: ConnectionDefinition,
        tool_name: str,
        scope_name: str,
        requested: str | None,
        delegated_scope: dict[str, tuple[str, ...]],
    ) -> str | None:
        allowed = self.scoped_tool_resources(
            connection, tool_name, scope_name, delegated_scope
        )
        if "*" in allowed:
            return requested
        if requested:
            if requested not in allowed:
                raise PermissionError(
                    f"resource is not authorized for tool binding: "
                    f"{scope_name}={requested}"
                )
            return requested
        if len(allowed) == 1:
            return allowed[0]
        if allowed:
            raise PermissionError(f"resource must be specified: {scope_name}")
        raise PermissionError(
            f"tool binding has no authorized resources: {scope_name}"
        )

    def resolve_resource(
        self,
        connection: ConnectionDefinition,
        scope_name: str,
        requested: str | None,
        delegated_scope: dict[str, tuple[str, ...]],
    ) -> str | None:
        allowed = self.scoped_resources(
            connection, scope_name, delegated_scope
        )
        if "*" in allowed:
            return requested
        if requested:
            if requested not in allowed:
                raise PermissionError(
                    f"resource is not authorized for delegated scope: "
                    f"{scope_name}={requested}"
                )
            return requested
        if len(allowed) == 1:
            return allowed[0]
        if allowed:
            raise PermissionError(f"resource must be specified: {scope_name}")
        raise PermissionError(
            f"delegated scope has no authorized resources: {scope_name}"
        )

    def _client(
        self,
        provider: ConnectorProvider,
        connection: ConnectionDefinition,
    ) -> Any:
        values = self.connections.resolved_values(connection)
        fingerprint = self._fingerprint(values)
        cached = self._clients.get(connection.id)
        if cached is not None and cached[0] == fingerprint:
            return cached[1]
        client = provider.create_client(values)
        self._clients[connection.id] = (fingerprint, client)
        return client

    @staticmethod
    def _transient(exc: Exception) -> bool:
        current: BaseException | None = exc
        fragments = (
            "network",
            "timeout",
            "timed out",
            "http 429",
            "http 500",
            "http 502",
            "http 503",
            "http 504",
            "\u7f51\u7edc",
        )
        while current is not None:
            text = str(current).lower()
            if isinstance(current, (TimeoutError, ConnectionError, OSError)):
                return True
            if any(fragment in text for fragment in fragments):
                return True
            current = current.__cause__
        return False

    def execute(
        self,
        tenant_id: str,
        connector_type: ConnectorType,
        operation: Callable[[Any, ConnectionDefinition], T],
    ) -> T:
        connection = self.connection(tenant_id, connector_type)
        return self._execute_connection(connection, operation)

    def _execute_connection(
        self,
        connection: ConnectionDefinition,
        operation: Callable[[Any, ConnectionDefinition], T],
        *,
        retry_transient: bool = True,
    ) -> T:
        provider = self.providers[connection.connector_type]
        state = self._state(connection.id)
        with state.lock:
            now = time.monotonic()
            if state.circuit_open_until > now:
                remaining = state.circuit_open_until - now
                raise ConnectionError(
                    f"connector circuit is open for {remaining:.1f}s: {connection.id}"
                )
            delay = state.next_allowed_at - now
            if delay > 0:
                time.sleep(delay)
            state.next_allowed_at = time.monotonic() + provider.min_interval_seconds
            client = self._client(provider, connection)
            for attempt in range(self.max_retries + 1):
                try:
                    result = operation(client, connection)
                    state.failures = 0
                    state.circuit_open_until = 0.0
                    state.last_error = None
                    state.last_success_at = self._now()
                    return result
                except Exception as exc:
                    state.failures += 1
                    state.last_error = f"{type(exc).__name__}: {exc}"
                    state.last_failure_at = self._now()
                    if state.failures >= self.failure_threshold:
                        state.circuit_open_until = (
                            time.monotonic() + self.circuit_cooldown_seconds
                        )
                    if (
                        not retry_transient
                        or attempt >= self.max_retries
                        or not self._transient(exc)
                    ):
                        raise
                    time.sleep(min(0.1 * (2**attempt), 1.0))
            raise RuntimeError("unreachable connector execution state")

    def execute_tool(
        self,
        tenant_id: str,
        tool_name: str,
        operation: Callable[[Any, ConnectionDefinition], T],
        *,
        retry_transient: bool = True,
    ) -> T:
        connection = self.connection_for_tool(tenant_id, tool_name)
        return self._execute_connection(
            connection, operation, retry_transient=retry_transient
        )

    def execute_connection(
        self,
        tenant_id: str,
        connection_id: str,
        operation: Callable[[Any, ConnectionDefinition], T],
        *,
        retry_transient: bool = True,
    ) -> T:
        connection = self.connections.require_id(connection_id, tenant_id)
        return self._execute_connection(
            connection, operation, retry_transient=retry_transient
        )

    def invalidate(self, connection_id: str) -> None:
        with self._lock:
            self._clients.pop(connection_id, None)
            self._states.pop(connection_id, None)

    def health_for_tenant(self, tenant_id: str) -> list[dict[str, Any]]:
        now = time.monotonic()
        items: list[dict[str, Any]] = []
        for connection in self.connections.list_for_tenant(tenant_id):
            state = self._states.get(connection.id)
            ready = self.connections.is_ready(connection)
            circuit_open = bool(state and state.circuit_open_until > now)
            items.append(
                {
                    "connection_id": connection.id,
                    "connector_type": connection.connector_type,
                    "state": (
                        "circuit_open"
                        if circuit_open
                        else "ready" if ready else "misconfigured"
                    ),
                    "failures": state.failures if state else 0,
                    "last_success_at": state.last_success_at if state else None,
                    "last_failure_at": state.last_failure_at if state else None,
                    "last_error": state.last_error if state else None,
                }
            )
        return items


def create_connector_runtime(
    connections: ConnectionRegistry,
    bindings: ToolBindingRegistry | None = None,
) -> ConnectorRuntime:
    return ConnectorRuntime(
        connections,
        [
            AnalyticsConnectorProvider(),
            LingXingConnectorProvider(),
            KingdeeConnectorProvider(),
            DingTalkConnectorProvider(),
            QdrantConnectorProvider(),
            MilvusConnectorProvider(),
        ],
        bindings=bindings,
    )


def create_tool_bindings(path: Path | None = None) -> ToolBindingRegistry:
    registry = ToolBindingRegistry(path)
    for binding in (
        ToolBinding("amazon_finance_query", "analytics", "query_settlements"),
        ToolBinding(
            "profit_report_query", "analytics", "query_profit", "store_names"
        ),
        ToolBinding(
            "lingxing_profit_query", "lingxing", "profit_report", "sids"
        ),
        ToolBinding("kingdee_cloud_query", "kingdee", "execute_bill_query"),
        ToolBinding(
            "dingtalk_send_direct_message",
            "dingtalk",
            "send_direct_message",
            "dingtalk_user_ids",
        ),
        ToolBinding(
            "dingtalk_send_group_message",
            "dingtalk",
            "send_group_message",
            "dingtalk_conversation_ids",
        ),
        ToolBinding(
            "dingtalk_create_todo",
            "dingtalk",
            "create_todo",
            "dingtalk_union_ids",
        ),
    ):
        registry.register(binding)
    return registry
