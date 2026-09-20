from __future__ import annotations

import pytest

from ops_agent.config import Settings
from ops_agent.runtime.agent_loop import AgentRuntime
from ops_agent.runtime.domain import RuntimeAgentRequest, ToolCall
from ops_agent.runtime.intent_router import (
    HardIntentMatcher,
    ThreeLayerIntentRouter,
    _validate_model_route,
    optional_arguments_are_grounded,
)
from ops_agent.runtime.model_router import ModelRouter
from ops_agent.runtime.session_events import SessionEvent
from ops_agent.runtime.subagents import DelegateSubagentArguments
from ops_agent.runtime.tools import ToolDefinition, ToolExecutor, ToolRegistry


def _schema(name: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": name,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


DELEGATION_SCHEMAS = [
    _schema(
        "delegate_subagent",
        {
            "agent_id": {"type": "string"},
            "objective": {"type": "string"},
            "run_in_background": {"type": "boolean", "default": False},
        },
        ["objective"],
    ),
    _schema(
        "delegate_specialists",
        {"tasks": {"type": "array"}},
        ["tasks"],
    ),
]


def test_hard_match_routes_one_explicit_domain_to_specialist():
    route = HardIntentMatcher().match(
        "查询 2026 年 7 月 Amazon 结算费用 Top 5",
        DELEGATION_SCHEMAS,
    )

    assert route is not None
    assert route.layer == "hard_match"
    assert route.calls[0].name == "delegate_subagent"
    assert route.calls[0].arguments["agent_id"] == "amazon-finance-analyst"


def test_hard_match_batches_multiple_explicit_domains():
    route = HardIntentMatcher().match(
        "查询 Amazon 结算费用，并统计金蝶应收回款",
        DELEGATION_SCHEMAS,
    )

    assert route is not None
    assert route.calls[0].name == "delegate_specialists"
    assert len(route.calls[0].arguments["tasks"]) == 2


def test_small_model_route_rejects_schema_fields_that_do_not_exist():
    schemas = [_schema("inventory_lookup", {"sku": {"type": "string"}}, ["sku"])]
    with pytest.raises(ValueError, match="outside the tool schema"):
        _validate_model_route(
            {
                "decision": "tool_plan",
                "candidate_tool": None,
                "actions": [
                    {
                        "tool": "inventory_lookup",
                        "arguments": {"sku": "A-1", "limit": 5},
                    }
                ],
                "missing_slots": [],
            },
            schemas,
        )


def test_optional_argument_requires_user_value_or_schema_default():
    schemas = [
        _schema(
            "inventory_lookup",
            {
                "sku": {"type": "string"},
                "limit": {"type": "integer"},
                "warehouse": {"type": "string", "default": "main"},
            },
            ["sku"],
        )
    ]
    guessed = ToolCall(call_id="x", name="inventory_lookup", arguments={"sku": "A-1", "limit": 5})
    grounded = ToolCall(
        call_id="y",
        name="inventory_lookup",
        arguments={"sku": "A-1", "limit": 5, "warehouse": "main"},
    )

    assert optional_arguments_are_grounded(guessed, schemas, "查询 A-1 库存") is False
    assert optional_arguments_are_grounded(grounded, schemas, "查询 A-1 库存，最多 5 条") is True


def test_small_model_failure_falls_open_to_coordinator(monkeypatch):
    settings = Settings(_env_file=None, intent_routing_enabled=True)
    router = ThreeLayerIntentRouter(settings)

    def fail(*_args, **_kwargs):
        raise TimeoutError("offline")

    monkeypatch.setattr(router.small_model, "classify", fail)
    route = router.route(query="请解释什么是毛利率", history=[], schemas=[])

    assert route.layer == "coordinator"
    assert route.uses_coordinator is True
    assert "TimeoutError" in route.reason


def test_disabled_router_goes_directly_to_coordinator():
    router = ThreeLayerIntentRouter(Settings(_env_file=None, intent_routing_enabled=False))
    route = router.route(query="查询 Amazon 费用", history=[], schemas=DELEGATION_SCHEMAS)
    assert route.layer == "disabled"
    assert route.uses_coordinator is True


class _MemoryEvents:
    def __init__(self) -> None:
        self.events: list[SessionEvent] = []

    def append(self, *, session_id, tenant_id, user_id, event_type, payload=None):
        event = SessionEvent(
            session_id=session_id,
            sequence=len([item for item in self.events if item.session_id == session_id]) + 1,
            tenant_id=tenant_id,
            user_id=user_id,
            event_type=event_type,
            payload=payload or {},
            created_at="2026-01-01T00:00:00+00:00",
        )
        self.events.append(event)
        return event

    def list_events(self, *, session_id, tenant_id):
        return [
            item
            for item in self.events
            if item.session_id == session_id and item.tenant_id == tenant_id
        ]


class _MustNotInvokeCoordinator:
    provider = "fake"
    model_name = "coordinator"
    input_modalities = frozenset({"text"})

    def invoke(self, _messages, _tools, **_kwargs):
        raise AssertionError("hard-matched request must not invoke Coordinator")


def test_hard_match_short_circuits_coordinator_and_returns_subagent_answer():
    settings = Settings(_env_file=None, intent_routing_enabled=True)
    tools = ToolRegistry()
    tools.register(
        ToolDefinition(
            name="delegate_subagent",
            description="delegate one business domain",
            arguments_model=DelegateSubagentArguments,
            handler=lambda args, _context: {
                "agent_id": args.agent_id,
                "status": "completed",
                "answer": "Amazon 费用分析完成",
            },
            builtin=True,
        )
    )
    events = _MemoryEvents()
    runtime = AgentRuntime(
        router=ModelRouter({"fake": _MustNotInvokeCoordinator()}, default_model_id="fake"),
        registry=tools,
        executor=ToolExecutor(tools),
        event_store=events,
        settings=settings,
        intent_router=ThreeLayerIntentRouter(settings),
    )

    response = runtime.run(
        RuntimeAgentRequest(question="查询 2026 年 7 月 Amazon 费用 Top 5"),
        tenant_id="tenant-a",
        user_id="user-a",
    )

    assert response.answer == "Amazon 费用分析完成"
    assert response.tool_results[0].tool_name == "delegate_subagent"
    routed = next(item for item in events.events if item.event_type == "intent.routed")
    assert routed.payload["layer"] == "hard_match"
