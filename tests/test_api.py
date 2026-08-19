import base64
import io
import json
import threading
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from ops_agent.api.app import create_app
from ops_agent.agent_roles import SPECIALIST_ANALYST_IDS, SYSTEM_DEFAULT_TOOL_NAMES
from ops_agent.config import Settings
from ops_agent.runtime.model_errors import ModelProviderError
from ops_agent.runtime.result_store import StoredResult
from ops_agent.runtime.domain import ToolResult
from ops_agent.workflows.amazon_finance.domain import AmazonFinanceQueryPlan


def _settings(tmp_path: Path, **overrides) -> Settings:
    configured_model = overrides.pop("configured_model", True)
    values = dict(
        _env_file=None,
        platform_db_path=tmp_path / "platform.sqlite3",
        session_event_path=tmp_path / "session-events.sqlite3",
        runtime_governance_path=tmp_path / "runtime-governance.sqlite3",
        runtime_metrics_path=tmp_path / "runtime-metrics.sqlite3",
        memory_db_path=tmp_path / "memory.sqlite3",
        agent_definitions_path=tmp_path / "agent-definitions.json",
        model_definitions_path=tmp_path / "model-definitions.json",
        connection_definitions_path=tmp_path / "connections.json",
        connection_secrets_path=tmp_path / "connection-secrets.json",
        tool_bindings_path=tmp_path / "tool-bindings.json",
        knowledge_spaces_path=tmp_path / "knowledge-spaces.json",
        attachment_path=tmp_path / "attachments",
        app_api_key="",
    )
    values.update(overrides)
    settings = Settings(**values)
    if configured_model and not settings.model_definitions_path.exists():
        settings.model_definitions_path.write_text(
            json.dumps(
                {
                    "test-mock": {
                        "name": "Test Mock",
                        "provider": "mock",
                        "model_name": "mock-function-calling",
                        "enabled": True,
                        "is_default": True,
                    }
                }
            ),
            encoding="utf-8",
        )
    return settings


def _create_analytics_connection(client: TestClient) -> dict:
    response = client.post(
        "/v1/connections",
        json={
            "connector_type": "analytics",
            "name": "测试 PostgreSQL",
            "credentials": {"dsn": "postgresql://configured"},
            "resource_scopes": {"store_names": ["*"]},
        },
    )
    assert response.status_code == 201
    return response.json()


def _create_qdrant_connection(client: TestClient) -> dict:
    response = client.post(
        "/v1/connections",
        json={
            "connector_type": "qdrant",
            "name": "Knowledge Qdrant",
            "config": {"url": "https://qdrant.example.com"},
            "credentials": {"api_key": "secret-key"},
        },
    )
    assert response.status_code == 201
    return response.json()


