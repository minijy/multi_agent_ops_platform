import json

from ops_agent.agent_registry import create_agent_registry
from ops_agent.config import Settings
from ops_agent.agent_roles import (
    ANALYST_AGENT_ID,
    AMAZON_FINANCE_ANALYST_ID,
    COORDINATOR_AGENT_ID,
    ERP_ANALYST_ID,
    PROFIT_ANALYST_ID,
    SYSTEM_DEFAULT_TOOL_NAMES,
)
from ops_agent.runtime.agent_tool_policy import resolve_agent_tool_allowlist
from ops_agent.runtime.tools import ToolDefinition, ToolRegistry
from pydantic import BaseModel
import pytest

from ops_agent.runtime.agent_loop import AgentRuntime
from ops_agent.runtime.domain import ModelTurn
from ops_agent.runtime.domain import ToolCall
from ops_agent.runtime.governance import SQLiteRuntimeGovernanceStore
from ops_agent.runtime.model_router import ModelRouter
from ops_agent.runtime.session_events import SQLiteSessionEventStore
from ops_agent.runtime.subagents import (
    DelegateSpecialistsArguments,
    SubagentManager,
    SubagentSubmitRequest,
    register_subagent_tool,
)
from ops_agent.runtime.tools import ToolExecutionContext, ToolExecutor
from ops_agent.connections import create_connection_registry


class _Args(BaseModel):
    value: str = "x"


class AnswerAdapter:
    provider = "fake"
    model_name = "fake-answer"
    input_modalities = frozenset({"text"})

    def invoke(self, _messages, _tools):
        return ModelTurn(
            provider=self.provider,
            model=self.model_name,
            content="子任务完成",
            usage={"total_tokens": 8},
        )


def _register(registry: ToolRegistry, name: str, *, approval: bool = False) -> None:
    registry.register(
        ToolDefinition(
            name=name,
            description=name,
            arguments_model=_Args,
            handler=lambda _args, _ctx: {"ok": True},
            builtin=True,
            requires_approval=approval,
        )
    )


def _analytics_connections(tmp_path):
    connections = create_connection_registry(
        tmp_path / "connections.json", tmp_path / "connection-secrets.json"
    )
    connections.create(
        tenant_id="tenant-a",
        connector_type="analytics",
        name="测试 PostgreSQL",
        values={"dsn": "postgresql://reader@localhost/wenshu"},
    )
    return connections


def test_coordinator_and_analyst_use_different_tool_allowlists(tmp_path):
    settings = Settings(
        _env_file=None,
        agent_definitions_path=tmp_path / "agents.json",
    )
    agent_registry = create_agent_registry(settings.agent_definitions_path)
    connections = _analytics_connections(tmp_path)
    tools = ToolRegistry()
    for name in (
        "delegate_subagent",
        "delegate_specialists",
        "load_skill",
        "sandbox_read_only",
        "amazon_finance_query",
        "profit_report_query",
        "lingxing_profit_query",
        "kingdee_cloud_query",
    ):
        _register(tools, name)

    coordinator = resolve_agent_tool_allowlist(
        agent_registry.runtime_config(), agent_registry, settings, tools,
        connections, "tenant-a",
    )
    analyst = resolve_agent_tool_allowlist(
        agent_registry.analyst_config(), agent_registry, settings, tools,
        connections, "tenant-a",
    )
    assert coordinator == {"delegate_subagent", "load_skill"}
    assert "amazon_finance_query" not in coordinator
    assert "delegate_subagent" not in analyst
    assert "amazon_finance_query" in analyst
    assert "profit_report_query" in analyst


