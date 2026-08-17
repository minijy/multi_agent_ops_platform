from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from ops_agent.config import Settings
from ops_agent.runtime.agent_loop import AgentRuntime
from ops_agent.runtime.domain import ModelTurn, RuntimeAgentRequest, ToolCall
from ops_agent.runtime.governance import SQLiteRuntimeGovernanceStore
from ops_agent.runtime.model_router import ModelRouter
from ops_agent.runtime.sandbox import SandboxRunner, SandboxUnavailableError
from ops_agent.runtime.session_events import SQLiteSessionEventStore
from ops_agent.runtime.subagents import SubagentManager, SubagentSubmitRequest
from ops_agent.runtime.tools import (
    ToolDefinition,
    ToolExecutor,
    ToolRegistry,
)


class EmptyArguments(BaseModel):
    pass


class ApprovalAdapter:
    provider = "fake"
    model_name = "fake-approval"
    input_modalities = frozenset({"text"})

    def invoke(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ModelTurn:
        if messages[-1].get("role") == "tool":
            return ModelTurn(
                provider=self.provider,
                model=self.model_name,
                content="高风险操作已处理",
                usage={"total_tokens": 10},
            )
        return ModelTurn(
            provider=self.provider,
            model=self.model_name,
            tool_calls=[
                ToolCall(
                    call_id="danger-1",
                    name="dangerous_action",
                    arguments={},
                )
            ],
            usage={"total_tokens": 10},
        )


class AnswerAdapter:
    provider = "fake"
    model_name = "fake-answer"
    input_modalities = frozenset({"text"})

    def invoke(self, _messages, _tools):
        return ModelTurn(
            provider=self.provider,
            model=self.model_name,
            content="子任务完成",
            usage={"total_tokens": 20},
        )


def _runtime(
    tmp_path: Path,
    adapter,
    registry: ToolRegistry,
    governance: SQLiteRuntimeGovernanceStore,
) -> tuple[AgentRuntime, SQLiteSessionEventStore]:
    events = SQLiteSessionEventStore(tmp_path / "events.sqlite3")
    return (
        AgentRuntime(
            router=ModelRouter({"fake": adapter}, default_model_id="fake"),
            registry=registry,
            executor=ToolExecutor(registry),
            event_store=events,
            governance_store=governance,
        ),
        events,
    )


def test_high_risk_tool_waits_for_single_call_approval(tmp_path: Path):
    executions = []
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="dangerous_action",
            description="danger",
            arguments_model=EmptyArguments,
            handler=lambda _args, _context: executions.append("done") or {"ok": True},
            risk="high",
            requires_approval=True,
            allowed_roles=frozenset({"admin"}),
        )
    )
    governance = SQLiteRuntimeGovernanceStore(tmp_path / "governance.sqlite3")
    runtime, events = _runtime(
        tmp_path, ApprovalAdapter(), registry, governance
    )

    waiting = runtime.run(
        RuntimeAgentRequest(question="执行危险操作"),
        tenant_id="tenant-a",
        user_id="user-a",
        role="admin",
    )
    assert waiting.status == "waiting_approval"
    assert len(waiting.pending_approval_ids) == 1
    assert executions == []

    completed = runtime.decide_approval(
        approval_id=waiting.pending_approval_ids[0],
        tenant_id="tenant-a",
        decided_by="approver-a",
        approved=True,
        comment="verified",
    )
    assert completed.status == "completed"
    assert completed.answer == "高风险操作已处理"
    assert executions == ["done"]
    event_types = {
        event.event_type
        for event in events.list_events(
            session_id=waiting.session_id, tenant_id="tenant-a"
        )
    }
    assert {"approval.requested", "approval.decided", "tool.completed"} <= event_types


def test_subagent_runs_in_background_with_parent_child_events(tmp_path: Path):
    governance = SQLiteRuntimeGovernanceStore(tmp_path / "governance.sqlite3")
    registry = ToolRegistry()
    runtime, events = _runtime(tmp_path, AnswerAdapter(), registry, governance)
    settings = Settings(
        _env_file=None,
        subagent_worker_count=2,
        subagent_default_timeout_seconds=10,
        subagent_default_token_budget=300,
    )
    manager = SubagentManager(
        runtime=runtime,
        registry=registry,
        event_store=events,
        governance_store=governance,
        settings=settings,
    )
    try:
        task = manager.submit(
            SubagentSubmitRequest(
                objective="独立分析问题",
                parent_session_id="parent-session",
            ),
            tenant_id="tenant-a",
            user_id="user-a",
            role="admin",
        )
        resolved = manager.wait(task.task_id, "tenant-a", timeout=5)
        assert resolved.status == "completed"
        assert resolved.answer == "子任务完成"
        assert resolved.child_session_id != resolved.parent_session_id
        parent_events = events.list_events(
            session_id="parent-session", tenant_id="tenant-a"
        )
        assert [item.event_type for item in parent_events] == [
            "subagent.started",
            "subagent.finished",
        ]
    finally:
        manager.shutdown()


