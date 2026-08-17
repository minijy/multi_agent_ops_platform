from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout

from pydantic import BaseModel, Field

from ..config import Settings
from .agent_loop import AgentRuntime
from .domain import RuntimeAgentRequest
from .governance import (
    TERMINAL_SUBAGENT_STATUSES,
    RuntimeGovernanceStore,
    SubagentTaskRecord,
    _now,
)
from .tools import ToolDefinition, ToolExecutionContext, ToolRegistry


class SubagentSubmitRequest(BaseModel):
    objective: str = Field(min_length=2, max_length=4000)
    parent_session_id: str = Field(min_length=1, max_length=128)
    allowed_tools: list[str] = Field(default_factory=list, max_length=64)
    timeout_seconds: float | None = Field(default=None, ge=5, le=1800)
    token_budget: int | None = Field(default=None, ge=256)
    wait: bool = False


class DelegateSubagentArguments(BaseModel):
    objective: str = Field(min_length=2, max_length=4000)
    allowed_tools: list[str] = Field(default_factory=list, max_length=64)
    timeout_seconds: float | None = Field(default=None, ge=5, le=1800)
    token_budget: int | None = Field(default=None, ge=256)
    run_in_background: bool = True


def execute_subagent_task(
    *,
    runtime: AgentRuntime,
    store: RuntimeGovernanceStore,
    event_store,
    record: SubagentTaskRecord,
    cancellation: threading.Event,
) -> SubagentTaskRecord:
    started = record.model_copy(
        update={
            "status": "running" if record.status != "cancel_requested" else record.status,
            "started_at": record.started_at or _now(),
        }
    )
    if started.status == "cancel_requested":
        cancellation.set()
    store.update_task(started)
    final = started
    try:
        if cancellation.is_set():
            final = started.model_copy(
                update={
                    "status": "cancelled",
                    "completed_at": _now(),
                    "worker_id": None,
                    "lease_expires_at": None,
                }
            )
        else:
            response = runtime.run(
                RuntimeAgentRequest(
                    question=record.objective,
                    session_id=record.child_session_id,
                ),
                tenant_id=record.tenant_id,
                user_id=record.user_id,
                role=record.role,
                allowed_tools=set(record.allowed_tools),
                delegation_depth=record.depth,
                parent_session_id=record.parent_session_id,
                cancellation_event=cancellation,
                timeout_seconds=record.timeout_seconds,
                token_budget=record.token_budget,
            )
            mapped = "cancelled" if cancellation.is_set() else response.status
            if mapped not in {
                "completed",
                "cancelled",
                "timed_out",
                "failed",
                "waiting_approval",
                "budget_exceeded",
            }:
                mapped = "failed"
            final = started.model_copy(
                update={
                    "status": mapped,
                    "answer": response.answer,
                    "completed_at": _now(),
                    "worker_id": None,
                    "lease_expires_at": None,
                }
            )
    except Exception as exc:
        cancelled = cancellation.is_set()
        final = started.model_copy(
            update={
                "status": "cancelled" if cancelled else "failed",
                "error": str(exc),
                "completed_at": _now(),
                "worker_id": None,
                "lease_expires_at": None,
            }
        )
    store.update_task(final)
    event_store.append(
        session_id=record.parent_session_id,
        tenant_id=record.tenant_id,
        user_id=record.user_id,
        event_type="subagent.finished",
        payload={
            "task_id": record.task_id,
            "child_session_id": record.child_session_id,
            "status": final.status,
            "answer": final.answer,
            "error": final.error,
        },
    )
    return final


