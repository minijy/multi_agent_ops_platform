from __future__ import annotations

import json
import stat

import pytest
from pydantic import ValidationError

from ops_agent.connections import create_connection_registry
from ops_agent.runtime.subagents import DelegateSubagentArguments
from ops_agent.workflows.kingdee_cloud.domain import KingdeeQueryPlan


def test_connection_separates_secrets_and_enforces_tenant_resources(tmp_path):
    definitions = tmp_path / "connections.json"
    secret_path = tmp_path / "secrets.json"
    registry = create_connection_registry(definitions, secret_path)
    connection = registry.upsert(
        tenant_id="tenant-a",
        connector_type="analytics",
        values={"dsn": "postgresql://secret@db/analytics"},
        resource_scopes={"store_names": ["store-a"]},
    )

    assert "postgresql://secret" not in definitions.read_text(encoding="utf-8")
    assert registry.resolved_values(connection)["dsn"].startswith("postgresql://")
    assert stat.S_IMODE(secret_path.stat().st_mode) == 0o600
    assert registry.resolve_resource(connection, "store_names", None) == "store-a"
    with pytest.raises(PermissionError, match="not authorized"):
        registry.resolve_resource(connection, "store_names", "store-b")
    with pytest.raises(PermissionError, match="no enabled analytics"):
        registry.require("tenant-b", "analytics")


def test_connection_update_preserves_masked_secret(tmp_path):
    registry = create_connection_registry(
        tmp_path / "connections.json", tmp_path / "secrets.json"
    )
    registry.upsert(
        tenant_id="tenant-a",
        connector_type="lingxing",
        values={"app_id": "old", "app_secret": "secret"},
    )
    updated = registry.upsert(
        tenant_id="tenant-a",
        connector_type="lingxing",
        values={"app_id": "new", "app_secret": "********"},
    )
    assert registry.resolved_values(updated) == {
        "app_id": "new",
        "app_secret": "secret",
    }


def test_mysql_connection_is_ready_and_keeps_dsn_secret(tmp_path):
    definitions = tmp_path / "connections.json"
    registry = create_connection_registry(definitions, tmp_path / "secrets.json")
    connection = registry.create(
        tenant_id="tenant-a",
        connector_type="analytics",
        name="MySQL warehouse",
        values={
            "database_type": "mysql",
            "dsn": "mysql://reader:secret@mysql.example.com:3306/analytics",
        },
    )

    assert connection.config["database_type"] == "mysql"
    assert registry.is_ready(connection) is True
    assert registry.masked_values(connection)["dsn"] == "********"
    assert "reader:secret@" not in definitions.read_text(encoding="utf-8")


def test_analytics_database_type_must_match_dsn(tmp_path):
    registry = create_connection_registry(
        tmp_path / "connections.json", tmp_path / "secrets.json"
    )
    with pytest.raises(ValueError, match="mysql://"):
        registry.create(
            tenant_id="tenant-a",
            connector_type="analytics",
            name="Invalid MySQL warehouse",
            values={
                "database_type": "mysql",
                "dsn": "postgresql://reader:secret@db/analytics",
            },
        )


def test_dingtalk_connection_masks_secret_and_preserves_target_scopes(tmp_path):
    definitions = tmp_path / "connections.json"
    registry = create_connection_registry(definitions, tmp_path / "secrets.json")
    connection = registry.create(
        tenant_id="tenant-a",
        connector_type="dingtalk",
        name="DingTalk",
        values={
            "app_key": "app-key",
            "app_secret": "app-secret",
            "robot_code": "robot-code",
        },
        resource_scopes={
            "dingtalk_user_ids": ["user-1"],
            "dingtalk_conversation_ids": ["cid-1"],
            "dingtalk_union_ids": ["union-1"],
        },
    )

    assert registry.is_ready(connection)
    assert registry.masked_values(connection)["app_secret"] == "********"
    assert connection.config["base_url"] == "https://api.dingtalk.com"
    assert connection.resource_scopes["dingtalk_user_ids"] == ["user-1"]
    assert "app-secret" not in definitions.read_text(encoding="utf-8")