def test_specialist_mode_repairs_stale_coordinator_override(tmp_path):
    path = tmp_path / "agents.json"
    path.write_text(
        json.dumps(
            {
                COORDINATOR_AGENT_ID: {
                    "id": COORDINATOR_AGENT_ID,
                    "strict_tool_allowlist": True,
                    "allowed_tools": ["delegate_subagent", "load_skill"],
                    "system_prompt": (
                        "你是 Coordinator。调用 delegate_subagent；"
                        "agent_id 必须是 analyst。"
                    ),
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    settings = Settings(
        _env_file=None,
        analyst_mode="specialized_parallel",
        agent_definitions_path=path,
    )
    agents = create_agent_registry(path)
    tools = ToolRegistry()
    for name in ("delegate_subagent", "delegate_specialists", "load_skill"):
        _register(tools, name)

    allowed = resolve_agent_tool_allowlist(
        agents.runtime_config(), agents, settings, tools
    )

    assert "delegate_specialists" in allowed
    assert "delegate_subagent" not in allowed
    assert "agent_id 必须是 analyst" not in agents.runtime_config().system_prompt


def test_specialist_analysts_have_distinct_tool_allowlists(tmp_path):
    settings = Settings(
        _env_file=None,
        agent_definitions_path=tmp_path / "agents.json",
    )
    agent_registry = create_agent_registry(settings.agent_definitions_path)
    connections = _analytics_connections(tmp_path)
    tools = ToolRegistry()
    for name in (
        "delegate_subagent",
        "load_skill",
        "amazon_finance_query",
        "profit_report_query",
        "lingxing_profit_query",
        "kingdee_cloud_query",
    ):
        _register(tools, name)

    amazon = resolve_agent_tool_allowlist(
        agent_registry.get(AMAZON_FINANCE_ANALYST_ID), agent_registry, settings, tools,
        connections, "tenant-a",
    )
    profit = resolve_agent_tool_allowlist(
        agent_registry.get(PROFIT_ANALYST_ID), agent_registry, settings, tools,
        connections, "tenant-a",
    )
    erp = resolve_agent_tool_allowlist(
        agent_registry.get(ERP_ANALYST_ID), agent_registry, settings, tools,
        connections, "tenant-a",
    )
    assert amazon == {"load_skill", "amazon_finance_query"}
    assert set(agent_registry.get(PROFIT_ANALYST_ID).allowed_tools) == {
        "load_skill",
        "lingxing_profit_query",
        "profit_report_query",
    }
    assert profit == {"load_skill", "profit_report_query"}
    assert set(agent_registry.get(ERP_ANALYST_ID).allowed_tools) == {
        "load_skill",
        "kingdee_cloud_query",
    }
    assert erp == {"load_skill"}


def test_specialized_mode_rejects_general_and_limits_parallel_tasks(tmp_path):
    settings = Settings(
        _env_file=None,
        analyst_mode="specialized_parallel",
        subagent_queue_backend="db",
        agent_definitions_path=tmp_path / "agents.json",
    )
    agent_registry = create_agent_registry(settings.agent_definitions_path)
    connections = _analytics_connections(tmp_path)
    tools = ToolRegistry()
    _register(tools, "load_skill")
    events = SQLiteSessionEventStore(tmp_path / "events.sqlite3")
    runtime = AgentRuntime(
        router=ModelRouter({"fake": AnswerAdapter()}, default_model_id="fake"),
        registry=tools,
        executor=ToolExecutor(tools),
        event_store=events,
        settings=settings,
        agent_registry=agent_registry,
        connection_registry=connections,
    )
    manager = SubagentManager(
        runtime=runtime,
        registry=tools,
        event_store=events,
        governance_store=SQLiteRuntimeGovernanceStore(tmp_path / "gov.sqlite3"),
        settings=settings,
    )
    try:
        with pytest.raises(PermissionError, match="general analyst is disabled"):
            manager.submit(
                SubagentSubmitRequest(
                    agent_id=ANALYST_AGENT_ID,
                    objective="通用分析不应运行",
                    parent_session_id="parent-specialized",
                ),
                tenant_id="tenant-a",
                user_id="user-a",
                role="admin",
            )

        for agent_id in (
            AMAZON_FINANCE_ANALYST_ID,
            PROFIT_ANALYST_ID,
            ERP_ANALYST_ID,
        ):
            manager.submit(
                SubagentSubmitRequest(
                    agent_id=agent_id,
                    objective=f"运行 {agent_id}",
                    parent_session_id="parent-specialized",
                ),
                tenant_id="tenant-a",
                user_id="user-a",
                role="admin",
            )

        with pytest.raises(ValueError, match="parallel limit reached: 3"):
            manager.submit(
                SubagentSubmitRequest(
                    agent_id=AMAZON_FINANCE_ANALYST_ID,
                    objective="第四个任务应被拒绝",
                    parent_session_id="parent-specialized",
                ),
                tenant_id="tenant-a",
                user_id="user-a",
                role="admin",
            )
    finally:
        manager.shutdown()


def test_delegate_specialists_returns_all_parallel_results(tmp_path):
    settings = Settings(
        _env_file=None,
        analyst_mode="specialized_parallel",
        subagent_worker_count=3,
        agent_definitions_path=tmp_path / "agents.json",
    )
    agent_registry = create_agent_registry(settings.agent_definitions_path)
    tools = ToolRegistry()
    _register(tools, "load_skill")
    events = SQLiteSessionEventStore(tmp_path / "events.sqlite3")
    runtime = AgentRuntime(
        router=ModelRouter({"fake": AnswerAdapter()}, default_model_id="fake"),
        registry=tools,
        executor=ToolExecutor(tools),
        event_store=events,
        settings=settings,
        agent_registry=agent_registry,
    )
    manager = SubagentManager(
        runtime=runtime,
        registry=tools,
        event_store=events,
        governance_store=SQLiteRuntimeGovernanceStore(tmp_path / "gov.sqlite3"),
        settings=settings,
    )
    register_subagent_tool(tools, manager)
    try:
        result = ToolExecutor(tools).execute(
            ToolCall(
                call_id="parallel-specialists",
                name="delegate_specialists",
                arguments={
                    "tasks": [
                        {"agent_id": AMAZON_FINANCE_ANALYST_ID, "objective": "查结算"},
                        {"agent_id": PROFIT_ANALYST_ID, "objective": "查利润"},
                        {"agent_id": ERP_ANALYST_ID, "objective": "查应收"},
                    ],
                    "timeout_seconds": 10,
                },
            ),
            ToolExecutionContext(
                session_id="parent-batch",
                tenant_id="tenant-a",
                user_id="user-a",
                role="admin",
            ),
        )
        assert result.ok is True
        assert result.output["count"] == 3
        assert {item["agent_id"] for item in result.output["tasks"]} == {
            AMAZON_FINANCE_ANALYST_ID,
            PROFIT_ANALYST_ID,
            ERP_ANALYST_ID,
        }
        assert {item["status"] for item in result.output["tasks"]} == {"completed"}
        assert all(item["answer"] == "子任务完成" for item in result.output["tasks"])
        assert all("allowed_tools" not in item for item in result.output["tasks"])
    finally:
        manager.shutdown()


def test_delegate_specialists_allows_parallel_same_role(tmp_path):
    settings = Settings(
        _env_file=None,
        analyst_mode="specialized_parallel",
        subagent_worker_count=3,
        agent_definitions_path=tmp_path / "agents.json",
    )
    agent_registry = create_agent_registry(settings.agent_definitions_path)
    tools = ToolRegistry()
    _register(tools, "load_skill")
    events = SQLiteSessionEventStore(tmp_path / "events.sqlite3")
    runtime = AgentRuntime(
        router=ModelRouter({"fake": AnswerAdapter()}, default_model_id="fake"),
        registry=tools,
        executor=ToolExecutor(tools),
        event_store=events,
        settings=settings,
        agent_registry=agent_registry,
    )
    manager = SubagentManager(
        runtime=runtime,
        registry=tools,
        event_store=events,
        governance_store=SQLiteRuntimeGovernanceStore(tmp_path / "gov.sqlite3"),
        settings=settings,
    )
    register_subagent_tool(tools, manager)
    try:
        result = ToolExecutor(tools).execute(
            ToolCall(
                call_id="parallel-profit",
                name="delegate_specialists",
                arguments={
                    "tasks": [
                        {"agent_id": PROFIT_ANALYST_ID, "objective": "查询一月利润"},
                        {"agent_id": PROFIT_ANALYST_ID, "objective": "查询二月利润"},
                    ]
                },
            ),
            ToolExecutionContext(
                session_id="parent-profit",
                tenant_id="tenant-a",
                user_id="user-a",
                role="admin",
            ),
        )
        assert result.ok is True
        assert result.output["count"] == 2
        assert [item["agent_id"] for item in result.output["tasks"]] == [
            PROFIT_ANALYST_ID,
            PROFIT_ANALYST_ID,
        ]
    finally:
        manager.shutdown()


def test_delegate_subagent_requires_analyst_role(tmp_path):
    settings = Settings(
        _env_file=None,
        agent_definitions_path=tmp_path / "agents.json",
        subagent_worker_count=1,
    )
    agent_registry = create_agent_registry(settings.agent_definitions_path)
    connections = _analytics_connections(tmp_path)
    tools = ToolRegistry()
    for name in (
        "delegate_subagent",
        "load_skill",
        "sandbox_read_only",
        "amazon_finance_query",
        "profit_report_query",
    ):
        _register(tools, name)
    events = SQLiteSessionEventStore(tmp_path / "events.sqlite3")
    runtime = AgentRuntime(
        router=ModelRouter({"fake": AnswerAdapter()}, default_model_id="fake"),
        registry=tools,
        executor=ToolExecutor(tools),
        event_store=events,
        settings=settings,
        agent_registry=agent_registry,
        connection_registry=connections,
    )
    manager = SubagentManager(
        runtime=runtime,
        registry=tools,
        event_store=events,
        governance_store=SQLiteRuntimeGovernanceStore(tmp_path / "gov.sqlite3"),
        settings=settings,
    )
    try:
        task = manager.submit(
            SubagentSubmitRequest(
                agent_id=ANALYST_AGENT_ID,
                objective="查 7 月费用",
                parent_session_id="parent-1",
            ),
            tenant_id="tenant-a",
            user_id="user-a",
            role="admin",
        )
        assert task.agent_id == ANALYST_AGENT_ID
        assert "amazon_finance_query" in task.allowed_tools
        assert "delegate_subagent" not in task.allowed_tools

        with pytest.raises(PermissionError, match="not delegatable"):
            manager.submit(
                SubagentSubmitRequest(
                    agent_id=COORDINATOR_AGENT_ID,
                    objective="不能委派给自己",
                    parent_session_id="parent-1",
                ),
                tenant_id="tenant-a",
                user_id="user-a",
                role="admin",
            )
        with pytest.raises(PermissionError, match="not delegatable"):
            manager.submit(
                SubagentSubmitRequest(
                    agent_id="amazon-finance-query",
                    objective="假 Agent 不能接任务",
                    parent_session_id="parent-1",
                ),
                tenant_id="tenant-a",
                user_id="user-a",
                role="admin",
            )
        with pytest.raises(PermissionError, match="disabled in general mode"):
            manager.submit(
                SubagentSubmitRequest(
                    agent_id=AMAZON_FINANCE_ANALYST_ID,
                    objective="专业 Agent 在通用模式下不可运行",
                    parent_session_id="parent-1",
                ),
                tenant_id="tenant-a",
                user_id="user-a",
                role="admin",
            )
    finally:
        manager.shutdown()


def test_coordinator_prompt_does_not_list_query_tools(tmp_path):
    settings = Settings(
        _env_file=None,
        agent_definitions_path=tmp_path / "agents.json",
    )
    agent_registry = create_agent_registry(settings.agent_definitions_path)
    tools = ToolRegistry()
    _register(tools, "delegate_subagent")
    _register(tools, "delegate_specialists")
    events = SQLiteSessionEventStore(tmp_path / "events.sqlite3")
    runtime = AgentRuntime(
        router=ModelRouter({"fake": AnswerAdapter()}, default_model_id="fake"),
        registry=tools,
        executor=ToolExecutor(tools),
        event_store=events,
        settings=settings,
        agent_registry=agent_registry,
    )
    coordinator_prompt = runtime._base_system_prompt(
        {"delegate_subagent"}, agent_id=COORDINATOR_AGENT_ID
    )
    analyst_prompt = runtime._base_system_prompt(
        {"amazon_finance_query"}, agent_id=ANALYST_AGENT_ID
    )
    assert "agent_id 填 analyst" in coordinator_prompt
    assert "amazon_finance_query" not in coordinator_prompt
    assert "amazon_finance_query" in analyst_prompt

    settings.analyst_mode = "specialized_parallel"
    specialist_tools = resolve_agent_tool_allowlist(
        agent_registry.runtime_config(), agent_registry, settings, tools
    )
    assert "delegate_specialists" in specialist_tools
    assert "delegate_subagent" not in specialist_tools
    specialist_prompt = runtime._base_system_prompt(
        {"delegate_subagent"}, agent_id=COORDINATOR_AGENT_ID
    )
    assert AMAZON_FINANCE_ANALYST_ID in specialist_prompt
    assert PROFIT_ANALYST_ID in specialist_prompt
    assert ERP_ANALYST_ID in specialist_prompt
    assert "agent_id 必须是 analyst" not in specialist_prompt
    assert "不要按月份" in specialist_prompt


def test_runtime_normalizes_legacy_delegations_into_bounded_batches(tmp_path):
    settings = Settings(
        _env_file=None,
        analyst_mode="specialized_parallel",
        agent_definitions_path=tmp_path / "agents.json",
    )
    tools = ToolRegistry()
    _register(tools, "delegate_specialists")
    runtime = AgentRuntime(
        router=ModelRouter({"fake": AnswerAdapter()}, default_model_id="fake"),
        registry=tools,
        executor=ToolExecutor(tools),
        event_store=SQLiteSessionEventStore(tmp_path / "events.sqlite3"),
        settings=settings,
        agent_registry=create_agent_registry(settings.agent_definitions_path),
    )
    calls = [
        ToolCall(
            call_id=f"month-{month}",
            name="delegate_subagent",
            arguments={
                "agent_id": PROFIT_ANALYST_ID,
                "objective": f"查询 2026-{month:02d} 利润",
            },
        )
        for month in range(1, 6)
    ]

    normalized, original_count, batch_count = (
        runtime._normalize_specialist_delegations(
            calls,
            ToolExecutionContext(
                session_id="parent",
                tenant_id="tenant-a",
                user_id="user-a",
                role="admin",
                allowed_tool_names=frozenset({"delegate_specialists"}),
            ),
        )
    )

    assert original_count == 5
    assert batch_count == 2
    assert [call.name for call in normalized] == [
        "delegate_specialists",
        "delegate_specialists",
    ]
    assert [len(call.arguments["tasks"]) for call in normalized] == [3, 2]
    assert all(
        task["agent_id"] == PROFIT_ANALYST_ID
        for call in normalized
        for task in call.arguments["tasks"]
    )


def test_runtime_normalizes_rich_objectives_and_merges_same_specialist(tmp_path):
    settings = Settings(
        _env_file=None,
        analyst_mode="specialized_parallel",
        agent_definitions_path=tmp_path / "agents.json",
    )
    tools = ToolRegistry()
    runtime = AgentRuntime(
        router=ModelRouter({"fake": AnswerAdapter()}, default_model_id="fake"),
        registry=tools,
        executor=ToolExecutor(tools),
        event_store=SQLiteSessionEventStore(tmp_path / "events.sqlite3"),
        settings=settings,
        agent_registry=create_agent_registry(settings.agent_definitions_path),
    )
    calls = [
        ToolCall(
            call_id="rich-specialists",
            name="delegate_specialists",
            arguments={
                "tasks": [
                    {
                        "agent_id": PROFIT_ANALYST_ID,
                        "objective": {
                            "content": "汇总 2026 年 1-7 月每个 MSKU 的销量",
                            "scopes": ["user"],
                        },
                    },
                    {
                        "agent_id": PROFIT_ANALYST_ID,
                        "objective": {
                            "content": "汇总 2026 年 1-7 月每个 MSKU 的利润",
                            "scopes": ["user"],
                        },
                    },
                ]
            },
        )
    ]

    normalized, objective_count, merged_count = (
        runtime._canonicalize_delegation_arguments(calls)
    )

    assert objective_count == 2
    assert merged_count == 1
    assert len(normalized[0].arguments["tasks"]) == 1
    objective = normalized[0].arguments["tasks"][0]["objective"]
    assert isinstance(objective, str)
    assert "销量" in objective
    assert "利润" in objective
    DelegateSpecialistsArguments.model_validate(normalized[0].arguments)


def test_session_snapshot_refreshes_orchestration_tools_after_mode_switch():
    current = {
        "delegate_specialists",
        "search_memory",
        "profit_report_query",
    }
    old_general_snapshot = {
        "delegate_subagent",
        "search_memory",
        "profit_report_query",
    }

    merged = AgentRuntime._merge_session_tool_snapshot(
        current, old_general_snapshot
    )

    assert "delegate_specialists" in merged
    assert "delegate_subagent" not in merged
    assert "search_memory" in merged
    assert "profit_report_query" in merged
    assert merged <= (old_general_snapshot | SYSTEM_DEFAULT_TOOL_NAMES)


def test_general_mode_repairs_stale_specialist_delegation_call():
    calls = [
        ToolCall(
            call_id="stale-specialist-call",
            name="delegate_specialists",
            arguments={
                "tasks": [
                    {
                        "agent_id": PROFIT_ANALYST_ID,
                        "objective": "查询 2026 年 5 月毛利率",
                    },
                    {
                        "agent_id": AMAZON_FINANCE_ANALYST_ID,
                        "objective": "核对 2026 年 5 月结算费用",
                    },
                ],
                "timeout_seconds": 120,
            },
        )
    ]

    repaired, count = AgentRuntime._repair_delegation_mode(
        calls, {"delegate_subagent", "search_memory"}
    )

    assert count == 1
    assert repaired[0].name == "delegate_subagent"
    assert repaired[0].arguments["agent_id"] == ANALYST_AGENT_ID
    assert repaired[0].arguments["run_in_background"] is False
    assert "5 月毛利率" in repaired[0].arguments["objective"]
    assert "5 月结算费用" in repaired[0].arguments["objective"]


def test_subagent_rejects_foreign_connection_and_widened_resource_scope(tmp_path):
    settings = Settings(
        _env_file=None,
        agent_definitions_path=tmp_path / "agents.json",
        subagent_worker_count=1,
    )
    agent_registry = create_agent_registry(settings.agent_definitions_path)
    connections = create_connection_registry(
        tmp_path / "connections.json", tmp_path / "secrets.json"
    )
    own = connections.upsert(
        tenant_id="tenant-a",
        connector_type="analytics",
        values={"dsn": "postgresql://own"},
        resource_scopes={"store_names": ["store-a"]},
    )
    foreign = connections.upsert(
        tenant_id="tenant-b",
        connector_type="analytics",
        values={"dsn": "postgresql://foreign"},
        resource_scopes={"store_names": ["store-b"]},
    )
    tools = ToolRegistry()
    _register(tools, "amazon_finance_query")
    events = SQLiteSessionEventStore(tmp_path / "events.sqlite3")
    runtime = AgentRuntime(
        router=ModelRouter({"fake": AnswerAdapter()}, default_model_id="fake"),
        registry=tools,
        executor=ToolExecutor(tools),
        event_store=events,
        settings=settings,
        agent_registry=agent_registry,
        connection_registry=connections,
    )
    manager = SubagentManager(
        runtime=runtime,
        registry=tools,
        event_store=events,
        governance_store=SQLiteRuntimeGovernanceStore(tmp_path / "gov.sqlite3"),
        settings=settings,
    )
    try:
        with pytest.raises(PermissionError, match="connections are not visible"):
            manager.submit(
                SubagentSubmitRequest(
                    objective="查询",
                    parent_session_id="parent",
                    connection_ids=[foreign.id],
                ),
                tenant_id="tenant-a",
                user_id="user-a",
                role="admin",
            )
        with pytest.raises(PermissionError, match="resource scope is not visible"):
            manager.submit(
                SubagentSubmitRequest(
                    objective="查询",
                    parent_session_id="parent",
                    connection_ids=[own.id],
                    resource_scope={"store_names": ["store-b"]},
                ),
                tenant_id="tenant-a",
                user_id="user-a",
                role="admin",
            )
        task = manager.submit(
            SubagentSubmitRequest(
                objective="查询",
                parent_session_id="parent",
                connection_ids=[own.id],
                resource_scope={"store_names": ["store-a"]},
            ),
            tenant_id="tenant-a",
            user_id="user-a",
            role="admin",
        )
        manager.wait(task.task_id, "tenant-a", timeout=2)
        child_events = events.list_events(
            session_id=task.child_session_id, tenant_id="tenant-a"
        )
        created = next(
            item for item in child_events if item.event_type == "session.created"
        )
        assert created.payload["connection_ids"] == [own.id]
        assert created.payload["resource_scope"] == {
            "store_names": ["store-a"]
        }
    finally:
        manager.shutdown()
