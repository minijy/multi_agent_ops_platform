import os
import uuid

import pytest
from fastapi.testclient import TestClient

from ops_agent.api.app import create_app
from ops_agent.config import Settings


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_TESTS") != "1",
    reason="set RUN_POSTGRES_TESTS=1 to run against the configured PostgreSQL database",
)


def _cleanup(dsn: str, tenant_id: str, session_ids: list[str]) -> None:
    import psycopg

    with psycopg.connect(dsn) as connection:
        for session_id in session_ids:
            connection.execute(
                "DELETE FROM ops_audit_events WHERE resource_id=%s", (session_id,)
            )
            connection.execute(
                "DELETE FROM agent_session_events WHERE session_id=%s", (session_id,)
            )
        connection.execute(
            "DELETE FROM ops_audit_events WHERE tenant_id=%s", (tenant_id,)
        )
        connection.execute(
            "DELETE FROM agent_session_events WHERE tenant_id=%s", (tenant_id,)
        )


def test_postgres_agent_session_persists_across_restart():
    settings = Settings(model_provider="mock")
    settings.validate_runtime()
    assert settings.control_plane_backend == "postgres"
    assert settings.session_event_backend == "postgres"

    tenant_id = f"pg-test-{uuid.uuid4()}"
    headers = {
        "X-Tenant-ID": tenant_id,
        "X-User-ID": "pg-test-user",
        "X-User-Role": "operator",
    }
    session_ids: list[str] = []
    try:
        with TestClient(create_app(settings)) as client:
            created = client.post(
                "/v1/agent/query",
                headers=headers,
                json={"question": "你好，请介绍当前 Runtime"},
            )
            assert created.status_code == 200
            session_id = created.json()["session_id"]
            session_ids.append(session_id)
            assert created.json()["event_count"] >= 5

            metrics = client.get("/v1/agent/metrics", headers=headers).json()
            assert metrics["turn_count"] >= 1

        with TestClient(create_app(settings)) as client:
            events = client.get(
                f"/v1/agent/sessions/{session_id}/events",
                headers=headers,
            )
            assert events.status_code == 200
            assert events.json()["count"] >= 5

            audit = client.get(
                "/v1/audit-events",
                headers={**headers, "X-User-Role": "admin"},
            ).json()
            assert audit["count"] >= 1
    finally:
        _cleanup(settings.postgres_dsn, tenant_id, session_ids)
