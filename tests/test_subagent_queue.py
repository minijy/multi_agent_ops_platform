from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from ops_agent.agent_registry import create_agent_registry
from ops_agent.model_registry import create_model_registry
from ops_agent.config import Settings
from ops_agent.runtime.agent_loop import AgentRuntime
from ops_agent.runtime.domain import ModelTurn
from ops_agent.runtime.governance import SQLiteRuntimeGovernanceStore
from ops_agent.runtime.model_router import ModelRouter
from ops_agent.runtime.session_events import SQLiteSessionEventStore
from ops_agent.runtime.subagent_worker import SubagentQueueWorker
from ops_agent.runtime.subagents import SubagentManager, SubagentSubmitRequest
from ops_agent.runtime.tools import ToolExecutor, ToolRegistry
from ops_agent.runtime.stack import RuntimeStack


class AnswerAdapter:
    provider = "fake"
    model_name = "fake-answer"
    input_modalities = frozenset({"text"})

    def invoke(self, _messages, _tools):
        return ModelTurn(
            provider=self.provider,
            model=self.model_name,
            content="外部队列完成",
            usage={"total_tokens": 12},
        )


class SlowAdapter:
    provider = "fake"
    model_name = "fake-slow"
    input_modalities = frozenset({"text"})

    def invoke(self, _messages, _tools):
        time.sleep(1.5)
        return ModelTurn(
            provider=self.provider,
            model=self.model_name,
            content="should cancel",
            usage={"total_tokens": 5},
        )


def _components(tmp_path: Path, adapter: Any):
    governance = SQLiteRuntimeGovernanceStore(tmp_path / "governance.sqlite3")
    events = SQLiteSessionEventStore(tmp_path / "events.sqlite3")
    registry = ToolRegistry()
    runtime = AgentRuntime(
        router=ModelRouter({"fake": adapter}, default_model_id="fake"),
        registry=registry,
        executor=ToolExecutor(registry),
        event_store=events,
        governance_store=governance,
    )
    settings = Settings(
        _env_file=None,
        agent_definitions_path=tmp_path / "agent_definitions.json",
        subagent_queue_backend="db",
        subagent_worker_count=1,
        subagent_default_timeout_seconds=10,
        subagent_default_token_budget=300,
        subagent_lease_seconds=5,
        subagent_lease_renew_seconds=0.2,
        subagent_worker_poll_seconds=0.05,
        subagent_max_attempts=3,
    )
    manager = SubagentManager(
        runtime=runtime,
        registry=registry,
        event_store=events,
        governance_store=governance,
        settings=settings,
    )
    stack = RuntimeStack(
        settings=settings,
        agent_registry=create_agent_registry(settings.agent_definitions_path),
        model_registry=create_model_registry(settings.model_definitions_path, settings),
        tool_registry=registry,
        skill_registry=None,  # type: ignore[arg-type]
        mcp_manager=None,  # type: ignore[arg-type]
        session_events=events,
        metrics_store=None,
        governance_store=governance,
        attachment_store=None,  # type: ignore[arg-type]
        sandbox_runner=None,  # type: ignore[arg-type]
        agent_runtime=runtime,
        subagent_manager=manager,
    )
    return manager, governance, events, stack, settings


def test_db_queue_claim_and_worker_completes(tmp_path: Path):
    manager, governance, events, stack, settings = _components(tmp_path, AnswerAdapter())
    assert manager.pool is None
    task = manager.submit(
        SubagentSubmitRequest(
            objective="外部队列任务",
            parent_session_id="parent-db",
        ),
        tenant_id="tenant-a",
        user_id="user-a",
        role="admin",
    )
    assert task.status == "queued"
    assert governance.get_task(task.task_id, "tenant-a").status == "queued"

    stop = threading.Event()
    worker = SubagentQueueWorker(stack=stack, worker_id="worker-1", stop_event=stop)
    thread = threading.Thread(target=worker.run_forever, daemon=True)
    thread.start()
    try:
        resolved = manager.wait(task.task_id, "tenant-a", timeout=5)
        assert resolved.status == "completed"
        assert resolved.answer == "外部队列完成"
        assert resolved.worker_id is None
        assert resolved.attempt == 1
        parent_events = [
            item.event_type
            for item in events.list_events(
                session_id="parent-db", tenant_id="tenant-a"
            )
        ]
        assert parent_events == [
            "subagent.started",
            "subagent.running",
            "subagent.finished",
        ]
    finally:
        stop.set()
        thread.join(timeout=2)


def test_db_queue_cancel_propagates_across_worker(tmp_path: Path):
    manager, _governance, events, stack, _settings = _components(
        tmp_path, SlowAdapter()
    )
    task = manager.submit(
        SubagentSubmitRequest(
            objective="可取消任务",
            parent_session_id="parent-db-cancel",
        ),
        tenant_id="tenant-a",
        user_id="user-a",
        role="admin",
    )
    stop = threading.Event()
    worker = SubagentQueueWorker(stack=stack, worker_id="worker-cancel", stop_event=stop)
    thread = threading.Thread(target=worker.run_forever, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            current = manager.get(task.task_id, "tenant-a")
            if current and current.status == "running":
                break
            time.sleep(0.05)
        requested = manager.cancel(task.task_id, "tenant-a")
        assert requested.status in {"cancel_requested", "cancelled"}
        resolved = manager.wait(task.task_id, "tenant-a", timeout=5)
        assert resolved.status == "cancelled"
        event_types = {
            item.event_type
            for item in events.list_events(
                session_id="parent-db-cancel", tenant_id="tenant-a"
            )
        }
        assert "subagent.cancel_requested" in event_types
        assert "subagent.finished" in event_types
    finally:
        stop.set()
        thread.join(timeout=2)


def test_requeue_expired_lease_then_retry(tmp_path: Path):
    manager, governance, _events, stack, settings = _components(
        tmp_path, AnswerAdapter()
    )
    task = manager.submit(
        SubagentSubmitRequest(
            objective="租约过期重试",
            parent_session_id="parent-lease",
        ),
        tenant_id="tenant-a",
        user_id="user-a",
        role="admin",
    )
    claimed = governance.claim_next_task(worker_id="dead-worker", lease_seconds=0.05)
    assert claimed is not None
    assert claimed.task_id == task.task_id
    assert claimed.status == "running"
    time.sleep(0.08)
    recovered = governance.requeue_expired_leases(
        max_attempts=settings.subagent_max_attempts
    )
    assert recovered == 1
    requeued = governance.get_task(task.task_id, "tenant-a")
    assert requeued is not None
    assert requeued.status == "queued"
    assert requeued.worker_id is None
    assert requeued.attempt == 1

    stop = threading.Event()
    worker = SubagentQueueWorker(stack=stack, worker_id="worker-retry", stop_event=stop)
    thread = threading.Thread(target=worker.run_forever, daemon=True)
    thread.start()
    try:
        resolved = manager.wait(task.task_id, "tenant-a", timeout=5)
        assert resolved.status == "completed"
        assert resolved.attempt == 2
    finally:
        stop.set()
        thread.join(timeout=2)