def test_health_and_dashboard(tmp_path: Path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        health = client.get("/health").json()
        assert health["status"] == "ok"
        assert health["session_events"] == "sqlite"
        assert health["agent_runtime"] == "ready"


def test_fresh_install_requires_model_configuration(tmp_path: Path):
    settings = _settings(tmp_path, configured_model=False)
    with TestClient(create_app(settings)) as client:
        health = client.get("/health").json()
        assert health["model_provider"] == "unconfigured"

        configuration = client.get("/v1/configuration").json()
        assert configuration["model"]["configured"] is False
        assert configuration["models"]["items"] == []
        assert configuration["models"]["default_model_id"] is None

        assert client.get("/v1/models").json() == {
            "items": [],
            "count": 0,
            "default_model_id": None,
        }
        response = client.post(
            "/v1/agent/query",
            json={"question": "这个请求不应该由 Mock 回答"},
        )
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "model_configuration_required"

        dashboard = client.get("/v1/dashboard/summary").json()
        assert "audit_events" in dashboard
        assert "waiting_approval" in dashboard
        assert "runtime" in dashboard


def test_analytics_env_dsn_does_not_create_a_runtime_connection(tmp_path: Path):
    settings = _settings(tmp_path, analytics_dsn="postgresql://legacy-env-value")
    with TestClient(create_app(settings)) as client:
        assert client.get("/v1/connections").json()["count"] == 0
        assert client.get("/health").json()["amazon_finance"] == "disabled"
        response = client.post(
            "/v1/amazon-finance/query",
            json={"question": "查询总览", "plan": {"metric": "overview"}},
        )
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "connector_not_configured"


def test_knowledge_space_uses_tenant_vector_connection(tmp_path: Path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        connection = _create_qdrant_connection(client)
        created = client.post(
            "/v1/knowledge/spaces",
            json={
                "name": "Operations handbook",
                "connection_id": connection["id"],
                "collection_name": "ops_chunks",
                "embedding_model": "BAAI/bge-m3",
                "vector_dimension": 1024,
                "top_k": 8,
                "text_field": "content",
                "knowledge_base_id": "ops",
            },
        )
        assert created.status_code == 201
        space = created.json()
        assert space["collection_name"] == "ops_chunks"
        assert space["vector_dimension"] == 1024

        listed = client.get("/v1/knowledge/spaces").json()
        assert listed["count"] == 1
        assert listed["items"][0]["connector_type"] == "qdrant"
        assert client.get("/v1/configuration").json()["knowledge"]["count"] == 1

        client.app.state.connector_runtime.execute_connection = (
            lambda _tenant, _connection, _operation: {
                "items": [
                    {
                        "id": "chunk-1",
                        "content": "Refunds require approval.",
                        "category": "policy",
                        "metadata": {"document": "handbook.pdf", "page": 3},
                    }
                ],
                "next_cursor": None,
                "total": 1,
            }
        )
        contents = client.get(
            f"/v1/knowledge/spaces/{space['id']}/contents"
        )
        assert contents.status_code == 200
        assert contents.json()["categories"] == ["policy"]
        assert contents.json()["items"][0]["content"].startswith("Refunds")

        blocked = client.delete(f"/v1/connections/{connection['id']}")
        assert blocked.status_code == 409
        assert "knowledge spaces" in blocked.json()["detail"]

        updated = client.patch(
            f"/v1/knowledge/spaces/{space['id']}",
            json={"top_k": 12, "enabled": False},
        )
        assert updated.status_code == 200
        assert updated.json()["top_k"] == 12

        assert client.delete(f"/v1/knowledge/spaces/{space['id']}").status_code == 204
        assert client.delete(f"/v1/connections/{connection['id']}").status_code == 204


def test_knowledge_space_rejects_non_vector_connection(tmp_path: Path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        connection = _create_analytics_connection(client)
        response = client.post(
            "/v1/knowledge/spaces",
            json={
                "name": "Invalid",
                "connection_id": connection["id"],
                "collection_name": "chunks",
                "embedding_model": "BAAI/bge-m3",
            },
        )
        assert response.status_code == 400
        assert "Qdrant" in response.json()["detail"]


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


def test_agent_session_user_isolation_within_tenant(tmp_path: Path):
    owner = {
        "X-Tenant-ID": "tenant-a",
        "X-User-ID": "operator-a",
        "X-User-Role": "operator",
    }
    other_user = {
        "X-Tenant-ID": "tenant-a",
        "X-User-ID": "operator-b",
        "X-User-Role": "operator",
    }
    with TestClient(create_app(_settings(tmp_path))) as client:
        created = client.post(
            "/v1/agent/query",
            headers=owner,
            json={"question": "你好，请介绍当前 Runtime"},
        )
        assert created.status_code == 200
        session_id = created.json()["session_id"]

        owner_sessions = client.get(
            "/v1/agent/sessions", headers=owner
        ).json()
        assert owner_sessions["count"] == 1
        assert owner_sessions["items"][0]["session_id"] == session_id
        assert owner_sessions["items"][0]["title"].startswith("你好")
        assert client.get(
            "/v1/agent/sessions", headers=other_user
        ).json()["count"] == 0

        client.app.state.session_events.append(
            session_id="child-session-for-owner",
            tenant_id="tenant-a",
            user_id="operator-a",
            event_type="session.created",
            payload={"parent_session_id": session_id},
        )
        client.app.state.session_events.append(
            session_id="child-session-for-owner",
            tenant_id="tenant-a",
            user_id="operator-a",
            event_type="user.message",
            payload={"content": "子 Agent 任务"},
        )
        assert client.get(
            "/v1/agent/sessions", headers=owner
        ).json()["count"] == 1

        assert client.get(
            f"/v1/agent/sessions/{session_id}/events", headers=other_user
        ).status_code == 404
        assert client.get(
            f"/v1/agent/sessions/{session_id}/events",
            headers={
                "X-Tenant-ID": "tenant-a",
                "X-User-ID": "other-admin",
                "X-User-Role": "admin",
            },
        ).status_code == 404
        assert client.post(
            "/v1/agent/query/resume",
            headers=other_user,
            json={"session_id": session_id},
        ).status_code == 404
        assert client.post(
            "/v1/agent/query",
            headers=other_user,
            json={"question": "继续", "session_id": session_id},
        ).status_code == 404
        assert client.delete(
            f"/v1/agent/sessions/{session_id}", headers=other_user
        ).status_code == 404
        assert client.get(
            f"/v1/agent/sessions/{session_id}/events", headers=owner
        ).status_code == 200


def test_catalog_configuration_and_frontend_are_available(tmp_path: Path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        home = client.get("/")
        assert home.status_code == 200
        assert "ArkFlow" in home.text
        assert "长期记忆" in home.text
        assert client.get("/ui/styles.css").status_code == 200
        frontend_script = client.get("/ui/app.js")
        assert frontend_script.status_code == 200
        assert "loadMemory" in frontend_script.text
        assert "roleCanAccessPage" in frontend_script.text
        assert "紧凑结果与计算口径" not in frontend_script.text
        assert "step.task_id===payload.task_id" in frontend_script.text
        assert "查看${escapeHTML(action)}详情" in frontend_script.text
        assert 'data-role-allow="admin"' in home.text

        catalog = client.get("/v1/catalog").json()
        assert catalog["workflows"][0]["id"] == "function-calling-runtime-v1"
        assert len(catalog["agents"]) == 9
        assert catalog["agents"][0]["status"] in {"active", "disabled"}
        assert catalog["tools"]
        assert catalog["tool_bindings"]

        connector_health = client.get("/v1/connections/health")
        assert connector_health.status_code == 200
        assert connector_health.json() == {"items": [], "count": 0}

        agents = client.get("/v1/agents").json()
        assert agents["count"] == 9
        detail = client.get("/v1/agents/function-calling-runtime").json()
        assert "system_prompt" in detail
        assert "delegate_subagent" in detail["role_tools"]
        assert "delegate_specialists" in detail["role_tools"]
        assert "tool_catalog" in detail

        configuration = client.get("/v1/configuration").json()
        assert configuration["secrets"] == {"exposed": False}
        for model in configuration.get("models", {}).get("items", []):
            if model.get("api_key"):
                assert model["api_key"] == "********"
        assert "sk-" not in str(configuration).lower()
        assert configuration["context_window"]["enabled"] is True
        assert configuration["context_window"]["keep_recent_user_turns"] >= 1


def test_non_admin_management_pages_are_api_restricted(tmp_path: Path):
    operator = {"X-User-ID": "operator-a", "X-User-Role": "operator"}
    approver = {"X-User-ID": "approver-a", "X-User-Role": "approver"}
    with TestClient(create_app(_settings(tmp_path))) as client:
        for path in (
            "/v1/dashboard/summary",
            "/v1/connections",
            "/v1/connections/health",
            "/v1/memories",
            "/v1/access-control",
            "/v1/audit-events",
        ):
            assert client.get(path, headers=operator).status_code == 403

        assert client.get("/v1/agent/approvals", headers=operator).status_code == 403
        assert client.get("/v1/agent/approvals", headers=approver).status_code == 200
        assert client.get("/v1/dashboard/summary", headers=approver).status_code == 403


def test_direct_workflow_query_uses_shared_tool_executor(tmp_path: Path):
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        _create_analytics_connection(client)
        captured = {}
        client.app.state.amazon_finance_agent.plan = lambda _payload: (
            AmazonFinanceQueryPlan(metric="overview")
        )

        def execute(call, context):
            captured["call"] = call
            captured["context"] = context
            return ToolResult(
                call_id=call.call_id,
                tool_name=call.name,
                ok=True,
                output={
                    "plan": {"metric": "overview", "limit": 20},
                    "columns": ["transaction_count"],
                    "rows": [{"transaction_count": 1}],
                    "summary": "ok",
                    "data_scope": "RELEASED only",
                },
            )

        client.app.state.runtime_tool_executor.execute = execute
        response = client.post(
            "/v1/amazon-finance/query",
            json={"question": "查询 Amazon 财务概览"},
        )

        assert response.status_code == 200
        assert captured["call"].name == "amazon_finance_query"
        assert captured["context"].allowed_tool_names == frozenset(
            {"amazon_finance_query"}
        )
        assert captured["context"].connection_ids
        assert "seller_id" not in response.json()


def test_direct_query_skips_model_when_plan_provided(tmp_path: Path):
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        _create_analytics_connection(client)
        def structured(*_args, **_kwargs):
            raise AssertionError("model should not be called when plan is provided")

        client.app.state.amazon_finance_agent.model.structured = structured
        client.app.state.profit_report_agent.model.structured = structured

        def execute(call, context):
            return ToolResult(
                call_id=call.call_id,
                tool_name=call.name,
                ok=True,
                output={
                    "plan": call.arguments,
                    "columns": ["transaction_count"],
                    "rows": [{"transaction_count": 3}],
                    "summary": "ok",
                    "total_rows": 3,
                    "data_scope": "test",
                },
            )

        client.app.state.runtime_tool_executor.execute = execute
        amazon = client.post(
            "/v1/amazon-finance/query",
            json={
                "question": "load-test amazon overview",
                "plan": {"metric": "overview"},
            },
        )
        profit = client.post(
            "/v1/profit-report/query",
            json={
                "question": "load-test profit daily",
                "plan": {"metric": "daily", "limit": 10},
            },
        )
        assert amazon.status_code == 200
        assert amazon.json()["plan"]["metric"] == "overview"
        assert profit.status_code == 200
        assert profit.json()["plan"]["metric"] == "daily"


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


def test_coordinator_delegates_amazon_question_to_analyst(tmp_path: Path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        response = client.post(
            "/v1/agent/query",
            json={"question": "分析 2026年7月 Top 5 Amazon 费用"},
        )
        assert response.status_code == 200
        tools = [item["tool_name"] for item in response.json().get("tool_results", [])]
        assert "delegate_subagent" in tools
        assert "amazon_finance_query" not in tools


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


def test_agent_session_interrupt_signals_live_execution(tmp_path: Path):
    settings = _settings(tmp_path)
    app = create_app(settings)
    session_id = "33333333-3333-3333-3333-333333333333"
    with TestClient(app) as client:
        app.state.session_events.append(
            session_id=session_id,
            tenant_id="tenant-a",
            user_id="user-a",
            event_type="session.created",
            payload={"role": "operator"},
        )
        app.state.session_events.append(
            session_id=session_id,
            tenant_id="tenant-a",
            user_id="user-a",
            event_type="user.message",
            payload={"content": "长任务", "attachment_ids": []},
        )
        control = threading.Event()
        app.state.agent_runtime.live_hub.begin(session_id, control)
        try:
            response = client.post(
                f"/v1/agent/sessions/{session_id}/interrupt",
                headers={"X-User-ID": "user-a", "X-User-Role": "operator"},
            )
            assert response.status_code == 200
            assert response.json()["status"] == "interrupt_requested"
            assert control.is_set()
            events = app.state.session_events.list_events(
                session_id=session_id, tenant_id="tenant-a"
            )
            assert events[-1].event_type == "turn.interrupt_requested"
        finally:
            app.state.agent_runtime.live_hub.end(session_id)

        stopped = client.post(
            f"/v1/agent/sessions/{session_id}/interrupt",
            headers={"X-User-ID": "user-a", "X-User-Role": "operator"},
        )
        assert stopped.status_code == 409


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


def test_materialized_result_api_is_paginated_and_tenant_scoped(tmp_path: Path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        client.app.state.result_store.put(
            StoredResult(
                result_ref="result-api-test",
                tenant_id="tenant-a",
                user_id="alice",
                session_id="session-result",
                tool_name="profit_report_query",
                payload={
                    "columns": ["n"],
                    "rows": [{"n": index} for index in range(25)],
                    "summary": "25 rows",
                    "data_quality": {"source_rows": 250},
                },
                created_at="2026-08-18T00:00:00+00:00",
            )
        )
        page = client.get(
            "/v1/agent/results/result-api-test?offset=10&limit=5",
            headers={"X-User-ID": "alice", "X-User-Role": "operator"},
        )
        assert page.status_code == 200
        assert [row["n"] for row in page.json()["rows"]] == list(range(10, 15))
        assert page.json()["source_rows"] == 250

        denied = client.get(
            "/v1/agent/results/result-api-test",
            headers={"X-User-ID": "bob", "X-User-Role": "operator"},
        )
        assert denied.status_code == 404
        hidden = client.get(
            "/v1/agent/results/result-api-test",
            headers={"X-Tenant-ID": "tenant-b"},
        )
        assert hidden.status_code == 404


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

        other_user_headers = {
            "X-User-ID": "other-user",
            "X-User-Role": "operator",
        }
        hidden_tasks = client.get(
            "/v1/agent/subagents",
            params={"parent_session_id": "parent-api-session"},
            headers=other_user_headers,
        )
        assert hidden_tasks.json()["count"] == 0
        assert client.get(
            f"/v1/agent/subagents/{task['task_id']}", headers=other_user_headers
        ).status_code == 404
        assert client.post(
            f"/v1/agent/subagents/{task['task_id']}/cancel",
            headers=other_user_headers,
        ).status_code == 404

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


def test_analyst_runtime_mode_configuration_roundtrip(tmp_path: Path):
    settings = _settings(
        tmp_path,
        runtime_overrides_path=tmp_path / "overrides.json",
    )
    with TestClient(create_app(settings)) as client:
        initial = client.get("/v1/configuration").json()["analyst_runtime"]
        assert initial == {"mode": "general", "max_parallel": 3}

        patched = client.patch(
            "/v1/configuration/analyst-runtime",
            json={"mode": "specialized_parallel"},
        )
        assert patched.status_code == 200
        assert patched.json()["analyst_runtime"] == {
            "mode": "specialized_parallel",
            "max_parallel": 3,
        }
        assert settings.analyst_mode == "specialized_parallel"
        saved = json.loads((tmp_path / "overrides.json").read_text(encoding="utf-8"))
        assert saved["analyst_mode"] == "specialized_parallel"

        denied = client.patch(
            "/v1/configuration/analyst-runtime",
            headers={"X-User-Role": "viewer"},
            json={"mode": "general"},
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
            },
        )
        assert patched.status_code == 200
        body = patched.json()
        assert body["name"] == "Runtime Agent"
        assert "delegate_subagent" in body["allowed_tools"]

        again = client.get("/v1/agents/function-calling-runtime").json()
        assert again["system_prompt"] == "你是测试 Agent。"

        denied = client.patch(
            "/v1/agents/function-calling-runtime",
            headers={"X-User-Role": "viewer"},
            json={"enabled": False},
        )
        assert denied.status_code == 403

        data_tool_rejected = client.patch(
            "/v1/agents/function-calling-runtime",
            json={"allowed_tools": ["amazon_finance_query"]},
        )
        assert data_tool_rejected.status_code == 400

        bad_tool = client.patch(
            "/v1/agents/function-calling-runtime",
            json={"allowed_tools": ["missing-tool"]},
        )
        assert bad_tool.status_code == 400


def test_lingxing_connection_must_be_configured_on_connector_page(tmp_path: Path):
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
        assert patched.status_code == 400
        assert patched.json()["detail"]["code"] == "connector_page_required"

        created = client.post(
            "/v1/connections",
            json={
                "connector_type": "lingxing",
                "name": "领星测试连接",
                "config": {
                    "app_id": "demo-app-id",
                    "base_url": "https://openapi.lingxing.com",
                },
                "credentials": {"app_secret": "demo-app-secret"},
            },
        )
        assert created.status_code == 201
        assert created.json()["app_secret"] == "********"


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
                "provider": "openai",
                "model_name": "gpt-4o-mini",
                "api_key": "sk-denied",
            },
        )
        assert denied.status_code == 403

        deleted = client.delete("/v1/configuration/models/openai-test")
        assert deleted.status_code == 204
        assert not any(
            item["id"] == "openai-test"
            for item in client.get("/v1/configuration/models").json()["items"]
        )

        qwen = client.post(
            "/v1/configuration/models",
            json={
                "id": "qwen-test",
                "name": "通义千问测试",
                "provider": "qwen",
                "model_name": "qwen3.7-plus",
                "api_key": "sk-qwen",
                "supports_image_input": True,
                "supports_audio_input": False,
                "enable_thinking": True,
                "thinking_budget": 4096,
                "enabled": True,
            },
        )
        assert qwen.status_code == 201
        qwen_body = qwen.json()
        assert qwen_body["provider"] == "qwen"
        assert qwen_body["api_key"] == "********"
        assert qwen_body["enable_thinking"] is True
        assert qwen_body["thinking_budget"] == 4096
        assert qwen_body["base_url"].endswith("/compatible-mode/v1")
        assert qwen_body["supports_vision"] is True
        assert qwen_body["supports_image"] is True
        assert qwen_body["supports_audio"] is False
        qwen_chat = next(
            item
            for item in client.get("/v1/models").json()["items"]
            if item["id"] == "qwen-test"
        )
        assert qwen_chat["supports_image"] is True
        assert qwen_chat["supports_audio"] is False

        deepseek = client.post(
            "/v1/configuration/models",
            json={
                "id": "deepseek-test",
                "name": "DeepSeek 测试",
                "provider": "deepseek",
                "model_name": "deepseek-chat",
                "api_key": "sk-deepseek",
                "enable_thinking": True,
                "reasoning_effort": "max",
                "enabled": True,
            },
        )
        assert deepseek.status_code == 201
        deepseek_body = deepseek.json()
        assert deepseek_body["provider"] == "deepseek"
        assert deepseek_body["api_key"] == "********"
        assert deepseek_body["enable_thinking"] is True
        assert deepseek_body["reasoning_effort"] == "max"
        assert deepseek_body["base_url"] == "https://api.deepseek.com"

        zhipu_thinking = client.post(
            "/v1/configuration/models",
            json={
                "id": "zhipu-thinking",
                "name": "智谱思考测试",
                "provider": "zhipu",
                "model_name": "glm-5.2",
                "api_key": "sk-zhipu",
                "enable_thinking": True,
                "reasoning_effort": "max",
                "enabled": True,
            },
        )
        assert zhipu_thinking.status_code == 201
        zhipu_body = zhipu_thinking.json()
        assert zhipu_body["provider"] == "zhipu"
        assert zhipu_body["enable_thinking"] is True
        assert zhipu_body["reasoning_effort"] == "max"


def test_remote_model_without_key_cannot_be_enabled_or_selected(tmp_path: Path):
    settings = _settings(
        tmp_path,
        model_definitions_path=tmp_path / "model_definitions.json",
        model_provider="mock",
    )
    with TestClient(create_app(settings)) as client:
        rejected = client.post(
            "/v1/configuration/models",
            json={
                "id": "zhipu-no-key",
                "name": "No Key",
                "provider": "zhipu",
                "model_name": "glm-test",
                "enabled": True,
            },
        )
        assert rejected.status_code == 400
        assert "API Key" in rejected.json()["detail"]

        disabled = client.post(
            "/v1/configuration/models",
            json={
                "id": "zhipu-disabled",
                "name": "Disabled",
                "provider": "zhipu",
                "model_name": "glm-test",
                "enabled": False,
            },
        )
        assert disabled.status_code == 201
        assert disabled.json()["callable"] is False
        assert "zhipu-disabled" not in {
            item["id"] for item in client.get("/v1/models").json()["items"]
        }

        cannot_enable = client.patch(
            "/v1/configuration/models/zhipu-disabled",
            json={"enabled": True},
        )
        assert cannot_enable.status_code == 400

        enabled = client.patch(
            "/v1/configuration/models/zhipu-disabled",
            json={"api_key": "configured-key", "enabled": True},
        )
        assert enabled.status_code == 200
        assert enabled.json()["callable"] is True
        assert "zhipu-disabled" in {
            item["id"] for item in client.get("/v1/models").json()["items"]
        }


def test_kingdee_connection_must_be_configured_on_connector_page(tmp_path: Path):
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
        assert patched.status_code == 400
        assert patched.json()["detail"]["code"] == "connector_page_required"

        created = client.post(
            "/v1/connections",
            json={
                "connector_type": "kingdee",
                "name": "金蝶测试连接",
                "config": {
                    "server_url": "https://erp.example.com/K3Cloud",
                    "acct_id": "100001",
                    "app_id": "demo-app",
                    "username": "demo-user",
                    "lcid": 2052,
                },
                "credentials": {"app_secret": "demo-secret"},
            },
        )
        assert created.status_code == 201
        assert created.json()["app_secret"] == "********"


def test_connection_api_is_tenant_scoped_and_masks_credentials(tmp_path: Path):
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        created = client.put(
            "/v1/connections/analytics",
            json={
                "credentials": {"dsn": "postgresql://reader:secret@db/a"},
                "resource_scopes": {
                    "store_names": ["store-a"],
                },
            },
        )
        assert created.status_code == 200
        assert created.json()["dsn"] == "********"
        assert "seller_ids" not in created.json()["resource_scopes"]
        catalog = client.get("/v1/catalog").json()
        amazon = next(
            item for item in catalog["agents"] if item["id"] == "amazon-finance-query"
        )
        assert amazon["status"] == "active"

        foreign = client.get(
            "/v1/connections", headers={"X-Tenant-ID": "tenant-b"}
        )
        assert foreign.status_code == 200
        assert foreign.json()["count"] == 0

        denied = client.put(
            "/v1/connections/analytics",
            headers={"X-User-Role": "viewer"},
            json={"credentials": {"dsn": "postgresql://blocked"}},
        )
        assert denied.status_code == 403


def test_mysql_connection_can_be_configured_from_api(tmp_path: Path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        created = client.post(
            "/v1/connections",
            json={
                "connector_type": "analytics",
                "name": "MySQL 分析库",
                "config": {"database_type": "mysql"},
                "credentials": {
                    "dsn": "mysql://reader:secret@mysql.example.com:3306/analytics"
                },
            },
        )
        assert created.status_code == 201
        assert created.json()["database_type"] == "mysql"
        assert created.json()["dsn"] == "********"
        assert created.json()["dsn_configured"] is True

        invalid = client.post(
            "/v1/connections",
            json={
                "connector_type": "analytics",
                "name": "Invalid MySQL",
                "config": {"database_type": "mysql"},
                "credentials": {"dsn": "postgresql://reader:secret@db/analytics"},
            },
        )
        assert invalid.status_code == 400
        assert "mysql://" in invalid.json()["detail"]


def test_dingtalk_connection_and_tool_bindings_are_exposed_by_api(tmp_path: Path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        created = client.post(
            "/v1/connections",
            json={
                "connector_type": "dingtalk",
                "name": "企业钉钉",
                "config": {
                    "app_key": "app-key",
                    "robot_code": "robot-code",
                    "default_todo_owner_union_id": "owner-1",
                },
                "credentials": {"app_secret": "app-secret"},
                "resource_scopes": {
                    "dingtalk_user_ids": ["user-1"],
                    "dingtalk_conversation_ids": ["cid-1"],
                    "dingtalk_union_ids": ["owner-1", "executor-1"],
                },
            },
        )
        assert created.status_code == 201
        assert created.json()["app_secret"] == "********"
        assert created.json()["app_secret_configured"] is True

        bindings = {
            item["tool_name"]: item
            for item in client.get("/v1/tool-bindings").json()["items"]
        }
        expected = {
            "dingtalk_send_direct_message": "dingtalk_user_ids",
            "dingtalk_send_group_message": "dingtalk_conversation_ids",
            "dingtalk_create_todo": "dingtalk_union_ids",
        }
        for tool_name, scope_name in expected.items():
            assert bindings[tool_name]["connector_type"] == "dingtalk"
            assert bindings[tool_name]["resource_scope"] == scope_name
            bound = client.put(
                f"/v1/tools/{tool_name}/connection",
                json={
                    "connection_id": created.json()["id"],
                    "resource_scopes": {
                        scope_name: created.json()["resource_scopes"][scope_name]
                    },
                },
            )
            assert bound.status_code == 200

        catalog = client.get("/v1/catalog").json()
        tools = {item["id"]: item for item in catalog["tools"]}
        for tool_name in expected:
            assert tools[tool_name]["approval"] is True
            assert tools[tool_name]["risk"] == "medium"


def test_tavily_connection_and_web_search_binding_are_exposed_by_api(tmp_path: Path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        created = client.post(
            "/v1/connections",
            json={
                "connector_type": "tavily",
                "name": "Tavily 搜索",
                "config": {},
                "credentials": {"api_key": "tvly-test-secret"},
            },
        )
        assert created.status_code == 201
        assert created.json()["api_key"] == "********"
        assert created.json()["api_key_configured"] is True
        assert created.json()["base_url"] == "https://api.tavily.com"

        bindings = {
            item["tool_name"]: item
            for item in client.get("/v1/tool-bindings").json()["items"]
        }
        assert bindings["web_search"]["connector_type"] == "tavily"
        bound = client.put(
            "/v1/tools/web_search/connection",
            json={"connection_id": created.json()["id"]},
        )
        assert bound.status_code == 200
        catalog = {item["id"]: item for item in client.get("/v1/catalog").json()["tools"]}
        assert catalog["web_search"]["approval"] is False
        assert catalog["web_search"]["risk"] == "low"


def test_multiple_connections_can_be_bound_to_individual_tools(tmp_path: Path):
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        first = client.post(
            "/v1/connections",
            json={
                "connector_type": "analytics",
                "name": "Amazon warehouse",
                "credentials": {"dsn": "postgresql://amazon"},
            },
        )
        second = client.post(
            "/v1/connections",
            json={
                "connector_type": "analytics",
                "name": "Profit warehouse",
                "credentials": {"dsn": "postgresql://profit"},
                "resource_scopes": {"store_names": ["store-a"]},
            },
        )
        assert first.status_code == 201
        assert second.status_code == 201
        assert first.json()["id"] != second.json()["id"]

        bound = client.put(
            "/v1/tools/profit_report_query/connection",
            json={"connection_id": second.json()["id"]},
        )
        assert bound.status_code == 200
        bindings = client.get("/v1/tool-bindings").json()["items"]
        profit = next(
            item for item in bindings if item["tool_name"] == "profit_report_query"
        )
        assert profit["connection_id"] == second.json()["id"]
        assert {item["name"] for item in profit["connections"]} == {
            "Amazon warehouse",
            "Profit warehouse",
        }

        blocked_delete = client.delete(
            f"/v1/connections/{second.json()['id']}"
        )
        assert blocked_delete.status_code == 409

        foreign_bind = client.put(
            "/v1/tools/amazon_finance_query/connection",
            headers={"X-Tenant-ID": "tenant-b"},
            json={"connection_id": first.json()["id"]},
        )
        assert foreign_bind.status_code == 400


def test_access_control_api_and_tool_binding_data_scope(tmp_path: Path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        connection = client.post(
            "/v1/connections",
            json={
                "connector_type": "analytics",
                "name": "Scoped warehouse",
                "credentials": {"dsn": "postgresql://scoped"},
                "resource_scopes": {"store_names": ["store-a", "store-b"]},
            },
        ).json()
        narrowed = client.put(
            "/v1/tools/profit_report_query/connection",
            json={
                "connection_id": connection["id"],
                "resource_scopes": {"store_names": ["store-a"]},
            },
        )
        assert narrowed.status_code == 200
        assert narrowed.json()["resource_scopes"] == {"store_names": ["store-a"]}

        widened = client.put(
            "/v1/tools/profit_report_query/connection",
            json={
                "connection_id": connection["id"],
                "resource_scopes": {"store_names": ["store-c"]},
            },
        )
        assert widened.status_code == 400

        assert client.put(
            "/v1/access-control/users/alice",
            json={"id": "alice", "name": "Alice", "enabled": True},
        ).status_code == 200
        group = client.post(
            "/v1/access-control/groups", json={"name": "Finance"}
        ).json()
        rule = client.post(
            "/v1/access-control/rules",
            json={
                "group_id": group["id"],
                "name": "Profit only",
                "tool_names": ["profit_report_query"],
            },
        ).json()
        duplicate = client.post(
            "/v1/access-control/rules",
            json={
                "group_id": group["id"],
                "name": "Duplicate profit",
                "tool_names": ["profit_report_query"],
            },
        )
        assert duplicate.status_code == 409
        assert duplicate.json()["detail"]["code"] == "tool_already_assigned"
        assert duplicate.json()["detail"]["conflicts"][0]["rule_id"] == rule["id"]
        other_group = client.post(
            "/v1/access-control/groups", json={"name": "Operations"}
        ).json()
        reused = client.post(
            "/v1/access-control/rules",
            json={
                "group_id": other_group["id"],
                "name": "Operations profit",
                "tool_names": ["profit_report_query"],
            },
        )
        assert reused.status_code == 201
        assert reused.json()["group_id"] == other_group["id"]
        direct_grant = client.put(
            f"/v1/access-control/groups/{group['id']}/tools",
            json={
                "tool_names": ["profit_report_query", "amazon_finance_query"]
            },
        )
        assert direct_grant.status_code == 200
        assert direct_grant.json()["tool_names"] == [
            "amazon_finance_query", "profit_report_query"
        ]
        renamed = client.put(
            f"/v1/access-control/groups/{group['id']}",
            json={"name": "Finance Operations", "description": "Updated"},
        )
        assert renamed.status_code == 200
        assert renamed.json()["name"] == "Finance Operations"
        assert renamed.json()["description"] == "Updated"
        assert renamed.json()["tool_names"] == [
            "amazon_finance_query", "profit_report_query"
        ]
        cross_group_grant = client.put(
            f"/v1/access-control/groups/{other_group['id']}/tools",
            json={"tool_names": ["profit_report_query"]},
        )
        assert cross_group_grant.status_code == 200
        assert cross_group_grant.json()["tool_names"] == ["profit_report_query"]
        system_tool_rule = client.post(
            "/v1/access-control/rules",
            json={
                "group_id": group["id"],
                "name": "Invalid runtime rule",
                "tool_names": ["delegate_subagent"],
            },
        )
        assert system_tool_rule.status_code == 400
        assert system_tool_rule.json()["detail"]["code"] == "system_tool_not_configurable"
        assert client.put(
            "/v1/access-control/users/alice/groups",
            json={"target_id": group["id"]},
        ).status_code == 200
        assert client.put(
            f"/v1/access-control/groups/{group['id']}/rules",
            json={"target_id": rule["id"]},
        ).status_code == 200

        snapshot = client.get("/v1/access-control").json()
        assert snapshot["configured"] is True
        assert snapshot["users"][0]["group_ids"] == [group["id"]]
        assert set(snapshot["default_tool_names"]) == SYSTEM_DEFAULT_TOOL_NAMES
        assert not (
            {tool["id"] for tool in snapshot["tool_catalog"]}
            & SYSTEM_DEFAULT_TOOL_NAMES
        )

        alice_catalog = client.get(
            "/v1/catalog",
            headers={"X-User-ID": "alice", "X-User-Role": "operator"},
        ).json()
        assert {tool["id"] for tool in alice_catalog["tools"]} == (
            SYSTEM_DEFAULT_TOOL_NAMES | {"profit_report_query"}
            | {"amazon_finance_query"}
        )

        alice_agents = client.get(
            "/v1/agents",
            headers={"X-User-ID": "alice", "X-User-Role": "operator"},
        ).json()
        visible_agent_ids = {item["id"] for item in alice_agents["items"]}
        assert "amazon-finance-analyst" in visible_agent_ids
        assert "profit-analyst" in visible_agent_ids
        assert "erp-analyst" not in visible_agent_ids
        assert client.get(
            "/v1/agents/erp-analyst",
            headers={"X-User-ID": "alice", "X-User-Role": "operator"},
        ).status_code == 404

        assert client.put(
            "/v1/access-control/users/bob",
            json={"id": "bob", "name": "Bob", "enabled": True},
        ).status_code == 200
        bob_agents = client.get(
            "/v1/agents",
            headers={"X-User-ID": "bob", "X-User-Role": "operator"},
        ).json()
        assert not (
            {item["id"] for item in bob_agents["items"]}
            & SPECIALIST_ANALYST_IDS
        )

        admin_catalog = client.get(
            "/v1/catalog",
            headers={"X-User-ID": "unregistered-admin", "X-User-Role": "admin"},
        ).json()
        assert "delegate_subagent" in {
            tool["id"] for tool in admin_catalog["tools"]
        }


def test_permission_denial_response_contains_actionable_hint(tmp_path: Path):
    with TestClient(
        create_app(
            _settings(tmp_path)
        )
    ) as client:
        _create_analytics_connection(client)
        client.put(
            "/v1/access-control/users/alice",
            json={"id": "alice", "name": "Alice", "enabled": True},
        )
        denied = client.post(
            "/v1/amazon-finance/query",
            headers={"X-User-ID": "alice", "X-User-Role": "operator"},
            json={"question": "查询总览", "plan": {"metric": "overview"}},
        )
        assert denied.status_code == 403
        detail = denied.json()["detail"]
        assert detail["code"] == "permission_group_missing"
        assert "管理员" in detail["hint"]


def test_memory_management_api_lifecycle(tmp_path: Path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        created = client.post(
            "/v1/memories",
            json={
                "content": "用户偏好 CAD 报表",
                "key": "report-currency",
                "scope": "profile",
                "kind": "profile",
                "owner_user_id": "alice",
                "importance": 0.8,
            },
        )
        assert created.status_code == 201
        memory_id = created.json()["id"]

        listed = client.get("/v1/memories?owner_user_id=alice").json()
        assert listed["count"] == 1
        assert listed["items"][0]["quality_score"] > 0

        found = client.get(
            "/v1/memories/search",
            params={"query": "报表币种", "owner_user_id": "alice"},
        ).json()
        assert found["items"][0]["id"] == memory_id

        corrected = client.post(
            f"/v1/memories/{memory_id}/correct",
            json={"content": "用户偏好 EUR 报表"},
        )
        assert corrected.status_code == 201
        assert corrected.json()["correction_of"] == memory_id

        profile = client.get("/v1/memories/profiles/alice").json()
        assert profile["attributes"]["report-currency"] == "用户偏好 EUR 报表"

        erased = client.delete("/v1/memories/users/alice/compliance")
        assert erased.status_code == 204
        remaining = client.get(
            "/v1/memories?owner_user_id=alice&include_deleted=true"
        ).json()
        assert remaining["count"] == 2
        assert all(item["content"] == "[deleted]" for item in remaining["items"])
