from __future__ import annotations

from pathlib import Path

import pytest

from ops_agent.connections import create_connection_registry
from ops_agent.runtime.connectors import ConnectorRuntime, create_tool_bindings
from ops_agent.runtime.domain import ToolCall
from ops_agent.runtime.tools import (
    ConnectorAccessGuard,
    ToolDefinition,
    ToolExecutionContext,
    ToolExecutor,
    ToolRegistry,
)
from pydantic import BaseModel


class FakeAnalyticsProvider:
    connector_type = "analytics"
    min_interval_seconds = 0.0

    def __init__(self) -> None:
        self.created = 0

    def create_client(self, values):
        self.created += 1
        return {"dsn": values["dsn"], "instance": self.created}


def _runtime(tmp_path: Path, **kwargs):
    registry = create_connection_registry(
        tmp_path / "connections.json", tmp_path / "secrets.json"
    )
    registry.upsert(
        tenant_id="tenant-a",
        connector_type="analytics",
        values={"dsn": "postgresql://first"},
        resource_scopes={"store_names": ["store-a"]},
    )
    provider = FakeAnalyticsProvider()
    return registry, provider, ConnectorRuntime(registry, [provider], **kwargs)


def test_connector_runtime_caches_client_and_refreshes_changed_credentials(tmp_path):
    registry, provider, runtime = _runtime(tmp_path)

    first = runtime.execute("tenant-a", "analytics", lambda client, _conn: client)
    second = runtime.execute("tenant-a", "analytics", lambda client, _conn: client)
    assert first is second
    assert provider.created == 1

    registry.upsert(
        tenant_id="tenant-a",
        connector_type="analytics",
        values={"dsn": "postgresql://second"},
    )
    refreshed = runtime.execute("tenant-a", "analytics", lambda client, _conn: client)
    assert refreshed["dsn"] == "postgresql://second"
    assert provider.created == 2


def test_connector_runtime_retries_transient_failure_and_reports_health(tmp_path):
    _registry, _provider, runtime = _runtime(tmp_path, max_retries=1)
    attempts = 0

    def flaky(client, _connection):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TimeoutError("temporary timeout")
        return client["instance"]

    assert runtime.execute("tenant-a", "analytics", flaky) == 1
    assert attempts == 2
    health = runtime.health_for_tenant("tenant-a")[0]
    assert health["state"] == "ready"
    assert health["failures"] == 0
    assert health["last_success_at"]
    assert health["last_error"] is None


def test_connector_runtime_opens_circuit_after_repeated_failures(tmp_path):
    _registry, _provider, runtime = _runtime(
        tmp_path,
        max_retries=0,
        failure_threshold=2,
        circuit_cooldown_seconds=60,
    )

    def unavailable(_client, _connection):
        raise TimeoutError("upstream unavailable")

    with pytest.raises(TimeoutError):
        runtime.execute("tenant-a", "analytics", unavailable)
    with pytest.raises(TimeoutError):
        runtime.execute("tenant-a", "analytics", unavailable)
    with pytest.raises(ConnectionError, match="circuit is open"):
        runtime.execute("tenant-a", "analytics", unavailable)
    assert runtime.health_for_tenant("tenant-a")[0]["state"] == "circuit_open"


def test_tool_binding_catalog_declares_connector_and_resource_scope():
    catalog = {item["tool_name"]: item for item in create_tool_bindings().catalog()}
    assert catalog["amazon_finance_query"] == {
        "tool_name": "amazon_finance_query",
        "connector_type": "analytics",
        "operation": "query_settlements",
        "resource_scope": None,
    }
    assert catalog["kingdee_cloud_query"]["connector_type"] == "kingdee"


def test_execution_scope_skips_optional_connector_without_connection(tmp_path):
    registry = create_connection_registry(
        tmp_path / "connections.json", tmp_path / "secrets.json"
    )
    bindings = create_tool_bindings(tmp_path / "bindings.json")

    connection_ids, scopes = bindings.execution_scope(
        "tenant-a", {"dingtalk_send_direct_message"}, registry
    )

    assert connection_ids == []
    assert scopes == {}


def test_tool_binding_selects_one_of_multiple_connector_instances(tmp_path):
    registry = create_connection_registry(
        tmp_path / "connections.json", tmp_path / "secrets.json"
    )
    first = registry.create(
        tenant_id="tenant-a",
        connector_type="analytics",
        name="first",
        values={"dsn": "postgresql://first"},
        resource_scopes={"store_names": ["store-a"]},
    )
    second = registry.create(
        tenant_id="tenant-a",
        connector_type="analytics",
        name="second",
        values={"dsn": "postgresql://second"},
        resource_scopes={"store_names": ["store-b"]},
    )
    bindings = create_tool_bindings(tmp_path / "bindings.json")
    bindings.select("tenant-a", "amazon_finance_query", second.id, registry)
    provider = FakeAnalyticsProvider()
    runtime = ConnectorRuntime(registry, [provider], bindings=bindings)

    selected = runtime.execute_tool(
        "tenant-a", "amazon_finance_query", lambda client, _connection: client
    )
    assert selected["dsn"] == "postgresql://second"
    assert first.id != second.id

    reloaded = create_tool_bindings(tmp_path / "bindings.json")
    assert reloaded.selected_connection_id(
        "tenant-a", "amazon_finance_query"
    ) == second.id


def test_connector_guard_rejects_connection_outside_delegated_snapshot(tmp_path):
    registry, _provider, _runtime_instance = _runtime(tmp_path)
    tools = ToolRegistry()

    class Arguments(BaseModel):
        metric: str = "overview"

    tools.register(
        ToolDefinition(
            name="amazon_finance_query",
            description="query",
            arguments_model=Arguments,
            handler=lambda _args, _context: {"ok": True},
        )
    )
    executor = ToolExecutor(
        tools,
        guards=[ConnectorAccessGuard(create_tool_bindings(), registry)],
    )
    result = executor.execute(
        ToolCall(
            call_id="call-1",
            name="amazon_finance_query",
            arguments={"metric": "overview"},
        ),
        ToolExecutionContext(
            session_id="child",
            tenant_id="tenant-a",
            user_id="user-a",
            connection_ids=(),
            connection_scope_enforced=True,
        ),
    )
    assert result.ok is False
    assert "outside delegated scope" in result.error


def test_connector_resource_scope_intersects_current_and_delegated_scope(tmp_path):
    registry, _provider, runtime = _runtime(tmp_path)
    connection = registry.require("tenant-a", "analytics")

    assert runtime.resolve_resource(
        connection,
        "store_names",
        "store-a",
        {"store_names": ("store-a", "store-b")},
    ) == "store-a"
    with pytest.raises(PermissionError, match="delegated scope"):
        runtime.resolve_resource(
            connection,
            "store_names",
            "store-b",
            {"store_names": ("store-a", "store-b")},
        )
