from __future__ import annotations

import asyncio
import threading
import time
import uuid
from concurrent.futures import Future

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field, model_validator
from typing import Any, AsyncIterator, Literal, TypedDict

from ..config import Settings
from .agent_loop import AgentRuntime
from .domain import RuntimeAgentRequest
from .governance import (
    TERMINAL_SUBAGENT_STATUSES,
    RuntimeGovernanceStore,
    SubagentTaskRecord,
    _now,
)
from ..agent_roles import (
    ANALYST_AGENT_ID,
    DATA_QUERY_TOOL_NAMES,
    SPECIALIST_ANALYST_IDS,
)
from .agent_tool_policy import resolve_agent_tool_allowlist
from .tools import ToolDefinition, ToolExecutionContext, ToolRegistry


class SubagentSubmitRequest(BaseModel):
    objective: str = Field(min_length=2, max_length=4000)
    parent_session_id: str = Field(min_length=1, max_length=128)
    agent_id: str = Field(default=ANALYST_AGENT_ID, min_length=1, max_length=64)
    model_id: str | None = Field(default=None, max_length=64)
    connection_ids: list[str] = Field(default_factory=list, max_length=32)
    resource_scope: dict[str, list[str]] = Field(default_factory=dict)
    memory_snapshot: list[dict[str, Any]] = Field(default_factory=list, max_length=50)
    allowed_tools: list[str] = Field(default_factory=list, max_length=64)
    timeout_seconds: float | None = Field(default=None, ge=5, le=1800)
    token_budget: int | None = Field(default=None, ge=256)
    wait: bool = False


class DelegateSubagentArguments(BaseModel):
    agent_id: str = Field(default=ANALYST_AGENT_ID, min_length=1, max_length=64)
    objective: str = Field(min_length=2, max_length=4000)
    allowed_tools: list[str] = Field(default_factory=list, max_length=64)
    timeout_seconds: float | None = Field(default=None, ge=5, le=1800)
    token_budget: int | None = Field(default=None, ge=256)
    run_in_background: bool = False

    @model_validator(mode="after")
    def validate_sync_timeout(self) -> "DelegateSubagentArguments":
        if not self.run_in_background and self.timeout_seconds is not None:
            if self.timeout_seconds > 170:
                raise ValueError(
                    "synchronous subagent timeout must be <= 170 seconds; "
                    "use run_in_background=true for longer tasks"
                )
        return self


class SpecialistDelegation(BaseModel):
    agent_id: str = Field(min_length=1, max_length=64)
    objective: str = Field(min_length=2, max_length=4000)

    @model_validator(mode="after")
    def validate_specialist(self) -> "SpecialistDelegation":
        if self.agent_id not in SPECIALIST_ANALYST_IDS:
            raise ValueError(f"not a specialist analyst: {self.agent_id}")
        return self


class DelegateSpecialistsArguments(BaseModel):
    tasks: list[SpecialistDelegation] = Field(min_length=1, max_length=3)
    timeout_seconds: float | None = Field(default=None, ge=5, le=170)
    token_budget: int | None = Field(default=None, ge=256)


class SubagentGraphState(TypedDict, total=False):
    """Private state owned by one compiled Analyst subgraph invocation."""

    record: SubagentTaskRecord
    cancellation: threading.Event
    response: Any
    error: str
    skip_finalize: bool