class SubagentManager:
    def __init__(
        self,
        *,
        runtime: AgentRuntime,
        registry: ToolRegistry,
        event_store,
        governance_store: RuntimeGovernanceStore,
        settings: Settings,
    ) -> None:
        self.runtime = runtime
        self.registry = registry
        self.event_store = event_store
        self.store = governance_store
        self.settings = settings
        self.queue_backend = settings.subagent_queue_backend
        self._lock = threading.Lock()
        self._futures: dict[str, Future[None]] = {}
        self._cancellations: dict[str, threading.Event] = {}
        self.pool: ThreadPoolExecutor | None = None
        if self.queue_backend == "inline":
            self.pool = ThreadPoolExecutor(
                max_workers=settings.subagent_worker_count,
                thread_name_prefix="subagent",
            )

    def _resolved_tools(
        self,
        requested: list[str],
        *,
        tenant_id: str,
        user_id: str,
        role: str,
        depth: int,
    ) -> list[str]:
        context = ToolExecutionContext(
            session_id="subagent-policy",
            tenant_id=tenant_id,
            user_id=user_id,
            role=role,
            delegation_depth=depth,
        )
        visible = {
            item["function"]["name"]
            for item in self.registry.schemas(context)
        }
        if requested:
            unknown = set(requested) - visible
            if unknown:
                raise PermissionError(
                    f"subagent tools are not visible: {sorted(unknown)}"
                )
            selected = set(requested)
        else:
            selected = set(visible)
        approval_required = {
            name
            for name in selected
            if self.registry.get(name, context).requires_approval
        }
        if requested and approval_required:
            raise PermissionError(
                "subagent cannot inherit approval-required tools: "
                f"{sorted(approval_required)}"
            )
        selected -= approval_required
        if depth >= self.settings.subagent_max_depth:
            selected.discard("delegate_subagent")
        return sorted(selected)

    def submit(
        self,
        request: SubagentSubmitRequest,
        *,
        tenant_id: str,
        user_id: str,
        role: str,
        depth: int = 1,
    ) -> SubagentTaskRecord:
        if depth > self.settings.subagent_max_depth:
            raise ValueError(
                f"subagent depth exceeds {self.settings.subagent_max_depth}"
            )
        timeout = (
            request.timeout_seconds
            or self.settings.subagent_default_timeout_seconds
        )
        token_budget = (
            request.token_budget
            or self.settings.subagent_default_token_budget
        )
        allowed_tools = self._resolved_tools(
            request.allowed_tools,
            tenant_id=tenant_id,
            user_id=user_id,
            role=role,
            depth=depth,
        )
        task_id = str(uuid.uuid4())
        child_session_id = str(uuid.uuid4())
        record = SubagentTaskRecord(
            task_id=task_id,
            parent_session_id=request.parent_session_id,
            child_session_id=child_session_id,
            tenant_id=tenant_id,
            user_id=user_id,
            role=role,
            objective=request.objective,
            status="queued",
            depth=depth,
            allowed_tools=allowed_tools,
            token_budget=token_budget,
            timeout_seconds=timeout,
            created_at=_now(),
        )
        self.store.create_task(record)
        self.event_store.append(
            session_id=request.parent_session_id,
            tenant_id=tenant_id,
            user_id=user_id,
            event_type="subagent.started",
            payload={
                "task_id": task_id,
                "child_session_id": child_session_id,
                "objective": request.objective,
                "depth": depth,
                "allowed_tools": allowed_tools,
                "queue_backend": self.queue_backend,
            },
        )
        if self.queue_backend == "inline":
            assert self.pool is not None
            cancellation = threading.Event()
            with self._lock:
                self._cancellations[task_id] = cancellation
            future = self.pool.submit(self._run_inline, record, cancellation)
            with self._lock:
                self._futures[task_id] = future
                if future.done():
                    self._futures.pop(task_id, None)
                    self._cancellations.pop(task_id, None)
        return record

    def _run_inline(
        self, record: SubagentTaskRecord, cancellation: threading.Event
    ) -> None:
        try:
            execute_subagent_task(
                runtime=self.runtime,
                store=self.store,
                event_store=self.event_store,
                record=record,
                cancellation=cancellation,
            )
        finally:
            with self._lock:
                self._futures.pop(record.task_id, None)
                self._cancellations.pop(record.task_id, None)

    def get(self, task_id: str, tenant_id: str) -> SubagentTaskRecord | None:
        return self.store.get_task(task_id, tenant_id)

    def list(
        self, tenant_id: str, parent_session_id: str | None = None
    ) -> list[SubagentTaskRecord]:
        return self.store.list_tasks(tenant_id, parent_session_id)

    def wait(
        self, task_id: str, tenant_id: str, timeout: float | None = None
    ) -> SubagentTaskRecord:
        deadline = None if timeout is None else time.monotonic() + timeout
        if self.queue_backend == "inline":
            with self._lock:
                future = self._futures.get(task_id)
            if future is not None:
                remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
                try:
                    future.result(timeout=remaining)
                except (FutureTimeout, Exception):
                    pass
        while True:
            record = self.get(task_id, tenant_id)
            if record is None:
                raise KeyError("subagent task not found")
            if record.status in TERMINAL_SUBAGENT_STATUSES:
                return record
            if deadline is not None and time.monotonic() >= deadline:
                return record
            time.sleep(0.05)

    def cancel(self, task_id: str, tenant_id: str) -> SubagentTaskRecord:
        record = self.get(task_id, tenant_id)
        if record is None:
            raise KeyError("subagent task not found")
        if record.status not in {"queued", "running", "cancel_requested"}:
            return record

        if self.queue_backend == "inline":
            with self._lock:
                cancellation = self._cancellations.get(task_id)
                future = self._futures.get(task_id)
            if cancellation is not None:
                cancellation.set()
            if future is not None:
                future.cancel()

        latest = self.get(task_id, tenant_id)
        if latest is None:
            raise KeyError("subagent task not found")
        if latest.status in TERMINAL_SUBAGENT_STATUSES:
            return latest

        if latest.status == "queued":
            cancelled = latest.model_copy(
                update={
                    "status": "cancelled",
                    "completed_at": _now(),
                    "worker_id": None,
                    "lease_expires_at": None,
                }
            )
            self.store.update_task(cancelled)
            verified = self.get(task_id, tenant_id) or cancelled
            if verified.status == "cancelled":
                self.event_store.append(
                    session_id=record.parent_session_id,
                    tenant_id=record.tenant_id,
                    user_id=record.user_id,
                    event_type="subagent.cancel_requested",
                    payload={
                        "task_id": task_id,
                        "child_session_id": record.child_session_id,
                    },
                )
                self.event_store.append(
                    session_id=record.parent_session_id,
                    tenant_id=record.tenant_id,
                    user_id=record.user_id,
                    event_type="subagent.finished",
                    payload={
                        "task_id": task_id,
                        "child_session_id": record.child_session_id,
                        "status": "cancelled",
                    },
                )
                return verified
            latest = verified

        if latest.status in TERMINAL_SUBAGENT_STATUSES:
            return latest

        updated = latest.model_copy(update={"status": "cancel_requested"})
        self.store.update_task(updated)
        self.event_store.append(
            session_id=record.parent_session_id,
            tenant_id=record.tenant_id,
            user_id=record.user_id,
            event_type="subagent.cancel_requested",
            payload={"task_id": task_id, "child_session_id": record.child_session_id},
        )
        return self.get(task_id, tenant_id) or updated

    def shutdown(self) -> None:
        with self._lock:
            cancellations = list(self._cancellations.values())
        for cancellation in cancellations:
            cancellation.set()
        if self.pool is not None:
            self.pool.shutdown(wait=False, cancel_futures=True)