def test_subagent_token_budget_stops_additional_work(tmp_path: Path):
    governance = SQLiteRuntimeGovernanceStore(tmp_path / "governance.sqlite3")
    registry = ToolRegistry()
    runtime, _events = _runtime(tmp_path, AnswerAdapter(), registry, governance)
    response = runtime.run(
        RuntimeAgentRequest(question="预算测试"),
        tenant_id="tenant-a",
        user_id="user-a",
        token_budget=10,
    )
    assert response.status == "budget_exceeded"
    assert "Token 预算" in response.answer


def test_macos_sandbox_read_only_denies_write(tmp_path: Path):
    runner = SandboxRunner(tmp_path, timeout_seconds=5)
    if not runner.restricted_available:
        pytest.skip("macOS Seatbelt sandbox-exec is unavailable")
    readable = runner.run(
        ["/bin/sh", "-c", "printf ok"],
        mode="read-only",
    )
    assert readable.exit_code == 0
    assert readable.stdout == "ok"

    denied = runner.run(
        ["/usr/bin/touch", "blocked.txt"],
        mode="read-only",
    )
    assert denied.exit_code != 0
    assert not (tmp_path / "blocked.txt").exists()

    writable = runner.run(
        ["/usr/bin/touch", "allowed.txt"],
        mode="workspace-write",
    )
    assert writable.exit_code == 0
    assert (tmp_path / "allowed.txt").exists()


def test_resolve_workspace_file_rejects_escape(tmp_path: Path):
    runner = SandboxRunner(tmp_path)
    inside = tmp_path / "ok.txt"
    inside.write_text("ok", encoding="utf-8")
    assert runner.resolve_workspace_file("ok.txt") == inside.resolve()
    assert runner.resolve_workspace_file(f"sandbox:{inside}") == inside.resolve()
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("no", encoding="utf-8")
    with pytest.raises(FileNotFoundError):
        runner.resolve_workspace_file(str(outside))
    with pytest.raises(FileNotFoundError):
        runner.resolve_workspace_file("missing.txt")


def test_echo_argv_materializes_workspace_file(tmp_path: Path):
    runner = SandboxRunner(tmp_path)
    dest = runner._materialize_echo_argv(
        ["echo", "-e", "费用类别\\t金额\\nFBA\\t-1", "file.txt"],
        tmp_path,
    )
    assert dest == (tmp_path / "file.csv").resolve()
    csv = dest.read_text(encoding="utf-8-sig")
    assert "费用类别,金额" in csv
    assert "file.txt" not in csv
    assert (tmp_path / "file.txt").exists()
    assert runner.resolve_workspace_file("file.txt") == dest.resolve()


def test_echo_strips_wrapping_quotes_from_csv(tmp_path: Path):
    runner = SandboxRunner(tmp_path)
    dest = runner._materialize_echo_argv(
        [
            "echo",
            "-e",
            '"费用类别\\t金额（美元）\\nFBA费用\\t-15,174.50\\n亚马逊费用\\t-0.01"',
            "report.csv",
        ],
        tmp_path,
    )
    csv = dest.read_text(encoding="utf-8-sig")
    assert csv.startswith("费用类别,金额（美元）")
    assert '"费用类别' not in csv
    assert not csv.rstrip().endswith('-0.01"')
    assert csv.rstrip().endswith("-0.01")
    cleaned = runner.sanitize_csv_bytes(('"' + csv.rstrip() + '"').encode("utf-8"))
    assert cleaned.decode("utf-8-sig").startswith("费用类别,")
    assert not cleaned.decode("utf-8-sig").rstrip().endswith('-0.01"')


def test_unwrap_keeps_legitimate_csv_quoted_header():
    text = '"费用,类别",金额\nFBA,"-1,234.56"\n'
    assert SandboxRunner.unwrap_echo_payload(text) == text


