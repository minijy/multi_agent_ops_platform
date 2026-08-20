from pathlib import Path

from fastapi.testclient import TestClient

from ops_agent.api.app import create_app
from ops_agent.config import Settings


def _settings(tmp_path: Path, **overrides) -> Settings:
    return Settings(
        _env_file=None,
        platform_db_path=tmp_path / "platform.sqlite3",
        session_event_path=tmp_path / "events.sqlite3",
        runtime_governance_path=tmp_path / "governance.sqlite3",
        runtime_metrics_path=tmp_path / "metrics.sqlite3",
        memory_db_path=tmp_path / "memory.sqlite3",
        agent_definitions_path=tmp_path / "agents.json",
        model_definitions_path=tmp_path / "models.json",
        connection_definitions_path=tmp_path / "connections.json",
        connection_secrets_path=tmp_path / "connection-secrets.json",
        tool_bindings_path=tmp_path / "tool-bindings.json",
        attachment_path=tmp_path / "attachments",
        skills_paths=str(tmp_path / "skills"),
        mcp_config_path=tmp_path / "mcp.json",
        **overrides,
    )


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_registration_login_refresh_and_header_bypass_protection(tmp_path: Path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        registered = client.post(
            "/v1/auth/register",
            json={
                "tenant_id": "tenant-a", "user_id": "owner",
                "display_name": "Owner", "password": "StrongPass123",
            },
        )
        assert registered.status_code == 201
        body = registered.json()
        assert body["account"]["role"] == "admin"
        assert client.get("/v1/auth/me", headers=_bearer(body["access_token"])).status_code == 200

        bypass = client.get(
            "/v1/dashboard/summary",
            headers={"X-Tenant-ID": "tenant-a", "X-User-ID": "fake", "X-User-Role": "admin"},
        )
        assert bypass.status_code == 401
        assert bypass.json()["detail"]["code"] == "login_required"

        joining_existing = client.post(
            "/v1/auth/register",
            json={
                "tenant_id": "tenant-a", "user_id": "intruder",
                "display_name": "Intruder", "password": "StrongPass123",
            },
        )
        assert joining_existing.status_code == 403
        assert joining_existing.json()["detail"]["code"] == "registration_closed"

        refreshed = client.post(
            "/v1/auth/refresh", json={"refresh_token": body["refresh_token"]}
        )
        assert refreshed.status_code == 200
        reused = client.post(
            "/v1/auth/refresh", json={"refresh_token": body["refresh_token"]}
        )
        assert reused.status_code == 401


def test_admin_temporary_password_requires_change_and_reset(tmp_path: Path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        owner = client.post(
            "/v1/auth/register",
            json={
                "tenant_id": "tenant-a", "user_id": "owner",
                "display_name": "Owner", "password": "StrongPass123",
            },
        ).json()
        created = client.put(
            "/v1/access-control/users/alice",
            headers=_bearer(owner["access_token"]),
            json={
                "id": "alice", "name": "Alice", "role": "operator", "enabled": True,
                "generate_temporary_password": True,
            },
        )
        assert created.status_code == 200
        temporary = created.json()["temporary_password"]
        assert created.json()["account"]["must_change_password"] is True

        login = client.post(
            "/v1/auth/login",
            json={"tenant_id": "tenant-a", "user_id": "alice", "password": temporary},
        ).json()
        blocked = client.get("/v1/dashboard/summary", headers=_bearer(login["access_token"]))
        assert blocked.status_code == 403
        assert blocked.json()["detail"]["code"] == "password_change_required"

        changed = client.post(
            "/v1/auth/change-password",
            headers=_bearer(login["access_token"]),
            json={"current_password": temporary, "new_password": "NewStrongPass456"},
        )
        assert changed.status_code == 200
        assert changed.json()["account"]["must_change_password"] is False
        updated_headers = _bearer(changed.json()["access_token"])
        dashboard = client.get("/v1/dashboard/summary", headers=updated_headers)
        assert dashboard.status_code == 403
        assert dashboard.json()["detail"]["code"] == "role_not_allowed"
        assert client.get("/v1/agents", headers=updated_headers).status_code == 200

        reset = client.post(
            "/v1/access-control/users/alice/reset-password",
            headers=_bearer(owner["access_token"]),
            json={"generate_temporary_password": True},
        )
        assert reset.status_code == 200
        assert reset.json()["account"]["must_change_password"] is True


def test_production_disables_self_service_registration(tmp_path: Path):
    settings = _settings(
        tmp_path,
        app_env="production",
        jwt_secret="test-production-jwt-secret",
    )
    with TestClient(create_app(settings)) as client:
        blocked = client.post(
            "/v1/auth/register",
            json={
                "tenant_id": "tenant-a",
                "user_id": "owner",
                "display_name": "Owner",
                "password": "StrongPass123",
            },
        )
        assert blocked.status_code == 403
        assert blocked.json()["detail"] == "self-service registration is disabled"
