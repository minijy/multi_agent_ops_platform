import os
import uuid
from typing import Any

import pytest
from pydantic import BaseModel

from ops_agent.config import Settings
from ops_agent.runtime.agent_loop import AgentRuntime
from ops_agent.runtime.domain import ModelTurn, RuntimeAgentRequest, ToolCall
from ops_agent.runtime.governance import PostgresRuntimeGovernanceStore
from ops_agent.runtime.model_router import ModelRouter
from ops_agent.runtime.session_events import PostgresSessionEventStore
from ops_agent.runtime.tools import ToolDefinition, ToolExecutor, ToolRegistry


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_TESTS") != "1",
    reason="set RUN_POSTGRES_TESTS=1 to run PostgreSQL integration tests",
)


class NoArguments(BaseModel):
    pass


class ApprovalAdapter:
    provider = "fake"
    model_name = "fake"
    input_modalities = frozenset({"text"})

    def invoke(
        self,
        messages: list[dict[str, Any]],
        _tools: list[dict[str, Any]],
    ) -> ModelTurn:
        if messages[-1].get("role") == "tool":
            return ModelTurn(
                provider="fake",
                model="fake",
                content="postgres approval resumed",
                usage={"total_tokens": 3},
            )
        return ModelTurn(
            provider="fake",
            model="fake",
            tool_calls=[
                ToolCall(call_id="pg-call", name="pg_danger", arguments={})
            ],
            usage={"total_tokens": 3},
        )


def test_postgres_runtime_approval_persists_and_resumes():
    settings = Settings()
    settings.validate_runtime()
    tenant_id = f"governance-{uuid.uuid4()}"
    governance = PostgresRuntimeGovernanceStore(settings.postgres_dsn)
    events = PostgresSessionEventStore(settings.postgres_dsn)
    registry = ToolRegistry()
    executions: list[str] = []
    registry.register(
        ToolDefinition(
            name="pg_danger",
            description="integration test",
            arguments_model=NoArguments,
            handler=lambda _args, _context: executions.append("done") or {"ok": True},
            risk="high",
            requires_approval=True,
            allowed_roles=frozenset({"admin"}),
        )
    )
    runtime = AgentRuntime(
        router=ModelRouter(
            {"fake": ApprovalAdapter()}, default_model_id="fake"
        ),
        registry=registry,
        executor=ToolExecutor(registry),
        event_store=events,
        governance_store=governance,
    )
    try:
        waiting = runtime.run(
            RuntimeAgentRequest(question="postgres governance"),
            tenant_id=tenant_id,
            user_id="pg-user",
            role="admin",
        )
        assert waiting.status == "waiting_approval"
        persisted = governance.get_approval(
            waiting.pending_approval_ids[0], tenant_id
        )
        assert persisted is not None
        assert persisted.status == "pending"

        completed = runtime.decide_approval(
            approval_id=persisted.approval_id,
            tenant_id=tenant_id,
            decided_by="pg-approver",
            approved=True,
            comment="integration",
        )
        assert completed.status == "completed"
        assert completed.answer == "postgres approval resumed"
        assert executions == ["done"]
    finally:
        import psycopg

        with psycopg.connect(settings.postgres_dsn) as connection:
            connection.execute(
                "DELETE FROM agent_tool_approvals WHERE tenant_id=%s",
                (tenant_id,),
            )
            connection.execute(
                "DELETE FROM agent_subagent_tasks WHERE tenant_id=%s",
                (tenant_id,),
            )
            connection.execute(
                "DELETE FROM agent_session_events WHERE tenant_id=%s",
                (tenant_id,),
            )
