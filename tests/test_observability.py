from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path

import jwt
from fastapi.testclient import TestClient

from ops_agent.api.app import create_app
from ops_agent.config import Settings
from ops_agent.evals import default_eval_path, load_eval_cases, run_eval_case
from ops_agent.evals import _offline_runtime
from ops_agent.runtime.observability import estimate_cost
from ops_agent.runtime.session_events import SessionEvent
from ops_agent.evals import project_replay


def _settings(tmp_path: Path, **overrides) -> Settings:
    values = dict(
        _env_file=None,
        platform_db_path=tmp_path / "platform.sqlite3",
        session_event_path=tmp_path / "session-events.sqlite3",
        runtime_governance_path=tmp_path / "governance.sqlite3",
        runtime_metrics_path=tmp_path / "metrics.sqlite3",
        memory_db_path=tmp_path / "memory.sqlite3",
        agent_definitions_path=tmp_path / "agents.json",
        model_definitions_path=tmp_path / "models.json",
        connection_definitions_path=tmp_path / "connections.json",
        connection_secrets_path=tmp_path / "connection-secrets.json",
        tool_bindings_path=tmp_path / "tool-bindings.json",
        attachment_path=tmp_path / "attachments",
        skills_paths=str(tmp_path / "missing-skills"),
        mcp_config_path=tmp_path / "missing-mcp.json",
    )
    values.update(overrides)
    settings = Settings(**values)
    if not settings.model_definitions_path.exists():
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


def test_estimate_cost_uses_provider_rates():
    assert estimate_cost("mock", "mock-function-calling", 1000, 1000) == 0
    assert estimate_cost("openai", "gpt-4o", 1_000_000, 1_000_000) == 12.5
    assert estimate_cost("qwen", "qwen3.7-plus", 1_000_000, 1_000_000) == 5.6
    assert estimate_cost("deepseek", "deepseek-chat", 1_000_000, 1_000_000) == 0.7


def test_replay_detects_leaked_protocol():
    events = [
        SessionEvent(
            session_id="s",
            sequence=1,
            tenant_id="t",
            user_id="u",
            event_type="model.response",
            payload={"content": '{"finish_reason":"stop","tool_calls":[]}'},
            created_at="2026-01-01T00:00:00+00:00",
        )
    ]
    view = project_replay(events)
    assert view.leaked_protocol is True


def test_golden_eval_cases_pass(tmp_path: Path):
    runtime = _offline_runtime(tmp_path / "eval.sqlite3")
    results = [
        run_eval_case(runtime, case, tenant_id="eval")
        for case in load_eval_cases(default_eval_path())
    ]
    failed = [item for item in results if not item.passed]
    assert failed == []


def test_agent_query_records_runtime_metrics(tmp_path: Path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        created = client.post(
            "/v1/agent/query",
            json={"question": "你好，请介绍当前 Runtime"},
        )
        assert created.status_code == 200
        metrics = client.get("/v1/agent/metrics")
        assert metrics.status_code == 200
        body = metrics.json()
        assert body["turn_count"] >= 1
        assert body["avg_latency_ms"] >= 0
        dashboard = client.get("/v1/dashboard/summary").json()
        assert dashboard["runtime"]["turn_count"] >= 1


def test_jwt_bearer_token_sets_tenant(tmp_path: Path):
    settings = _settings(tmp_path, jwt_secret="phase4-secret-phase4-secret-phase4")
    token = jwt.encode(
        {"sub": "jwt-user", "tenant_id": "tenant-jwt", "role": "admin"},
        "phase4-secret-phase4-secret-phase4",
        algorithm="HS256",
    )
    with TestClient(create_app(settings)) as client:
        created = client.post(
            "/v1/agent/query",
            headers={"Authorization": f"Bearer {token}"},
            json={"question": "你好，请介绍当前 Runtime"},
        )
        assert created.status_code == 200
        own = client.get(
            "/v1/agent/metrics",
            headers={"Authorization": f"Bearer {token}"},
        ).json()
        other = client.get(
            "/v1/agent/metrics",
            headers={"X-Tenant-ID": "tenant-a"},
        ).json()
        assert own["turn_count"] >= 1
        assert other["turn_count"] == 0


def test_jwt_required_rejects_header_only_identity(tmp_path: Path):
    settings = _settings(tmp_path, jwt_secret="phase4-secret-phase4-secret-phase4", jwt_required=True)
    with TestClient(create_app(settings)) as client:
        response = client.get("/v1/agent/metrics")
        assert response.status_code == 401


def test_concurrent_agent_queries(tmp_path: Path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        def once(index: int) -> int:
            return client.post(
                "/v1/agent/query",
                json={"question": f"你好，请介绍当前 Runtime {index}"},
            ).status_code

        with ThreadPoolExecutor(max_workers=4) as pool:
            codes = [
                future.result()
                for future in as_completed(
                    [pool.submit(once, index) for index in range(8)]
                )
            ]
    assert codes == [200] * 8
