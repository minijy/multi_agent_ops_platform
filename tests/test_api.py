import base64
import io
import json
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from ops_agent.api.app import create_app
from ops_agent.config import Settings
from ops_agent.runtime.model_errors import ModelProviderError


def _settings(tmp_path: Path, **overrides) -> Settings:
    values = dict(
        _env_file=None,
        platform_db_path=tmp_path / "platform.sqlite3",
        session_event_path=tmp_path / "session-events.sqlite3",
        app_api_key="",
    )
    values.update(overrides)
    return Settings(**values)


def test_health_and_dashboard(tmp_path: Path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        health = client.get("/health").json()
        assert health["status"] == "ok"
        assert health["session_events"] == "sqlite"
        assert health["agent_runtime"] == "ready"

        dashboard = client.get("/v1/dashboard/summary").json()
        assert "audit_events" in dashboard
        assert "waiting_approval" in dashboard
        assert "runtime" in dashboard


def test_api_rejects_invalid_key(tmp_path: Path):
    with TestClient(create_app(_settings(tmp_path, app_api_key="test-key"))) as client:
        response = client.post(
            "/v1/agent/query",
            json={"question": "你好"},
        )
        assert response.status_code == 401


def test_agent_session_tenant_isolation(tmp_path: Path):
    tenant_headers = {
        "X-Tenant-ID": "tenant-a",
        "X-User-ID": "operator-a",
        "X-User-Role": "operator",
    }
    with TestClient(create_app(_settings(tmp_path))) as client:
        created = client.post(
            "/v1/agent/query",
            headers=tenant_headers,
            json={"question": "你好，请介绍当前 Runtime"},
        )
        assert created.status_code == 200
        session_id = created.json()["session_id"]

        other_tenant = {**tenant_headers, "X-Tenant-ID": "tenant-b"}
        missing = client.get(
            f"/v1/agent/sessions/{session_id}/events",
            headers=other_tenant,
        )
        assert missing.status_code == 404


def test_catalog_configuration_and_frontend_are_available(tmp_path: Path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        home = client.get("/")
        assert home.status_code == 200
        assert "ArkFlow" in home.text
        assert client.get("/ui/styles.css").status_code == 200

        catalog = client.get("/v1/catalog").json()
        assert catalog["workflows"][0]["id"] == "function-calling-runtime-v1"
        assert len(catalog["agents"]) == 5
        assert catalog["agents"][0]["status"] in {"active", "disabled"}
        assert catalog["tools"]

        agents = client.get("/v1/agents").json()
        assert agents["count"] == 5
        detail = client.get("/v1/agents/function-calling-runtime").json()
        assert "system_prompt" in detail
        assert detail["builtin_tools"]
        assert "tool_catalog" in detail

        configuration = client.get("/v1/configuration").json()
        assert configuration["secrets"] == {"exposed": False}
        for model in configuration.get("models", {}).get("items", []):
            if model.get("api_key"):
                assert model["api_key"] == "********"
        assert "sk-" not in str(configuration).lower()
        assert configuration["context_window"]["enabled"] is True
        assert configuration["context_window"]["keep_recent_user_turns"] >= 1


def test_function_calling_runtime_and_event_api(tmp_path: Path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        response = client.post(
            "/v1/agent/query",
            json={"question": "你好，请介绍当前 Runtime"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["provider"] == "mock"
        assert body["event_count"] >= 5

        events = client.get(
            f"/v1/agent/sessions/{body['session_id']}/events"
        )
        assert events.status_code == 200
        assert events.json()["count"] == body["event_count"]

        foreign = client.delete(
            f"/v1/agent/sessions/{body['session_id']}",
            headers={"X-Tenant-ID": "tenant-b"},
        )
        assert foreign.status_code == 404

        deleted = client.delete(f"/v1/agent/sessions/{body['session_id']}")
        assert deleted.status_code == 200
        assert deleted.json()["deleted"] is True
        assert deleted.json()["event_count"] == body["event_count"]
        missing = client.get(f"/v1/agent/sessions/{body['session_id']}/events")
        assert missing.status_code == 404


def test_agent_query_stream_emits_tokens_and_done(tmp_path: Path):
    settings = _settings(
        tmp_path,
        app_api_key="",
        attachment_path=tmp_path / "attachments",
        skills_paths=str(tmp_path / "missing-skills"),
        mcp_config_path=tmp_path / "missing-mcp.json",
    )
    with TestClient(create_app(settings)) as client:
        with client.stream(
            "POST",
            "/v1/agent/query/stream",
            json={"question": "你好，请介绍当前 Runtime"},
        ) as response:
            body = "".join(response.iter_text())
        assert response.status_code == 200
        assert "event: session" in body
        assert "event: token" in body
        assert "event: done" in body
        assert "Function Calling" in body


def test_agent_query_resume_replays_completed_session(tmp_path: Path):
    settings = _settings(
        tmp_path,
        app_api_key="",
        attachment_path=tmp_path / "attachments",
        skills_paths=str(tmp_path / "missing-skills"),
        mcp_config_path=tmp_path / "missing-mcp.json",
    )
    with TestClient(create_app(settings)) as client:
        with client.stream(
            "POST",
            "/v1/agent/query/stream",
            json={"question": "你好，请介绍当前 Runtime"},
        ) as response:
            body = "".join(response.iter_text())
        session_id = None
        for line in body.splitlines():
            if line.startswith("data:"):
                payload = json.loads(line[5:].strip())
                if payload.get("session_id"):
                    session_id = payload["session_id"]
                    break
        assert session_id
        missing = client.post(
            "/v1/agent/query/resume", json={"session_id": "missing-session-id"}
        )
        assert missing.status_code == 404
        with client.stream(
            "POST",
            "/v1/agent/query/resume",
            json={"session_id": session_id},
        ) as resumed:
            resume_body = "".join(resumed.iter_text())
        assert resumed.status_code == 200
        assert "event: done" in resume_body
        events = client.get(f"/v1/agent/sessions/{session_id}/events")
        users = [
            item
            for item in events.json()["items"]
            if item["event_type"] == "user.message"
        ]
        assert len(users) == 1


def test_attachment_and_skill_api(tmp_path: Path):
    skill_dir = tmp_path / "skills" / "test-skill"
    skill_dir.mkdir(parents=True)
    skill_dir.joinpath("SKILL.md").write_text(
        "---\nname: test-skill\ndescription: Test lazy skills.\n---\n# Body\n",
        encoding="utf-8",
    )
    settings = _settings(
        tmp_path,
        app_api_key="",
        attachment_path=tmp_path / "attachments",
        skills_paths=str(tmp_path / "skills"),
        mcp_config_path=tmp_path / "missing-mcp.json",
    )
    buffer = io.BytesIO()
    Image.new("RGB", (4, 5), color="blue").save(buffer, format="PNG")
    with TestClient(create_app(settings)) as client:
        upload = client.post(
            "/v1/agent/attachments",
            json={
                "name": "chart.png",
                "media_type": "image/png",
                "data_base64": base64.b64encode(buffer.getvalue()).decode(),
            },
        )
        assert upload.status_code == 201
        assert upload.json()["width"] == 4
        assert upload.json()["attachment_id"].startswith("sha256:")

        skills = client.get("/v1/agent/skills")
        assert skills.status_code == 200
        assert skills.json()["items"][0]["name"] == "test-skill"


def test_agent_api_returns_friendly_rate_limit_error(tmp_path: Path):
    settings = _settings(
        tmp_path,
        app_api_key="",
        attachment_path=tmp_path / "attachments",
        skills_paths=str(tmp_path / "missing-skills"),
        mcp_config_path=tmp_path / "missing-mcp.json",
    )

    class FailingRuntime:
        def run(self, *_args, **_kwargs):
            raise ModelProviderError(
                provider="zhipu",
                code="1302",
                user_message="请求过于频繁，已触发模型速率限制，请稍后再试。",
                status_code=429,
                retry_after_seconds=30,
            )

    with TestClient(create_app(settings)) as client:
        client.app.state.agent_runtime = FailingRuntime()
        response = client.post(
            "/v1/agent/query",
            json={"question": "测试友好限流"},
        )
        assert response.status_code == 429
        assert response.headers["retry-after"] == "30"
        assert response.json()["detail"] == {
            "code": "1302",
            "message": "请求过于频繁，已触发模型速率限制，请稍后再试。",
            "provider": "zhipu",
            "retry_after_seconds": 30,
        }


def test_stage_three_subagent_and_governance_api(tmp_path: Path):
    settings = _settings(
        tmp_path,
        app_api_key="",
        session_event_path=tmp_path / "events.sqlite3",
        runtime_governance_path=tmp_path / "governance.sqlite3",
        attachment_path=tmp_path / "attachments",
        sandbox_workspace_root=tmp_path,
        subagent_default_timeout_seconds=10,
        subagent_default_token_budget=1000,
    )
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/v1/agent/subagents",
            json={
                "objective": "总结后台任务是否正常",
                "parent_session_id": "parent-api-session",
                "wait": True,
            },
        )
        assert response.status_code == 202
        task = response.json()
        assert task["status"] == "completed"
        assert task["child_session_id"] != task["parent_session_id"]

        tasks = client.get(
            "/v1/agent/subagents",
            params={"parent_session_id": "parent-api-session"},
        ).json()
        assert tasks["count"] == 1
        assert tasks["items"][0]["task_id"] == task["task_id"]

        approvals = client.get("/v1/agent/approvals")
        assert approvals.status_code == 200
        assert approvals.json()["count"] == 0

        configuration = client.get("/v1/configuration").json()
        assert configuration["agent_runtime"]["governance"]["per_call_approval"] is True
        assert configuration["agent_runtime"]["governance"]["subagent_max_depth"] == 3


def test_workspace_file_download_stays_inside_sandbox(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "file.txt"
    target.write_text("hello-download", encoding="utf-8")
    outside = tmp_path / "secret.txt"
    outside.write_text("nope", encoding="utf-8")
    settings = _settings(
        tmp_path,
        app_api_key="",
        attachment_path=tmp_path / "attachments",
        sandbox_workspace_root=workspace,
        mcp_config_path=tmp_path / "missing-mcp.json",
        skills_paths=str(tmp_path / "missing-skills"),
    )
    with TestClient(create_app(settings)) as client:
        ok = client.get("/v1/agent/workspace/file", params={"path": "file.txt"})
        assert ok.status_code == 200
        assert ok.content == b"hello-download"

        via_scheme = client.get(
            "/v1/agent/workspace/file",
            params={"path": f"sandbox:{target}"},
        )
        assert via_scheme.status_code == 200
        assert via_scheme.content == b"hello-download"

        escaped = client.get(
            "/v1/agent/workspace/file",
            params={"path": str(outside)},
        )
        assert escaped.status_code in {403, 404}
        assert escaped.content != b"nope"

        missing = client.get(
            "/v1/agent/workspace/file",
            params={"path": "missing.txt"},
        )
        assert missing.status_code == 404


def test_context_window_configuration_roundtrip(tmp_path: Path):
    settings = _settings(
        tmp_path,
        app_api_key="",
        attachment_path=tmp_path / "attachments",
        runtime_overrides_path=tmp_path / "overrides.json",
        mcp_config_path=tmp_path / "missing-mcp.json",
        skills_paths=str(tmp_path / "missing-skills"),
    )
    with TestClient(create_app(settings)) as client:
        patched = client.patch(
            "/v1/configuration/context-window",
            json={"keep_recent_user_turns": 3, "max_chars": 12000, "enabled": True},
        )
        assert patched.status_code == 200
        body = patched.json()["context_window"]
        assert body["keep_recent_user_turns"] == 3
        assert body["max_chars"] == 12000

        again = client.get("/v1/configuration").json()["context_window"]
        assert again["keep_recent_user_turns"] == 3
        saved = json.loads((tmp_path / "overrides.json").read_text(encoding="utf-8"))
        assert saved["context_keep_recent_user_turns"] == 3

        denied = client.patch(
            "/v1/configuration/context-window",
            headers={"X-User-Role": "viewer"},
            json={"enabled": False},
        )
        assert denied.status_code == 403


def test_agent_configuration_can_be_updated_by_admin(tmp_path: Path):
    settings = _settings(
        tmp_path,
        agent_definitions_path=tmp_path / "agent_definitions.json",
    )
    with TestClient(create_app(settings)) as client:
        patched = client.patch(
            "/v1/agents/function-calling-runtime",
            json={
                "name": "Runtime Agent",
                "role": "测试职责",
                "description": "页面配置测试",
                "enabled": True,
                "system_prompt": "你是测试 Agent。",
                "allowed_tools": [],
            },
        )
        assert patched.status_code == 200
        body = patched.json()
        assert body["name"] == "Runtime Agent"
        assert body["allowed_tools"] == []

        again = client.get("/v1/agents/function-calling-runtime").json()
        assert again["system_prompt"] == "你是测试 Agent。"

        denied = client.patch(
            "/v1/agents/function-calling-runtime",
            headers={"X-User-Role": "viewer"},
            json={"enabled": False},
        )
        assert denied.status_code == 403

        builtin_rejected = client.patch(
            "/v1/agents/function-calling-runtime",
            json={"allowed_tools": ["load_skill"]},
        )
        assert builtin_rejected.status_code == 400

        bad_tool = client.patch(
            "/v1/agents/function-calling-runtime",
            json={"allowed_tools": ["missing-tool"]},
        )
        assert bad_tool.status_code == 400


def test_lingxing_agent_integration_can_be_updated_by_admin(tmp_path: Path):
    settings = _settings(
        tmp_path,
        agent_definitions_path=tmp_path / "agent_definitions.json",
    )
    with TestClient(create_app(settings)) as client:
        patched = client.patch(
            "/v1/agents/lingxing-profit-report",
            json={
                "integration": {
                    "app_id": "demo-app-id",
                    "app_secret": "demo-app-secret",
                    "base_url": "https://openapi.lingxing.com",
                }
            },
        )
        assert patched.status_code == 200
        body = patched.json()
        assert body["integration"]["app_id"] == "demo-app-id"
        assert body["integration"]["app_secret"] == "********"
        assert body["integration"]["app_secret_configured"] is True

        detail = client.get("/v1/agents/lingxing-profit-report").json()
        assert detail["integration"]["app_secret"] == "********"

        secret_kept = client.patch(
            "/v1/agents/lingxing-profit-report",
            json={"integration": {"app_id": "demo-app-id", "app_secret": "********"}},
        )
        assert secret_kept.status_code == 200

        query = client.post(
            "/v1/lingxing-profit/query",
            json={"question": "查询 2024-09-01 到 2024-09-03 USD 利润"},
        )
        assert query.status_code in {400, 503}


def test_model_configuration_can_be_managed_by_admin(tmp_path: Path):
    settings = _settings(
        tmp_path,
        model_definitions_path=tmp_path / "model_definitions.json",
        model_provider="mock",
    )
    with TestClient(create_app(settings)) as client:
        configuration = client.get("/v1/configuration").json()
        assert configuration["models"]["count"] >= 1
        assert configuration["model"]["default_model_id"]

        chat_models = client.get("/v1/models").json()
        assert chat_models["count"] >= 1
        assert chat_models["default_model_id"]

        created = client.post(
            "/v1/configuration/models",
            json={
                "id": "openai-test",
                "name": "OpenAI Test",
                "provider": "openai",
                "model_name": "gpt-4o-mini",
                "api_key": "sk-test",
                "enabled": True,
            },
        )
        assert created.status_code == 201
        body = created.json()
        assert body["id"] == "openai-test"
        assert body["api_key"] == "********"
        assert body["api_key_configured"] is True

        updated = client.patch(
            "/v1/configuration/models/openai-test",
            json={"name": "OpenAI Updated", "is_default": True},
        )
        assert updated.status_code == 200
        assert updated.json()["name"] == "OpenAI Updated"
        assert updated.json()["is_default"] is True

        listed = client.get("/v1/configuration/models").json()
        assert any(item["id"] == "openai-test" for item in listed["items"])

        denied = client.post(
            "/v1/configuration/models",
            headers={"X-User-Role": "viewer"},
            json={
                "id": "blocked",
                "name": "Blocked",
                "provider": "mock",
                "model_name": "mock",
            },
        )
        assert denied.status_code == 403

        deleted = client.delete("/v1/configuration/models/openai-test")
        assert deleted.status_code == 204
        assert not any(
            item["id"] == "openai-test"
            for item in client.get("/v1/configuration/models").json()["items"]
        )


def test_kingdee_agent_integration_can_be_updated_by_admin(tmp_path: Path):
    settings = _settings(
        tmp_path,
        agent_definitions_path=tmp_path / "agent_definitions.json",
    )
    with TestClient(create_app(settings)) as client:
        patched = client.patch(
            "/v1/agents/kingdee-cloud",
            json={
                "enabled": True,
                "integration": {
                    "server_url": "https://erp.example.com/K3Cloud",
                    "acct_id": "100001",
                    "app_id": "demo-app",
                    "app_secret": "demo-secret",
                    "username": "demo-user",
                    "lcid": 2052,
                },
            },
        )
        assert patched.status_code == 200
        body = patched.json()
        assert body["integration"]["server_url"] == "https://erp.example.com/K3Cloud"
        assert body["integration"]["app_secret"] == "********"
        assert body["integration"]["app_secret_configured"] is True

        configuration = client.get("/v1/configuration").json()
        assert configuration["kingdee_cloud"]["configured"] is True
