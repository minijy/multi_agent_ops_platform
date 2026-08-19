from ops_agent.agent_registry import create_agent_registry
from ops_agent.config import Settings
from ops_agent.integrations.tavily.client import TavilyClient, TavilyError
from ops_agent.runtime.agent_tool_policy import resolve_agent_tool_allowlist
from ops_agent.runtime.tools import ToolDefinition, ToolExecutionContext, ToolRegistry
from ops_agent.runtime.web_search_tool import register_web_search_tool
from pydantic import BaseModel
import pytest


def _context(**overrides) -> ToolExecutionContext:
    values = {
        "session_id": "session-a",
        "tenant_id": "tenant-a",
        "user_id": "user-a",
        "role": "operator",
        "agent_id": "function-calling-runtime",
    }
    values.update(overrides)
    return ToolExecutionContext(**values)


class _FakeClient:
    def search(self, query, max_results=5, search_depth="basic"):
        assert query == "Amazon Europe VAT registration"
        assert max_results == 5
        return {
            "results": [
                {
                    "title": "VAT in the UK",
                    "url": "https://www.gov.uk/vat",
                    "content": "Value Added Tax is a tax on most goods and services.",
                    "score": 0.91,
                }
            ]
        }


class _ConfiguredConnectors:
    def execute_tool(self, tenant_id, tool_name, operation, retry_transient=True):
        assert tenant_id == "tenant-a"
        assert tool_name == "web_search"
        return operation(_FakeClient(), object())


class _MissingConnectors:
    def execute_tool(self, tenant_id, tool_name, operation, retry_transient=True):
        raise PermissionError("tenant has no enabled tavily connection")


def test_web_search_returns_cited_results():
    registry = ToolRegistry()
    register_web_search_tool(registry, _ConfiguredConnectors())
    definition = registry.get("web_search")
    result = definition.handler(
        definition.arguments_model(query="Amazon Europe VAT registration"),
        _context(),
    )
    assert result["ok"] is True
    assert result["items"][0]["url"] == "https://www.gov.uk/vat"
    assert "VAT in the UK" in result["summary"]


def test_web_search_unconfigured():
    registry = ToolRegistry()
    register_web_search_tool(registry, _MissingConnectors())
    definition = registry.get("web_search")
    result = definition.handler(
        definition.arguments_model(query="latest amazon news"),
        _context(),
    )
    assert result["ok"] is False
    assert result["configured"] is False
    assert "Tavily" in result["summary"]


def test_coordinator_allowlist_includes_web_search(tmp_path):
    class _Args(BaseModel):
        value: str = "x"

    settings = Settings(_env_file=None, agent_definitions_path=tmp_path / "agents.json")
    agents = create_agent_registry(settings.agent_definitions_path)
    registry = ToolRegistry()
    register_web_search_tool(registry, _MissingConnectors())
    for name in (
        "delegate_subagent",
        "delegate_specialists",
        "load_skill",
        "search_memory",
        "remember_fact",
        "forget_memory",
        "search_knowledge",
    ):
        registry.register(
            ToolDefinition(
                name=name,
                description=name,
                arguments_model=_Args,
                handler=lambda *_args, **_kwargs: {},
                builtin=True,
            )
        )
    allowed = resolve_agent_tool_allowlist(
        agents.runtime_config(), agents, settings, registry
    )
    assert "web_search" in allowed
    assert "web_search" in agents.runtime_config().system_prompt
    analyst = resolve_agent_tool_allowlist(
        agents.analyst_config(), agents, settings, registry
    )
    assert "web_search" not in analyst


def test_tavily_client_maps_auth_errors(monkeypatch):
    class _Response:
        status_code = 401
        text = "invalid"
        reason_phrase = "Unauthorized"

        def json(self):
            return {}

    monkeypatch.setattr(
        "ops_agent.integrations.tavily.client.httpx.post",
        lambda *args, **kwargs: _Response(),
    )
    with pytest.raises(TavilyError, match="API Key 无效"):
        TavilyClient("tvly-test").search("vat")


