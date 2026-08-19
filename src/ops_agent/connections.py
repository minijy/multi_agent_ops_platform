from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from .integrations.tavily.client import validate_tavily_base_url


ConnectorType = Literal[
    "analytics", "lingxing", "kingdee", "dingtalk", "qdrant", "milvus", "tavily"
]
AnalyticsDatabaseType = Literal["postgresql", "mysql"]
SECRET_MASK = "********"


def normalize_analytics_database_type(value: Any) -> AnalyticsDatabaseType:
    normalized = str(value or "postgresql").strip().lower()
    aliases = {"postgres": "postgresql", "pg": "postgresql"}
    normalized = aliases.get(normalized, normalized)
    if normalized not in {"postgresql", "mysql"}:
        raise ValueError(f"unsupported analytics database type: {normalized}")
    return normalized  # type: ignore[return-value]


def validate_analytics_dsn(dsn: str, database_type: AnalyticsDatabaseType) -> None:
    scheme = urlparse(str(dsn or "").strip()).scheme.lower()
    allowed = (
        {"mysql", "mysql+pymysql"}
        if database_type == "mysql"
        else {"postgres", "postgresql", "postgresql+psycopg"}
    )
    if scheme not in allowed:
        expected = "mysql://" if database_type == "mysql" else "postgresql://"
        raise ValueError(f"{database_type} DSN 必须以 {expected} 开头")


def validate_dingtalk_base_url(value: Any) -> str:
    normalized = str(value or "https://api.dingtalk.com").strip().rstrip("/")
    parsed = urlparse(normalized)
    if parsed.scheme != "https" or parsed.hostname != "api.dingtalk.com":
        raise ValueError("钉钉 API Base URL 必须是 https://api.dingtalk.com")
    return normalized


def validate_vector_endpoint(value: Any, label: str) -> str:
    normalized = str(value or "").strip().rstrip("/")
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"{label} 必须是有效的 http:// 或 https:// 地址")
    return normalized


class ConnectionDefinition(BaseModel):
    id: str = Field(min_length=1, max_length=160)
    tenant_id: str = Field(min_length=1, max_length=128)
    connector_type: ConnectorType
    name: str = Field(default="Default", max_length=120)
    enabled: bool = True
    config: dict[str, Any] = Field(default_factory=dict)
    secret_ref: str = Field(min_length=1, max_length=200)
    resource_scopes: dict[str, list[str]] = Field(default_factory=dict)


class ConnectionUpsertRequest(BaseModel):
    config: dict[str, Any] = Field(default_factory=dict)
    credentials: dict[str, str] = Field(default_factory=dict)
    resource_scopes: dict[str, list[str]] = Field(default_factory=dict)


class ConnectionCreateRequest(ConnectionUpsertRequest):
    connector_type: ConnectorType
    name: str = Field(min_length=1, max_length=120)
    enabled: bool = True


class ConnectionUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    enabled: bool | None = None
    config: dict[str, Any] | None = None
    credentials: dict[str, str] | None = None
    resource_scopes: dict[str, list[str]] | None = None