class LangGraphSubagent:
    """Compiled LangGraph subgraph for the governed Analyst lifecycle.

    The Coordinator continues to expose delegation as a tool, while each delegated
    task gets isolated graph state and an isolated child session.  The `agent` node
    invokes the existing model/tools agent graph through ``AgentRuntime``; the
    surrounding nodes keep queue status and audit events inside the subgraph.
    """

    def __init__(
        self,
        *,
        runtime: AgentRuntime,
        store: RuntimeGovernanceStore,
        event_store,
    ) -> None:
        self.runtime = runtime
        self.store = store
        self.event_store = event_store
        builder = StateGraph(SubagentGraphState)
        builder.add_node("prepare", self._prepare)
        builder.add_node("agent", self._run_agent)
        builder.add_node("finalize", self._finalize)
        builder.add_edge(START, "prepare")
        builder.add_conditional_edges(
            "prepare",
            self._route_after_prepare,
            {"agent": "agent", "finalize": "finalize"},
        )
        builder.add_edge("agent", "finalize")
        builder.add_edge("finalize", END)
        self.graph = builder.compile()

    def _prepare(self, state: SubagentGraphState) -> dict[str, Any]:
        submitted = state["record"]
        cancellation = state["cancellation"]
        record = self.store.get_task(
            submitted.task_id, submitted.tenant_id
        ) or submitted
        if record.status in TERMINAL_SUBAGENT_STATUSES:
            return {"record": record, "skip_finalize": True}
        cancelling = cancellation.is_set() or record.status == "cancel_requested"
        started = record.model_copy(
            update={
                "status": "cancel_requested" if cancelling else "running",
                "started_at": record.started_at or _now(),
            }
        )
        if cancelling:
            cancellation.set()
        self.store.update_task(started)
        if started.status == "running":
            self.event_store.append(
                session_id=record.parent_session_id,
                tenant_id=record.tenant_id,
                user_id=record.user_id,
                event_type="subagent.running",
                payload={
                    "task_id": record.task_id,
                    "child_session_id": record.child_session_id,
                    "agent_id": record.agent_id,
                    "objective": record.objective,
                    "status": "running",
                },
            )
        return {"record": started}

    @staticmethod
    def _route_after_prepare(
        state: SubagentGraphState,
    ) -> Literal["agent", "finalize"]:
        return "finalize" if state["cancellation"].is_set() else "agent"

    def _run_agent(self, state: SubagentGraphState) -> dict[str, Any]:
        record = state["record"]
        try:
            response = self.runtime.run(
                RuntimeAgentRequest(
                    question=record.objective,
                    session_id=record.child_session_id,
                    model_id=record.model_id,
                ),
                tenant_id=record.tenant_id,
                user_id=record.user_id,
                role=record.role,
                allowed_tools=set(record.allowed_tools),
                agent_id=record.agent_id,
                delegation_depth=record.depth,
                parent_session_id=record.parent_session_id,
                connection_ids=record.connection_ids,
                resource_scope=record.resource_scope,
                memory_snapshot=record.memory_snapshot,
                cancellation_event=state["cancellation"],
                timeout_seconds=record.timeout_seconds,
                token_budget=record.token_budget,
            )
            return {"response": response}
        except Exception as exc:
            return {"error": str(exc)}

    def _finalize(self, state: SubagentGraphState) -> dict[str, Any]:
        started = state["record"]
        if state.get("skip_finalize"):
            return {"record": started}
        cancellation = state["cancellation"]
        response = state.get("response")
        error = state.get("error")
        if error is not None:
            final = started.model_copy(
                update={
                    "status": "cancelled" if cancellation.is_set() else "failed",
                    "error": error,
                    "completed_at": _now(),
                    "worker_id": None,
                    "lease_expires_at": None,
                }
            )
        elif response is None:
            final = started.model_copy(
                update={
                    "status": "cancelled",
                    "completed_at": _now(),
                    "worker_id": None,
                    "lease_expires_at": None,
                }
            )
        else:
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
        self.store.update_task(final)
        self.event_store.append(
            session_id=final.parent_session_id,
            tenant_id=final.tenant_id,
            user_id=final.user_id,
            event_type="subagent.finished",
            payload={
                "task_id": final.task_id,
                "child_session_id": final.child_session_id,
                "status": final.status,
                "answer": final.answer,
                "error": final.error,
            },
        )
        return {"record": final}

    @staticmethod
    def _input(
        record: SubagentTaskRecord, cancellation: threading.Event
    ) -> SubagentGraphState:
        return {"record": record, "cancellation": cancellation}

    @staticmethod
    def _config(record: SubagentTaskRecord) -> dict[str, Any]:
        return {
            "tags": ["subagent", record.agent_id],
            "metadata": {
                "task_id": record.task_id,
                "parent_session_id": record.parent_session_id,
                "child_session_id": record.child_session_id,
                "agent_id": record.agent_id,
            },
        }

    def invoke(
        self, record: SubagentTaskRecord, cancellation: threading.Event
    ) -> SubagentTaskRecord:
        result = self.graph.invoke(
            self._input(record, cancellation), config=self._config(record)
        )
        return result["record"]

    async def ainvoke(
        self, record: SubagentTaskRecord, cancellation: threading.Event
    ) -> SubagentTaskRecord:
        result = await self.graph.ainvoke(
            self._input(record, cancellation), config=self._config(record)
        )
        return result["record"]

    async def astream(
        self, record: SubagentTaskRecord, cancellation: threading.Event
    ) -> AsyncIterator[dict[str, Any]]:
        async for update in self.graph.astream(
            self._input(record, cancellation),
            config=self._config(record),
            stream_mode="updates",
        ):
            yield update