def test_subagent_cancel_stops_running_task(tmp_path: Path):
    class SlowAdapter:
        provider = "fake"
        model_name = "fake-slow"
        input_modalities = frozenset({"text"})

        def invoke(self, _messages, _tools):
            import time

            time.sleep(2)
            return ModelTurn(
                provider=self.provider,
                model=self.model_name,
                content="should not finish",
                usage={"total_tokens": 5},
            )

    governance = SQLiteRuntimeGovernanceStore(tmp_path / "governance.sqlite3")
    registry = ToolRegistry()
    runtime, events = _runtime(tmp_path, SlowAdapter(), registry, governance)
    settings = Settings(
        _env_file=None,
        subagent_worker_count=1,
        subagent_default_timeout_seconds=10,
        subagent_default_token_budget=300,
    )
    manager = SubagentManager(
        runtime=runtime,
        registry=registry,
        event_store=events,
        governance_store=governance,
        settings=settings,
    )
    try:
        task = manager.submit(
            SubagentSubmitRequest(
                objective="长时间任务",
                parent_session_id="parent-cancel",
            ),
            tenant_id="tenant-a",
            user_id="user-a",
            role="admin",
        )
        requested = manager.cancel(task.task_id, "tenant-a")
        assert requested.status in {"cancelled", "cancel_requested"}
        resolved = manager.wait(task.task_id, "tenant-a", timeout=5)
        assert resolved.status in {"cancelled", "cancel_requested"}
        parent_events = {
            item.event_type
            for item in events.list_events(
                session_id="parent-cancel", tenant_id="tenant-a"
            )
        }
        assert "subagent.cancel_requested" in parent_events
    finally:
        manager.shutdown()


def test_subagent_cannot_inherit_approval_required_tools(tmp_path: Path):
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="dangerous_action",
            description="danger",
            arguments_model=EmptyArguments,
            handler=lambda _args, _context: {"ok": True},
            risk="high",
            requires_approval=True,
            allowed_roles=frozenset({"admin"}),
        )
    )
    governance = SQLiteRuntimeGovernanceStore(tmp_path / "governance.sqlite3")
    runtime, events = _runtime(tmp_path, AnswerAdapter(), registry, governance)
    manager = SubagentManager(
        runtime=runtime,
        registry=registry,
        event_store=events,
        governance_store=governance,
        settings=Settings(_env_file=None, subagent_worker_count=1),
    )
    try:
        task = manager.submit(
            SubagentSubmitRequest(
                objective="不要越权",
                parent_session_id="parent-policy",
            ),
            tenant_id="tenant-a",
            user_id="user-a",
            role="admin",
        )
        assert "dangerous_action" not in task.allowed_tools
        with pytest.raises(PermissionError, match="approval-required"):
            manager.submit(
                SubagentSubmitRequest(
                    objective="显式越权",
                    parent_session_id="parent-policy",
                    allowed_tools=["dangerous_action"],
                ),
                tenant_id="tenant-a",
                user_id="user-a",
                role="admin",
            )
    finally:
        manager.shutdown()


def test_runtime_timeout_returns_timed_out(tmp_path: Path):
    class SlowAdapter:
        provider = "fake"
        model_name = "fake-timeout"
        input_modalities = frozenset({"text"})

        def invoke(self, _messages, _tools):
            import time

            time.sleep(0.4)
            return ModelTurn(
                provider=self.provider,
                model=self.model_name,
                content="too late",
                usage={"total_tokens": 5},
            )

    governance = SQLiteRuntimeGovernanceStore(tmp_path / "governance.sqlite3")
    runtime, _events = _runtime(tmp_path, SlowAdapter(), ToolRegistry(), governance)
    response = runtime.run(
        RuntimeAgentRequest(question="超时测试"),
        tenant_id="tenant-a",
        user_id="user-a",
        timeout_seconds=0.05,
    )
    assert response.status == "timed_out"
    assert "超时" in response.answer


def test_sandbox_restricted_modes_fail_closed_without_backend(tmp_path: Path):
    runner = SandboxRunner(tmp_path, timeout_seconds=5)
    runner.restricted_available = False
    with pytest.raises(SandboxUnavailableError, match="unavailable"):
        runner.run(["/bin/echo", "no"], mode="read-only")
    unrestricted = runner.run(["/usr/bin/true"], mode="danger-full-access")
    assert unrestricted.exit_code == 0