def test_top_level_session_picks_up_tavily_added_after_session_start(tmp_path):
    from typing import Any

    from ops_agent.connections import create_connection_registry
    from ops_agent.runtime.agent_loop import AgentRuntime
    from ops_agent.runtime.connectors import create_tool_bindings
    from ops_agent.runtime.domain import ModelTurn, RuntimeAgentRequest, ToolCall
    from ops_agent.runtime.model_router import ModelRouter
    from ops_agent.runtime.session_events import SQLiteSessionEventStore
    from ops_agent.runtime.tools import ConnectorAccessGuard, ToolExecutor

    connections = create_connection_registry(
        tmp_path / "connections.json", tmp_path / "secrets.json"
    )
    analytics = connections.create(
        tenant_id="tenant-a",
        connector_type="analytics",
        name="PG",
        values={"dsn": "postgresql://reader@db/analytics"},
    )
    bindings = create_tool_bindings(tmp_path / "bindings.json")
    tools = ToolRegistry()
    register_web_search_tool(tools, _ConfiguredConnectors())

    class _Adapter:
        provider = "fake"
        model_name = "fake-search"
        calls = 0

        def invoke(self, messages: list[dict[str, Any]], tools_schema: list[dict[str, Any]]):
            if any(message.get("role") == "tool" for message in messages):
                return ModelTurn(provider=self.provider, model=self.model_name, content="检索完成")
            self.calls += 1
            if self.calls == 1:
                return ModelTurn(provider=self.provider, model=self.model_name, content="先确认问题")
            return ModelTurn(
                provider=self.provider,
                model=self.model_name,
                tool_calls=[
                    ToolCall(
                        call_id="ws-1",
                        name="web_search",
                        arguments={"query": "Amazon Europe VAT registration"},
                    )
                ],
            )

    runtime = AgentRuntime(
        router=ModelRouter({"fake": _Adapter()}, default_model_id="fake"),
        registry=tools,
        executor=ToolExecutor(
            tools, guards=[ConnectorAccessGuard(bindings, connections)]
        ),
        event_store=SQLiteSessionEventStore(tmp_path / "events.sqlite3"),
        connection_registry=connections,
        tool_bindings=bindings,
    )
    first = runtime.run(
        RuntimeAgentRequest(question="你好", session_id="sess-web"),
        tenant_id="tenant-a",
        user_id="user-a",
        allowed_tools={"web_search", "amazon_finance_query"},
    )
    created = next(
        event
        for event in runtime.event_store.list_events(
            session_id=first.session_id, tenant_id="tenant-a"
        )
        if event.event_type == "session.created"
    )
    assert analytics.id in created.payload["connection_ids"]
    assert not any(
        item.startswith("tenant-a:tavily:") for item in created.payload["connection_ids"]
    )

    tavily = connections.create(
        tenant_id="tenant-a",
        connector_type="tavily",
        name="网页搜索",
        values={"api_key": "tvly-test"},
    )
    second = runtime.run(
        RuntimeAgentRequest(question="搜一下 VAT", session_id="sess-web"),
        tenant_id="tenant-a",
        user_id="user-a",
        allowed_tools={"web_search", "amazon_finance_query"},
    )
    assert second.tool_results[0].ok is True
    assert second.tool_results[0].output["items"][0]["url"] == "https://www.gov.uk/vat"


def test_delegated_session_does_not_gain_tavily_from_live_scope(tmp_path):
    from typing import Any

    from ops_agent.connections import create_connection_registry
    from ops_agent.runtime.agent_loop import AgentRuntime
    from ops_agent.runtime.connectors import create_tool_bindings
    from ops_agent.runtime.domain import ModelTurn, RuntimeAgentRequest, ToolCall
    from ops_agent.runtime.model_router import ModelRouter
    from ops_agent.runtime.session_events import SQLiteSessionEventStore
    from ops_agent.runtime.tools import ConnectorAccessGuard, ToolExecutor

    connections = create_connection_registry(
        tmp_path / "connections.json", tmp_path / "secrets.json"
    )
    analytics = connections.create(
        tenant_id="tenant-a",
        connector_type="analytics",
        name="PG",
        values={"dsn": "postgresql://reader@db/analytics"},
    )
    connections.create(
        tenant_id="tenant-a",
        connector_type="tavily",
        name="网页搜索",
        values={"api_key": "tvly-test"},
    )
    bindings = create_tool_bindings(tmp_path / "bindings.json")
    tools = ToolRegistry()
    register_web_search_tool(tools, _ConfiguredConnectors())

    class _Adapter:
        provider = "fake"
        model_name = "fake-search"

        def invoke(self, messages: list[dict[str, Any]], tools_schema: list[dict[str, Any]]):
            if any(message.get("role") == "tool" for message in messages):
                return ModelTurn(provider=self.provider, model=self.model_name, content="done")
            return ModelTurn(
                provider=self.provider,
                model=self.model_name,
                tool_calls=[
                    ToolCall(
                        call_id="ws-1",
                        name="web_search",
                        arguments={"query": "Amazon Europe VAT registration"},
                    )
                ],
            )

    runtime = AgentRuntime(
        router=ModelRouter({"fake": _Adapter()}, default_model_id="fake"),
        registry=tools,
        executor=ToolExecutor(
            tools, guards=[ConnectorAccessGuard(bindings, connections)]
        ),
        event_store=SQLiteSessionEventStore(tmp_path / "events.sqlite3"),
        connection_registry=connections,
        tool_bindings=bindings,
    )
    response = runtime.run(
        RuntimeAgentRequest(question="搜网页", session_id="child-web"),
        tenant_id="tenant-a",
        user_id="user-a",
        allowed_tools={"web_search", "amazon_finance_query"},
        parent_session_id="parent-web",
        connection_ids=[analytics.id],
    )
    assert response.tool_results[0].ok is False
    assert "outside delegated scope" in (response.tool_results[0].error or "")