def execute_subagent_task(
    *,
    runtime: AgentRuntime,
    store: RuntimeGovernanceStore,
    event_store,
    record: SubagentTaskRecord,
    cancellation: threading.Event,
    subgraph: LangGraphSubagent | None = None,
) -> SubagentTaskRecord:
    workflow = subgraph or LangGraphSubagent(
        runtime=runtime,
        store=store,
        event_store=event_store,
    )
    return workflow.invoke(record, cancellation)


async def execute_subagent_task_async(
    *,
    runtime: AgentRuntime,
    store: RuntimeGovernanceStore,
    event_store,
    record: SubagentTaskRecord,
    cancellation: threading.Event,
    subgraph: LangGraphSubagent | None = None,
) -> SubagentTaskRecord:
    workflow = subgraph or LangGraphSubagent(
        runtime=runtime,
        store=store,
        event_store=event_store,
    )
    return await workflow.ainvoke(record, cancellation)


class SubagentManager:
    def __init__(
        self,
        *,
        runtime: AgentRuntime,
        registry: ToolRegistry,
        event_store,
        governance_store: RuntimeGovernanceStore,
        settings: Settings,
        access_control=None,
        memory_service=None,
    ) -> None:
        self.runtime = runtime
        self.registry = registry
        self.event_store = event_store
        self.store = governance_store
        self.settings = settings
        self.access_control = access_control
        self.memory_service = memory_service
        self.queue_backend = settings.subagent_queue_backend
        self._lock = threading.Lock()
        self._submission_lock = threading.Lock()
        self._futures: dict[str, Future[SubagentTaskRecord]] = {}
        self._cancellations: dict[str, threading.Event] = {}
        self._closed = False
        self.subgraph = LangGraphSubagent(
            runtime=runtime,
            store=governance_store,
            event_store=event_store,
        )
        self._async_loop: asyncio.AbstractEventLoop | None = None
        self._async_thread: threading.Thread | None = None
        self._async_ready = threading.Event()
        self._async_capacity: asyncio.Semaphore | None = None
        # Compatibility marker for callers that asserted DB mode had no pool.
        # Execution no longer uses a project-owned ThreadPoolExecutor.
        self.pool = None
        if self.queue_backend == "inline":
            self._async_loop = asyncio.new_event_loop()
            self._async_thread = threading.Thread(
                target=self._run_async_loop,
                name="subagent-langgraph",
                daemon=True,
            )
            self._async_thread.start()
            self._async_ready.wait(timeout=5)

    def _run_async_loop(self) -> None:
        assert self._async_loop is not None
        asyncio.set_event_loop(self._async_loop)
        self._async_capacity = asyncio.Semaphore(
            self.settings.subagent_worker_count
        )
        self._async_ready.set()
        self._async_loop.run_forever()

    async def _run_inline_async(
        self, record: SubagentTaskRecord, cancellation: threading.Event
    ) -> SubagentTaskRecord:
        assert self._async_capacity is not None
        async with self._async_capacity:
            return await execute_subagent_task_async(
                runtime=self.runtime,
                store=self.store,
                event_store=self.event_store,
                record=record,
                cancellation=cancellation,
                subgraph=self.subgraph,
            )

    def _resolve_target_agent(self, agent_id: str):
        registry = getattr(self.runtime, "agent_registry", None)
        if registry is None:
            return None
        agent = registry.get(agent_id)
        if agent is None:
            raise PermissionError(f"unknown agent: {agent_id}")
        if not agent.enabled:
            raise PermissionError(f"agent is disabled: {agent_id}")
        if not agent.accepts_delegation():
            raise PermissionError(f"agent is not delegatable: {agent_id}")
        return agent

    def _enforce_parallel_limit(
        self, *, tenant_id: str, parent_session_id: str
    ) -> None:
        active_statuses = {"queued", "running", "cancel_requested"}
        active = [
            task
            for task in self.store.list_tasks(tenant_id, parent_session_id)
            if task.status in active_statuses
        ]
        if len(active) >= self.settings.intent_routing_parallel_limit:
            raise ValueError(
                "specialist analyst parallel limit reached: "
                f"{self.settings.intent_routing_parallel_limit}"
            )

    def _resolved_tools(
        self,
        requested: list[str],
        *,
        tenant_id: str,
        user_id: str,
        role: str,
        depth: int,
        agent=None,
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
        if (
            agent is not None
            and getattr(self.runtime, "agent_registry", None) is not None
            and getattr(self.runtime, "settings", None) is not None
        ):
            base = resolve_agent_tool_allowlist(
                agent,
                self.runtime.agent_registry,
                self.runtime.settings,
                self.registry,
                getattr(self.runtime, "connection_registry", None),
                tenant_id,
                getattr(self.runtime, "tool_catalog", None),
            ) & visible
        else:
            base = set(visible)
        access_limited = False
        if self.access_control is not None:
            decision = self.access_control.effective_access(tenant_id, user_id, role)
            if decision.allowed_tools is not None:
                access_limited = True
                base &= set(decision.allowed_tools)
        if requested:
            unknown = set(requested) - base
            if unknown:
                raise PermissionError(
                    f"subagent tools are not visible: {sorted(unknown)}"
                )
            selected = set(requested)
        else:
            selected = set(base)
        if (
            agent is not None
            and agent.id in SPECIALIST_ANALYST_IDS
            and access_limited
            and not (selected & DATA_QUERY_TOOL_NAMES)
        ):
            raise PermissionError(
                f"user has no accessible data tools for specialist: {agent.id}"
            )
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

    def _resolved_connection_scope(
        self,
        requested_ids: list[str],
        requested_scope: dict[str, list[str]],
        *,
        tenant_id: str,
    ) -> tuple[list[str], dict[str, list[str]]]:
        connections = getattr(self.runtime, "connection_registry", None)
        if connections is None:
            if requested_ids or requested_scope:
                raise PermissionError("connection registry is not configured")
            return [], {}
        available = {
            item.id: item for item in connections.list_for_tenant(tenant_id)
        }
        selected_ids = list(dict.fromkeys(requested_ids)) or sorted(available)
        unknown = sorted(set(selected_ids) - set(available))
        if unknown:
            raise PermissionError(
                f"subagent connections are not visible: {unknown}"
            )
        maximum: dict[str, set[str]] = {}
        for connection_id in selected_ids:
            for name, values in available[connection_id].resource_scopes.items():
                maximum.setdefault(name, set()).update(str(item) for item in values)
        if not requested_scope:
            return selected_ids, {
                name: sorted(values) for name, values in maximum.items()
            }
        resolved: dict[str, list[str]] = {
            name: sorted(values) for name, values in maximum.items()
        }
        for name, values in requested_scope.items():
            normalized = set(str(item) for item in values)
            # An empty scope is metadata saying that the parent has no explicit
            # restriction for that dimension. It must not make an unrelated
            # specialist fail visibility validation.
            if not normalized:
                continue
            if name not in maximum:
                raise PermissionError(
                    f"subagent resource scope is not visible: {name}"
                )
            allowed = maximum.get(name, set())
            if "*" not in allowed and not normalized.issubset(allowed):
                raise PermissionError(
                    f"subagent resource scope is not visible: {name}={sorted(normalized)}"
                )
            resolved[name] = sorted(normalized)
        return selected_ids, resolved

    def _scope_for_allowed_tools(
        self,
        connection_ids: list[str],
        resource_scope: dict[str, list[str]],
        allowed_tools: list[str],
        *,
        tenant_id: str,
    ) -> tuple[list[str], dict[str, list[str]]]:
        """Reduce inherited connector authority to the child's actual tools."""
        bindings = getattr(self.runtime, "tool_bindings", None)
        connections = getattr(self.runtime, "connection_registry", None)
        if bindings is None or connections is None:
            return connection_ids, resource_scope
        tool_ids, tool_scope = bindings.execution_scope(
            tenant_id, set(allowed_tools), connections
        )
        if not tool_ids:
            return [], {}
        selected = sorted(set(connection_ids) & set(tool_ids))
        if not selected:
            raise PermissionError(
                "subagent has no reachable connector for its allowed tools"
            )
        narrowed: dict[str, list[str]] = {}
        for name, values in tool_scope.items():
            permitted = {str(item) for item in values}
            if not permitted:
                continue
            inherited = {
                str(item) for item in resource_scope.get(name, values)
            }
            if "*" in inherited:
                effective = permitted
            elif "*" in permitted:
                effective = inherited
            else:
                effective = inherited & permitted
            if effective:
                narrowed[name] = sorted(effective)
        return selected, narrowed

    def submit(
        self,
        request: SubagentSubmitRequest,
        *,
        tenant_id: str,
        user_id: str,
        role: str,
        depth: int = 1,
    ) -> SubagentTaskRecord:
        if self._closed:
            raise RuntimeError("subagent manager is shut down")
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
        with self._submission_lock:
            agent = self._resolve_target_agent(request.agent_id)
            self._enforce_parallel_limit(
                tenant_id=tenant_id,
                parent_session_id=request.parent_session_id,
            )
            allowed_tools = self._resolved_tools(
                request.allowed_tools,
                tenant_id=tenant_id,
                user_id=user_id,
                role=role,
                depth=depth,
                agent=agent,
            )
            connection_ids, resource_scope = self._resolved_connection_scope(
                request.connection_ids,
                request.resource_scope,
                tenant_id=tenant_id,
            )
            connection_ids, resource_scope = self._scope_for_allowed_tools(
                connection_ids,
                resource_scope,
                allowed_tools,
                tenant_id=tenant_id,
            )
            memory_snapshot = list(request.memory_snapshot)
            if self.memory_service is not None:
                memory_snapshot = self.memory_service.build_snapshot(
                    request.objective,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    agent_id=request.agent_id,
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
                model_id=request.model_id,
                connection_ids=connection_ids,
                resource_scope=resource_scope,
                memory_snapshot=memory_snapshot,
                objective=request.objective,
                status="queued",
                depth=depth,
                agent_id=request.agent_id,
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
                "agent_id": request.agent_id,
                "status": "queued",
                "model_id": request.model_id,
                "connection_ids": connection_ids,
                "resource_scope": resource_scope,
                "memory_snapshot_count": len(memory_snapshot),
                "objective": request.objective,
                "depth": depth,
                "allowed_tools": allowed_tools,
                "queue_backend": self.queue_backend,
            },
        )
        if self.queue_backend == "inline":
            if self._async_loop is None or not self._async_ready.is_set():
                raise RuntimeError("subagent async runtime is not available")
            cancellation = threading.Event()
            with self._lock:
                self._cancellations[task_id] = cancellation
            future = asyncio.run_coroutine_threadsafe(
                self._run_inline_async(record, cancellation), self._async_loop
            )
            with self._lock:
                self._futures[task_id] = future
            future.add_done_callback(
                lambda _future, submitted_task_id=task_id: self._forget_inline(
                    submitted_task_id
                )
            )
            if future.done():
                with self._lock:
                    self._futures.pop(task_id, None)
                    self._cancellations.pop(task_id, None)
        return record

    def _forget_inline(self, task_id: str) -> None:
        with self._lock:
            self._futures.pop(task_id, None)
            self._cancellations.pop(task_id, None)

    def get(self, task_id: str, tenant_id: str) -> SubagentTaskRecord | None:
        return self.store.get_task(task_id, tenant_id)

    def list(
        self, tenant_id: str, parent_session_id: str | None = None
    ) -> list[SubagentTaskRecord]:
        return self.store.list_tasks(tenant_id, parent_session_id)

    def wait(
        self,
        task_id: str,
        tenant_id: str,
        timeout: float | None = None,
        cancellation_event: threading.Event | None = None,
    ) -> SubagentTaskRecord:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if cancellation_event is not None and cancellation_event.is_set():
                self.cancel(task_id, tenant_id)
            record = self.get(task_id, tenant_id)
            if record is None:
                raise KeyError("subagent task not found")
            if record.status in TERMINAL_SUBAGENT_STATUSES:
                return record
            if deadline is not None and time.monotonic() >= deadline:
                return record
            time.sleep(0.05)

    async def wait_async(
        self,
        task_id: str,
        tenant_id: str,
        timeout: float | None = None,
        cancellation_event: threading.Event | None = None,
    ) -> SubagentTaskRecord:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if cancellation_event is not None and cancellation_event.is_set():
                self.cancel(task_id, tenant_id)
            record = self.get(task_id, tenant_id)
            if record is None:
                raise KeyError("subagent task not found")
            if record.status in TERMINAL_SUBAGENT_STATUSES:
                return record
            if deadline is not None and time.monotonic() >= deadline:
                return record
            await asyncio.sleep(0.05)

    def cancel(self, task_id: str, tenant_id: str) -> SubagentTaskRecord:
        record = self.get(task_id, tenant_id)
        if record is None:
            raise KeyError("subagent task not found")
        if record.status not in {"queued", "running", "cancel_requested"}:
            return record

        if record.status != "cancel_requested":
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

        if self.queue_backend == "inline":
            with self._lock:
                cancellation = self._cancellations.get(task_id)
            if cancellation is not None:
                cancellation.set()

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
        return self.get(task_id, tenant_id) or updated

    def shutdown(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            cancellations = list(self._cancellations.values())
            futures = list(self._futures.values())
        for cancellation in cancellations:
            cancellation.set()
        deadline = time.monotonic() + 2
        for future in futures:
            try:
                future.result(timeout=max(0.0, deadline - time.monotonic()))
            except Exception:
                if not future.done():
                    future.cancel()
        if self._async_loop is not None and self._async_loop.is_running():
            self._async_loop.call_soon_threadsafe(self._async_loop.stop)
        if self._async_thread is not None:
            self._async_thread.join(timeout=2)
        if (
            self._async_loop is not None
            and not self._async_loop.is_running()
            and not self._async_loop.is_closed()
        ):
            self._async_loop.close()


def register_subagent_tool(
    registry: ToolRegistry, manager: SubagentManager
) -> None:
    def task_projection(task: SubagentTaskRecord, *, answer_chars: int) -> dict:
        answer = task.answer or ""
        truncated = len(answer) > answer_chars
        return {
            "task_id": task.task_id,
            "child_session_id": task.child_session_id,
            "agent_id": task.agent_id,
            "status": task.status,
            "answer": answer[:answer_chars] + ("…" if truncated else ""),
            "answer_truncated": truncated,
            "error": task.error,
        }

    def delegate(
        arguments: DelegateSubagentArguments,
        context: ToolExecutionContext,
    ):
        resolved_timeout = arguments.timeout_seconds
        if not arguments.run_in_background and resolved_timeout is None:
            resolved_timeout = min(
                manager.settings.subagent_default_timeout_seconds, 170.0
            )
        task = manager.submit(
            SubagentSubmitRequest(
                agent_id=arguments.agent_id,
                objective=arguments.objective,
                parent_session_id=context.session_id,
                model_id=context.model_id,
                connection_ids=list(context.connection_ids),
                resource_scope={
                    key: list(values) for key, values in context.resource_scope.items()
                },
                allowed_tools=arguments.allowed_tools,
                timeout_seconds=resolved_timeout,
                token_budget=arguments.token_budget,
                wait=not arguments.run_in_background,
            ),
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            role=context.role,
            depth=context.delegation_depth + 1,
        )
        if arguments.run_in_background:
            return task_projection(task, answer_chars=0)
        resolved = manager.wait(
            task.task_id,
            context.tenant_id,
            timeout=task.timeout_seconds + 2,
            cancellation_event=context.cancellation_event,
        )
        return task_projection(resolved, answer_chars=2800)

    registry.register(
        ToolDefinition(
            name="delegate_subagent",
            description=(
                "把单一领域任务委派给分析 Agent。明确属于 Amazon、利润或 ERP 时选择"
                "对应专业 Agent；领域不明确时选择 analyst。objective 写清完整用户目标。"
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

    def delegate_specialists(
        arguments: DelegateSpecialistsArguments,
        context: ToolExecutionContext,
    ):
        timeout = arguments.timeout_seconds or min(
            manager.settings.subagent_default_timeout_seconds,
            170.0,
        )
        submitted = [
            manager.submit(
                SubagentSubmitRequest(
                    agent_id=item.agent_id,
                    objective=item.objective,
                    parent_session_id=context.session_id,
                    model_id=context.model_id,
                    connection_ids=list(context.connection_ids),
                    resource_scope={
                        key: list(values)
                        for key, values in context.resource_scope.items()
                    },
                    timeout_seconds=timeout,
                    token_budget=arguments.token_budget,
                ),
                tenant_id=context.tenant_id,
                user_id=context.user_id,
                role=context.role,
                depth=context.delegation_depth + 1,
            )
            for item in arguments.tasks
        ]
        deadline = time.monotonic() + timeout + 2
        completed = []
        answer_chars = max(500, min(1400, 3600 // len(submitted)))
        for task in submitted:
            remaining = max(0.0, deadline - time.monotonic())
            completed.append(
                task_projection(
                    manager.wait(
                    task.task_id,
                    context.tenant_id,
                    timeout=remaining,
                    cancellation_event=context.cancellation_event,
                    ),
                    answer_chars=answer_chars,
                )
            )
        return {"tasks": completed, "count": len(completed)}

    registry.register(
        ToolDefinition(
            name="delegate_specialists",
            description=(
                "跨多个独立业务领域时并行委派 1 到 3 个专业 Analyst，并等待全部结果。"
                "每个任务选择 amazon-finance-analyst、profit-analyst 或 erp-analyst；"
                "任务可以使用相同专业角色，但同领域统计应优先合并为一次查询。"
            ),
            arguments_model=DelegateSpecialistsArguments,
            handler=delegate_specialists,
            risk="low",
            timeout_seconds=180,
            concurrency_safe=True,
            source="subagent",
            builtin=True,
            allowed_roles=frozenset({"operator", "admin"}),
        )
    )