class LocalSecretStore:
    """Small local-development secret store kept separate from public config.

    Production deployments should replace this implementation with Vault/KMS while
    preserving the secret-ref interface.
    """

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        self._lock = threading.RLock()

    def _read(self) -> dict[str, dict[str, str]]:
        if not self.path.is_file():
            return {}
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return {}
        return loaded if isinstance(loaded, dict) else {}

    def put(self, secret_ref: str, values: dict[str, str]) -> None:
        with self._lock:
            payload = self._read()
            current = payload.get(secret_ref, {})
            payload[secret_ref] = {
                **current,
                **{key: value for key, value in values.items() if value != SECRET_MASK},
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.chmod(self.path, 0o600)

    def get(self, secret_ref: str) -> dict[str, str]:
        with self._lock:
            return dict(self._read().get(secret_ref, {}))

    def delete(self, secret_ref: str) -> None:
        with self._lock:
            payload = self._read()
            if secret_ref not in payload:
                return
            payload.pop(secret_ref, None)
            self.path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.chmod(self.path, 0o600)


class ConnectionRegistry:
    SECRET_FIELDS: dict[ConnectorType, frozenset[str]] = {
        "analytics": frozenset({"dsn"}),
        "lingxing": frozenset({"app_secret"}),
        "kingdee": frozenset({"app_secret"}),
        "dingtalk": frozenset({"app_secret"}),
        "qdrant": frozenset({"api_key"}),
        "milvus": frozenset({"token"}),
        "tavily": frozenset({"api_key"}),
    }

    def __init__(self, path: Path, secrets: LocalSecretStore) -> None:
        self.path = path.expanduser().resolve()
        self.secrets = secrets
        self._lock = threading.RLock()
        self._connections: dict[str, ConnectionDefinition] = {}
        self.reload()

    @staticmethod
    def default_id(tenant_id: str, connector_type: ConnectorType) -> str:
        return f"{tenant_id}:{connector_type}:default"

    def reload(self) -> None:
        with self._lock:
            if not self.path.is_file():
                self._connections = {}
                return
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, TypeError):
                loaded = []
            items = loaded if isinstance(loaded, list) else []
            self._connections = {
                item.id: item
                for raw in items
                if isinstance(raw, dict)
                for item in [
                    ConnectionDefinition.model_validate(raw).model_copy(
                        update={
                            "resource_scopes": self._normalize_scopes(
                                raw.get("resource_scopes")
                            )
                        }
                    )
                ]
            }
            if any(
                "seller_ids" in dict(raw.get("resource_scopes") or {})
                for raw in items
                if isinstance(raw, dict)
            ):
                self._save()

    @staticmethod
    def _normalize_scopes(
        scopes: dict[str, list[str]] | None,
    ) -> dict[str, list[str]]:
        return {
            str(name): [str(value) for value in values]
            for name, values in dict(scopes or {}).items()
            if name != "seller_ids"
        }

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                [item.model_dump(mode="json") for item in self._connections.values()],
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def upsert(
        self,
        *,
        tenant_id: str,
        connector_type: ConnectorType,
        values: dict[str, Any],
        resource_scopes: dict[str, list[str]] | None = None,
        connection_id: str | None = None,
        name: str | None = None,
        enabled: bool | None = None,
    ) -> ConnectionDefinition:
        connection_id = connection_id or self.default_id(tenant_id, connector_type)
        secret_ref = f"connection/{connection_id}"
        secret_fields = self.SECRET_FIELDS[connector_type]
        secrets = {
            key: str(value)
            for key, value in values.items()
            if key in secret_fields and value not in {None, "", SECRET_MASK}
        }
        config = {
            key: value
            for key, value in values.items()
            if key not in secret_fields and key not in {"connection_id", "resource_scopes"}
        }
        with self._lock:
            current = self._connections.get(connection_id)
            if connector_type == "analytics":
                database_type = normalize_analytics_database_type(
                    values.get("database_type")
                    or (current.config.get("database_type") if current else None)
                )
                current_secrets = (
                    self.secrets.get(current.secret_ref) if current else {}
                )
                resolved_dsn = str(secrets.get("dsn") or current_secrets.get("dsn") or "")
                if resolved_dsn:
                    validate_analytics_dsn(resolved_dsn, database_type)
                config["database_type"] = database_type
            elif connector_type == "dingtalk":
                config["base_url"] = validate_dingtalk_base_url(
                    values.get("base_url")
                    or (current.config.get("base_url") if current else None)
                )
            elif connector_type == "qdrant":
                config["url"] = validate_vector_endpoint(
                    values.get("url")
                    or (current.config.get("url") if current else None),
                    "Qdrant URL",
                )
            elif connector_type == "milvus":
                config["uri"] = validate_vector_endpoint(
                    values.get("uri")
                    or (current.config.get("uri") if current else None),
                    "Milvus URI",
                )
                config["db_name"] = str(
                    values.get("db_name")
                    or (current.config.get("db_name") if current else "default")
                    or "default"
                ).strip()
            elif connector_type == "tavily":
                config["base_url"] = validate_tavily_base_url(
                    values.get("base_url")
                    or (current.config.get("base_url") if current else None)
                )
            merged_config = {**(current.config if current else {}), **config}
            merged_scopes = self._normalize_scopes(
                resource_scopes
                if resource_scopes is not None
                else (current.resource_scopes if current else {})
            )
            connection = ConnectionDefinition(
                id=connection_id,
                tenant_id=tenant_id,
                connector_type=connector_type,
                name=name or (current.name if current else "Default"),
                enabled=(
                    enabled
                    if enabled is not None
                    else (current.enabled if current else True)
                ),
                config=merged_config,
                secret_ref=secret_ref,
                resource_scopes=merged_scopes,
            )
            if secrets:
                self.secrets.put(secret_ref, secrets)
            self._connections[connection_id] = connection
            self._save()
            return connection

    def create(
        self,
        *,
        tenant_id: str,
        connector_type: ConnectorType,
        name: str,
        values: dict[str, Any],
        resource_scopes: dict[str, list[str]] | None = None,
        enabled: bool = True,
    ) -> ConnectionDefinition:
        connection_id = f"{tenant_id}:{connector_type}:{uuid.uuid4().hex}"
        return self.upsert(
            tenant_id=tenant_id,
            connector_type=connector_type,
            values=values,
            resource_scopes=resource_scopes,
            connection_id=connection_id,
            name=name,
            enabled=enabled,
        )

    def get(self, connection_id: str, tenant_id: str) -> ConnectionDefinition | None:
        item = self._connections.get(connection_id)
        return item if item is not None and item.tenant_id == tenant_id else None

    def require_id(
        self,
        connection_id: str,
        tenant_id: str,
        connector_type: ConnectorType | None = None,
    ) -> ConnectionDefinition:
        item = self.get(connection_id, tenant_id)
        if item is None:
            raise PermissionError(f"connection is not visible: {connection_id}")
        if connector_type is not None and item.connector_type != connector_type:
            raise ValueError(
                f"connection type mismatch: expected {connector_type}, "
                f"got {item.connector_type}"
            )
        if not item.enabled or not self._ready(item):
            raise PermissionError(f"connection is disabled or incomplete: {connection_id}")
        return item

    def update(
        self,
        connection_id: str,
        tenant_id: str,
        request: ConnectionUpdateRequest,
    ) -> ConnectionDefinition:
        current = self.get(connection_id, tenant_id)
        if current is None:
            raise KeyError("connection not found")
        values = {
            **(request.config or {}),
            **(request.credentials or {}),
        }
        return self.upsert(
            tenant_id=tenant_id,
            connector_type=current.connector_type,
            values=values,
            resource_scopes=request.resource_scopes,
            connection_id=current.id,
            name=request.name,
            enabled=request.enabled,
        )

    def delete(self, connection_id: str, tenant_id: str) -> ConnectionDefinition:
        with self._lock:
            item = self.get(connection_id, tenant_id)
            if item is None:
                raise KeyError("connection not found")
            self._connections.pop(connection_id, None)
            self._save()
            self.secrets.delete(item.secret_ref)
            return item

    def get_default(
        self, tenant_id: str, connector_type: ConnectorType
    ) -> ConnectionDefinition | None:
        item = self._connections.get(self.default_id(tenant_id, connector_type))
        if item is not None and item.enabled and self._ready(item):
            return item
        return next(
            (
                candidate
                for candidate in self.list_for_tenant(tenant_id)
                if candidate.connector_type == connector_type
                and candidate.enabled
                and self._ready(candidate)
            ),
            None,
        )

    def _ready(self, connection: ConnectionDefinition) -> bool:
        values = self.resolved_values(connection)
        required: dict[ConnectorType, tuple[str, ...]] = {
            "analytics": ("dsn",),
            "lingxing": ("app_id", "app_secret"),
            "kingdee": (
                "server_url",
                "acct_id",
                "app_id",
                "app_secret",
                "username",
            ),
            "dingtalk": ("app_key", "app_secret", "robot_code"),
            "qdrant": ("url",),
            "milvus": ("uri", "db_name"),
            "tavily": ("api_key",),
        }
        ready = all(
            str(values.get(key) or "").strip()
            for key in required[connection.connector_type]
        )
        if ready and connection.connector_type == "analytics":
            try:
                database_type = normalize_analytics_database_type(
                    values.get("database_type")
                )
                validate_analytics_dsn(str(values.get("dsn") or ""), database_type)
            except ValueError:
                return False
        if ready and connection.connector_type == "dingtalk":
            try:
                validate_dingtalk_base_url(values.get("base_url"))
            except ValueError:
                return False
        if ready and connection.connector_type in {"qdrant", "milvus"}:
            try:
                field = "url" if connection.connector_type == "qdrant" else "uri"
                validate_vector_endpoint(
                    values.get(field),
                    "Qdrant URL" if field == "url" else "Milvus URI",
                )
            except ValueError:
                return False
        if ready and connection.connector_type == "tavily":
            try:
                validate_tavily_base_url(values.get("base_url"))
            except ValueError:
                return False
        return ready

    def is_ready(self, connection: ConnectionDefinition) -> bool:
        return connection.enabled and self._ready(connection)

    def require(
        self, tenant_id: str, connector_type: ConnectorType
    ) -> ConnectionDefinition:
        item = self.get_default(tenant_id, connector_type)
        if item is None:
            raise PermissionError(
                f"tenant has no enabled {connector_type} connection: {tenant_id}"
            )
        return item

    def resolved_values(self, connection: ConnectionDefinition) -> dict[str, Any]:
        return {**connection.config, **self.secrets.get(connection.secret_ref)}

    def masked_values(self, connection: ConnectionDefinition) -> dict[str, Any]:
        secrets = self.secrets.get(connection.secret_ref)
        result = {**connection.config, "connection_id": connection.id}
        for field in self.SECRET_FIELDS[connection.connector_type]:
            result[field] = SECRET_MASK if secrets.get(field) else ""
            result[f"{field}_configured"] = bool(secrets.get(field))
        result["resource_scopes"] = connection.resource_scopes
        return result

    def configured(self, connector_type: ConnectorType) -> bool:
        return any(
            item.connector_type == connector_type and item.enabled and self._ready(item)
            for item in self._connections.values()
        )

    def list_for_tenant(self, tenant_id: str) -> list[ConnectionDefinition]:
        return sorted(
            (
                item
                for item in self._connections.values()
                if item.tenant_id == tenant_id
            ),
            key=lambda item: (item.connector_type, item.id),
        )

    def resolve_resource(
        self,
        connection: ConnectionDefinition,
        scope_name: str,
        requested: str | None,
    ) -> str | None:
        allowed = connection.resource_scopes.get(scope_name, [])
        if "*" in allowed:
            return requested
        if requested:
            if requested not in allowed:
                raise PermissionError(
                    f"resource is not authorized for tenant: {scope_name}={requested}"
                )
            return requested
        if len(allowed) == 1:
            return allowed[0]
        if allowed:
            raise PermissionError(f"resource must be specified: {scope_name}")
        raise PermissionError(f"connection has no authorized resources: {scope_name}")


def create_connection_registry(
    definitions_path: Path, secrets_path: Path
) -> ConnectionRegistry:
    return ConnectionRegistry(definitions_path, LocalSecretStore(secrets_path))