def register_subagent_tool(
    registry: ToolRegistry, manager: SubagentManager
) -> None:
    def delegate(
        arguments: DelegateSubagentArguments,
        context: ToolExecutionContext,
    ):
        task = manager.submit(
            SubagentSubmitRequest(
                objective=arguments.objective,
                parent_session_id=context.session_id,
                allowed_tools=arguments.allowed_tools,
                timeout_seconds=arguments.timeout_seconds,
                token_budget=arguments.token_budget,
                wait=not arguments.run_in_background,
            ),
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            role=context.role,
            depth=context.delegation_depth + 1,
        )
        if arguments.run_in_background:
            return task.model_dump(mode="json")
        resolved = manager.wait(
            task.task_id,
            context.tenant_id,
            timeout=task.timeout_seconds + 2,
        )
        return resolved.model_dump(mode="json")

    registry.register(
        ToolDefinition(
            name="delegate_subagent",
            description=(
                "把独立调查或分析任务委派给受预算、深度和工具权限约束的子 Agent。"
                "默认后台运行，返回 task_id 与独立 child session。"
            ),
            arguments_model=DelegateSubagentArguments,
            handler=delegate,
            risk="low",
            timeout_seconds=180,
            concurrency_safe=True,
            source="subagent",
            builtin=True,
            allowed_roles=frozenset({"operator", "admin"}),
        )
    )
