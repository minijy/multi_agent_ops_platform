from ops_agent.agent_registry import AgentRegistry, AgentUpdateRequest, create_agent_registry
from ops_agent.config import Settings
from ops_agent.runtime.agent_tool_policy import (
    active_data_query_tools,
    runtime_tool_allowlist,
)
from ops_agent.runtime.tools import ToolDefinition, ToolRegistry
from pydantic import BaseModel


class _Args(BaseModel):
    value: str = "x"


def _register_stub(registry: ToolRegistry, name: str) -> None:
    registry.register(
        ToolDefinition(
            name=name,
            description=name,
            arguments_model=_Args,
            handler=lambda _args, _ctx: {},
            builtin=True,
        )
    )


def test_runtime_excludes_disabled_lingxing_tool(tmp_path):
    settings = Settings(
        analytics_dsn="postgresql://reader@localhost/wenshu",
        agent_definitions_path=tmp_path / "agents.json",
    )
    agent_registry = create_agent_registry(settings.agent_definitions_path)
    agent_registry.update(
        "lingxing-profit-report",
        AgentUpdateRequest(enabled=False),
    )
    tool_registry = ToolRegistry()
    for name in (
        "amazon_finance_query",
        "lingxing_profit_query",
        "profit_report_query",
        "load_skill",
    ):
        _register_stub(tool_registry, name)

    active = active_data_query_tools(agent_registry, settings)
    assert "lingxing_profit_query" not in active
    assert "amazon_finance_query" in active
    assert "profit_report_query" in active

    allowed = runtime_tool_allowlist(
        agent_registry,
        settings,
        tool_registry,
        [],
    )
    assert allowed is not None
    assert "lingxing_profit_query" not in allowed
    assert "profit_report_query" in allowed
