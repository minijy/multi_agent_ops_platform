from ops_agent.agent_registry import AgentUpdateRequest, create_agent_registry
from ops_agent.config import Settings
from ops_agent.connections import create_connection_registry
from ops_agent.runtime.agent_tool_policy import (
    coordinator_delegation_prompt,
    active_data_query_tools,
    data_tool_usage_prompt,
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
        agent_definitions_path=tmp_path / "agents.json",
    )
    agent_registry = create_agent_registry(settings.agent_definitions_path)
    agent_registry.update(
        "lingxing-profit-report",
        AgentUpdateRequest(enabled=False),
    )
    connections = create_connection_registry(
        tmp_path / "connections.json", tmp_path / "connection-secrets.json"
    )
    connections.create(
        tenant_id="tenant-a",
        connector_type="analytics",
        name="测试数据库",
        values={"dsn": "postgresql://reader@localhost/wenshu"},
    )
    tool_registry = ToolRegistry()
    for name in (
        "amazon_finance_query",
        "lingxing_profit_query",
        "profit_report_query",
        "load_skill",
    ):
        _register_stub(tool_registry, name)

    active = active_data_query_tools(
        agent_registry, settings, connections, "tenant-a"
    )
    assert "lingxing_profit_query" not in active
    assert "amazon_finance_query" in active
    assert "profit_report_query" in active

    allowed = runtime_tool_allowlist(
        agent_registry,
        settings,
        tool_registry,
        [],
        connections,
        "tenant-a",
    )
    assert allowed is not None
    assert "lingxing_profit_query" not in allowed
    assert "profit_report_query" in allowed


def test_coordinator_prompt_only_lists_accessible_specialists(tmp_path):
    settings = Settings(
        _env_file=None,
        agent_definitions_path=tmp_path / "agents.json",
    )
    registry = create_agent_registry(settings.agent_definitions_path)

    prompt = coordinator_delegation_prompt(
        registry,
        "specialized_parallel",
        {"kingdee_cloud_query"},
    )

    assert "erp-analyst" in prompt
    assert "amazon-finance-analyst" not in prompt
    assert "profit-analyst" not in prompt


def test_data_tool_prompt_chooses_accessible_equivalent_and_hides_table_name():
    prompt = data_tool_usage_prompt(
        frozenset({"lingxing_profit_query", "profit_report_query"})
    )

    assert "首选调用失败时自动改用分析仓" in prompt
    assert "不要向用户追问" in prompt
    assert "lingxing_profit_order_transactions" not in prompt
    assert "数据来源" in prompt
