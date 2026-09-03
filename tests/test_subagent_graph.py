from __future__ import annotations

import asyncio
import threading

from ops_agent.config import Settings
from ops_agent.runtime.domain import RuntimeAgentResponse
from ops_agent.runtime.governance import SubagentTaskRecord
from ops_agent.runtime.subagents import (
    LangGraphSubagent,
    SubagentManager,
    SubagentSubmitRequest,
)
from ops_agent.runtime.tools import ToolRegistry


class MemoryTaskStore:
    def __init__(self, record: SubagentTaskRecord) -> None:
        self.records = {record.task_id: record}

    def update_task(self, record: SubagentTaskRecord) -> None:
        self.records[record.task_id] = record

    def create_task(self, record: SubagentTaskRecord) -> None:
        self.records[record.task_id] = record

    def get_task(self, task_id: str, tenant_id: str):
        record = self.records.get(task_id)
        return record if record is not None and record.tenant_id == tenant_id else None

    def list_tasks(self, tenant_id: str, parent_session_id: str | None = None):
        return [
            record
            for record in self.records.values()
            if record.tenant_id == tenant_id
            and (
                parent_session_id is None
                or record.parent_session_id == parent_session_id
            )
        ]


class MemoryEventStore:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def append(self, *, event_type: str, payload: dict, **_kwargs):
        self.events.append((event_type, payload))


class AnswerRuntime:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.agent_registry = None
        self.connection_registry = None

    def run(self, request, **kwargs) -> RuntimeAgentResponse:
        self.calls.append({"request": request, **kwargs})
        return RuntimeAgentResponse(
            session_id=request.session_id,
            answer="子图执行完成",
            provider="fake",
            model="fake",
            event_count=1,
        )


def _record(
    *,
    status: str = "queued",
    task_id: str = "task-1",
    child_session_id: str = "child-session",
) -> SubagentTaskRecord:
    return SubagentTaskRecord(
        task_id=task_id,
        parent_session_id="parent-session",
        child_session_id=child_session_id,
        tenant_id="tenant-a",
        user_id="user-a",
        role="admin",
        objective="分析利润异常",
        status=status,
        depth=1,
        agent_id="profit-analyst",
        allowed_tools=["profit_report_query"],
        token_budget=1000,
        timeout_seconds=30,
        created_at="2026-01-01T00:00:00+00:00",
    )


def test_subagent_is_a_compiled_langgraph_with_isolated_lifecycle() -> None:
    record = _record()
    runtime = AnswerRuntime()
    store = MemoryTaskStore(record)
    events = MemoryEventStore()
    subgraph = LangGraphSubagent(
        runtime=runtime, store=store, event_store=events
    )

    nodes = set(subgraph.graph.get_graph().nodes)
    assert {"prepare", "agent", "finalize"}.issubset(nodes)

    result = subgraph.invoke(record, threading.Event())

    assert result.status == "completed"
    assert result.answer == "子图执行完成"
    assert runtime.calls[0]["agent_id"] == "profit-analyst"
    assert runtime.calls[0]["parent_session_id"] == "parent-session"
    assert runtime.calls[0]["allowed_tools"] == {"profit_report_query"}
    assert [name for name, _payload in events.events] == [
        "subagent.running",
        "subagent.finished",
    ]


def test_subagent_ainvoke_uses_cancel_branch_without_running_agent() -> None:
    async def scenario() -> None:
        record = _record()
        runtime = AnswerRuntime()
        store = MemoryTaskStore(record)
        events = MemoryEventStore()
        subgraph = LangGraphSubagent(
            runtime=runtime, store=store, event_store=events
        )
        cancellation = threading.Event()
        cancellation.set()

        result = await subgraph.ainvoke(record, cancellation)

        assert result.status == "cancelled"
        assert runtime.calls == []
        assert [name for name, _payload in events.events] == [
            "subagent.finished"
        ]

    asyncio.run(scenario())


def test_subagent_astream_exposes_framework_node_updates() -> None:
    async def scenario() -> None:
        record = _record()
        subgraph = LangGraphSubagent(
            runtime=AnswerRuntime(),
            store=MemoryTaskStore(record),
            event_store=MemoryEventStore(),
        )

        updates = [
            update
            async for update in subgraph.astream(record, threading.Event())
        ]

        assert [next(iter(update)) for update in updates] == [
            "prepare",
            "agent",
            "finalize",
        ]

    asyncio.run(scenario())


def test_multiple_ainvoke_calls_run_as_concurrent_langgraph_tasks() -> None:
    class BarrierRuntime(AnswerRuntime):
        barrier = threading.Barrier(2)

        def run(self, request, **kwargs) -> RuntimeAgentResponse:
            self.barrier.wait(timeout=2)
            return super().run(request, **kwargs)

    async def scenario() -> None:
        first = _record()
        second = _record(task_id="task-2", child_session_id="child-session-2")
        runtime = BarrierRuntime()
        store = MemoryTaskStore(first)
        store.create_task(second)
        subgraph = LangGraphSubagent(
            runtime=runtime,
            store=store,
            event_store=MemoryEventStore(),
        )

        results = await asyncio.gather(
            subgraph.ainvoke(first, threading.Event()),
            subgraph.ainvoke(second, threading.Event()),
        )

        assert [result.status for result in results] == ["completed", "completed"]
        assert len(runtime.calls) == 2

    asyncio.run(scenario())


def test_inline_manager_schedules_compiled_subgraph_with_ainvoke(tmp_path) -> None:
    placeholder = _record()
    store = MemoryTaskStore(placeholder)
    events = MemoryEventStore()
    runtime = AnswerRuntime()
    manager = SubagentManager(
        runtime=runtime,
        registry=ToolRegistry(),
        event_store=events,
        governance_store=store,
        settings=Settings(
            _env_file=None,
            subagent_queue_backend="inline",
            subagent_worker_count=2,
            agent_definitions_path=tmp_path / "agents.json",
        ),
    )
    try:
        task = manager.submit(
            SubagentSubmitRequest(
                objective="执行异步子图",
                parent_session_id="parent-async",
            ),
            tenant_id="tenant-a",
            user_id="user-a",
            role="admin",
        )

        result = manager.wait(task.task_id, "tenant-a", timeout=5)

        assert result.status == "completed"
        assert result.answer == "子图执行完成"
        assert manager.pool is None
        assert manager.subgraph.graph.get_graph().nodes["agent"] is not None
    finally:
        manager.shutdown()