def test_vector_connections_mask_credentials_and_validate_endpoints(tmp_path):
    definitions = tmp_path / "connections.json"
    registry = create_connection_registry(definitions, tmp_path / "secrets.json")
    qdrant = registry.create(
        tenant_id="tenant-a",
        connector_type="qdrant",
        name="Qdrant",
        values={"url": "https://qdrant.example.com/", "api_key": "q-secret"},
    )
    milvus = registry.create(
        tenant_id="tenant-a",
        connector_type="milvus",
        name="Milvus",
        values={
            "uri": "http://milvus.internal:19530/",
            "token": "root:secret",
            "db_name": "knowledge",
        },
    )

    assert registry.is_ready(qdrant)
    assert registry.is_ready(milvus)
    assert qdrant.config["url"] == "https://qdrant.example.com"
    assert milvus.config["db_name"] == "knowledge"
    assert registry.masked_values(qdrant)["api_key"] == "********"
    assert registry.masked_values(milvus)["token"] == "********"
    persisted = definitions.read_text(encoding="utf-8")
    assert "q-secret" not in persisted
    assert "root:secret" not in persisted


def test_tavily_connection_masks_api_key(tmp_path):
    definitions = tmp_path / "connections.json"
    registry = create_connection_registry(definitions, tmp_path / "secrets.json")
    connection = registry.create(
        tenant_id="tenant-a",
        connector_type="tavily",
        name="Tavily",
        values={"api_key": "tvly-secret"},
    )

    assert registry.is_ready(connection)
    assert connection.config["base_url"] == "https://api.tavily.com"
    assert registry.masked_values(connection)["api_key"] == "********"
    assert registry.masked_values(connection)["api_key_configured"] is True
    assert "tvly-secret" not in definitions.read_text(encoding="utf-8")

    with pytest.raises(ValueError, match="api.tavily.com"):
        registry.create(
            tenant_id="tenant-a",
            connector_type="tavily",
            name="Bad Tavily",
            values={"api_key": "tvly-secret", "base_url": "https://evil.example"},
        )

    with pytest.raises(ValueError, match="http://"):
        registry.create(
            tenant_id="tenant-a",
            connector_type="qdrant",
            name="Invalid",
            values={"url": "file:///tmp/qdrant"},
        )


def test_legacy_seller_scope_is_removed_from_persisted_connections(tmp_path):
    definitions = tmp_path / "connections.json"
    secrets = tmp_path / "secrets.json"
    registry = create_connection_registry(definitions, secrets)
    created = registry.upsert(
        tenant_id="tenant-a",
        connector_type="analytics",
        values={"dsn": "postgresql://analytics"},
        resource_scopes={
            "seller_ids": ["legacy-seller"],
            "store_names": ["store-a"],
        },
    )
    assert created.resource_scopes == {"store_names": ["store-a"]}
    assert "seller_ids" not in definitions.read_text(encoding="utf-8")


def test_long_subagent_must_run_in_background():
    with pytest.raises(ValidationError, match="run_in_background=true"):
        DelegateSubagentArguments(
            objective="long task", timeout_seconds=171, run_in_background=False
        )
    accepted = DelegateSubagentArguments(
        objective="long task", timeout_seconds=900, run_in_background=True
    )
    assert accepted.timeout_seconds == 900


def test_kingdee_rejects_untyped_filter_input():
    payload = {
        "document_type": "sale_order",
        "start_date": "2026-01-01",
        "end_date": "2026-01-31",
        "extra_filter": "1=1 or FBillNo <> ''",
    }
    with pytest.raises(ValidationError, match="extra_filter"):
        KingdeeQueryPlan.model_validate(payload)
    assert "extra_filter" not in json.dumps(KingdeeQueryPlan.model_json_schema())
