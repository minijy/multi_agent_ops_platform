from __future__ import annotations

import json
import threading
import time
import uuid
from typing import Any, Callable, Literal, TypedDict, TYPE_CHECKING
from contextvars import ContextVar

from langgraph.graph import END, START, StateGraph
from queue import Queue

from ..config import Settings
from .domain import (
    AttachmentReference,
    ModelTurn,
    RuntimeAgentRequest,
    RuntimeAgentResponse,
    ToolCall,
    ToolResult,
)
from .attachments import LocalAttachmentStore
from .model_router import ModelRouter
from .memory import (
    MemoryService,
    explicit_forget_requested,
    explicit_remember_requested,
    memory_prompt,
)
from .model_errors import ModelProviderError
from .governance import RuntimeGovernanceStore
from .observability import MetricsStore, TurnMetric, usage_from_events
from .session_events import SessionEvent, SessionEventStore
from .skills import SkillRegistry
from .tools import ToolExecutionContext, ToolExecutor, ToolRegistry
from .result_store import materialize_tool_output
from ..source_privacy import (
    StreamingPublicTextSanitizer,
    sanitize_public_text,
    sanitize_public_value,
)
from ..agent_roles import (
    ANALYST_AGENT_ID,
    ANALYST_SYSTEM_PROMPT,
    AMAZON_FINANCE_ANALYST_ID,
    AMAZON_FINANCE_ANALYST_PROMPT,
    COORDINATOR_AGENT_ID,
    COORDINATOR_SYSTEM_PROMPT,
    DATA_QUERY_TOOL_NAMES,
    ERP_ANALYST_ID,
    ERP_ANALYST_PROMPT,
    PROFIT_ANALYST_ID,
    PROFIT_ANALYST_PROMPT,
    SPECIALIST_ANALYST_IDS,
    SYSTEM_DEFAULT_TOOL_NAMES,
)
from .agent_tool_policy import (
    active_data_query_tools,
    coordinator_delegation_prompt,
    data_tool_usage_prompt,
    resolve_agent_tool_allowlist,
    skill_names_for_tools,
)
from .tracing import span

if TYPE_CHECKING:
    from ..agent_registry import AgentRegistry
    from ..connections import ConnectionRegistry
    from ..model_registry import ModelRegistry


SYSTEM_PROMPT = COORDINATOR_SYSTEM_PROMPT

_stream_events: ContextVar[Callable[[dict[str, Any]], None] | None] = ContextVar(
    "agent_stream_events", default=None
)


class SessionLiveHub:
    """Let a refreshed browser attach to an in-flight agent turn."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._inflight: set[str] = set()
        self._subscribers: dict[str, list[Queue[dict[str, Any] | None]]] = {}
        self._controls: dict[str, threading.Event] = {}

    def begin(self, session_id: str, control: threading.Event) -> None:
        with self._lock:
            self._inflight.add(session_id)
            self._controls[session_id] = control

    def is_inflight(self, session_id: str) -> bool:
        with self._lock:
            return session_id in self._inflight

    def interrupt(self, session_id: str) -> bool:
        with self._lock:
            control = self._controls.get(session_id)
        if control is None:
            return False
        control.set()
        return True

    def publish(self, session_id: str | None, item: dict[str, Any]) -> None:
        if not session_id:
            return
        with self._lock:
            subscribers = list(self._subscribers.get(session_id, ()))
        for queue in subscribers:
            queue.put(item)

    def subscribe(
        self, session_id: str
    ) -> tuple[Queue[dict[str, Any] | None] | None, bool]:
        queue: Queue[dict[str, Any] | None] = Queue()
        with self._lock:
            live = session_id in self._inflight
            if live:
                self._subscribers.setdefault(session_id, []).append(queue)
                return queue, True
        return None, False

    def unsubscribe(
        self, session_id: str, queue: Queue[dict[str, Any] | None]
    ) -> None:
        with self._lock:
            subscribers = self._subscribers.get(session_id, [])
            if queue in subscribers:
                subscribers.remove(queue)

    def end(self, session_id: str) -> None:
        with self._lock:
            self._inflight.discard(session_id)
            self._controls.pop(session_id, None)
            subscribers = list(self._subscribers.pop(session_id, ()))
        for queue in subscribers:
            queue.put(None)


def turn_is_open(events: list[SessionEvent]) -> bool:
    open_turn = False
    for event in events:
        if event.event_type == "user.message":
            open_turn = True
        elif event.event_type == "turn.completed":
            open_turn = False
    return open_turn


class RuntimeState(TypedDict):
    messages: list[dict[str, Any]]
    session_id: str
    tenant_id: str
    user_id: str
    role: str
    model_id: str
    required_modalities: set[str]
    pending_calls: list[dict[str, Any]]
    tool_results: list[dict[str, Any]]
    tool_steps: int
    answer: str
    provider: str
    model: str
    approved_call_ids: list[str]
    allowed_tools: list[str] | None
    agent_id: str
    delegation_depth: int
    connection_ids: list[str]
    resource_scope: dict[str, list[str]]
    connection_scope_enforced: bool
    waiting_approval: bool
    pending_approval_ids: list[str]
    cancellation_event: Any
    interruption_is_resumable: bool
    deadline: float | None
    token_budget: int
    tokens_used: int
    status: str
    explicit_memory_consent: bool
    explicit_memory_forget: bool
    memory_snapshot: list[dict[str, Any]]


class AgentRuntime:
    def __init__(
        self,
        *,
        router: ModelRouter,
        registry: ToolRegistry,
        executor: ToolExecutor,
        event_store: SessionEventStore,
        attachment_store: LocalAttachmentStore | None = None,
        skill_registry: SkillRegistry | None = None,
        governance_store: RuntimeGovernanceStore | None = None,
        metrics_store: MetricsStore | None = None,
        max_tool_steps: int = 8,
        max_attachments_per_message: int = 20,
        settings: Settings | None = None,
        agent_registry: AgentRegistry | None = None,
        model_registry: ModelRegistry | None = None,
        connection_registry: ConnectionRegistry | None = None,
        access_control=None,
        tool_bindings=None,
        result_store=None,
        memory_service: MemoryService | None = None,
    ) -> None:
        self.router = router
        self.registry = registry
        self.executor = executor
        self.event_store = event_store
        self.attachment_store = attachment_store
        self.skill_registry = skill_registry
        self.governance_store = governance_store
        self.metrics_store = metrics_store
        self.max_tool_steps = max_tool_steps
        self.max_attachments_per_message = max_attachments_per_message
        self.settings = settings
        self.agent_registry = agent_registry
        self.model_registry = model_registry
        self.connection_registry = connection_registry
        self.access_control = access_control
        self.tool_bindings = tool_bindings
        self.result_store = result_store
        self.memory_service = memory_service
        self.live_hub = SessionLiveHub()
        self.graph = self._build_graph()

    def reload_router(self, router: ModelRouter) -> None:
        self.router = router

    def _active_data_tools_for_prompt(
        self, allowed_tools: set[str] | None
    ) -> frozenset[str]:
        if allowed_tools is not None:
            return frozenset(
                tool for tool in allowed_tools if tool in DATA_QUERY_TOOL_NAMES
            )
        if self.agent_registry is None or self.settings is None:
            return frozenset()
        return active_data_query_tools(
            self.agent_registry, self.settings, self.connection_registry
        )

    def _default_prompt_for(self, agent_id: str) -> str:
        prompts = {
            ANALYST_AGENT_ID: ANALYST_SYSTEM_PROMPT,
            AMAZON_FINANCE_ANALYST_ID: AMAZON_FINANCE_ANALYST_PROMPT,
            PROFIT_ANALYST_ID: PROFIT_ANALYST_PROMPT,
            ERP_ANALYST_ID: ERP_ANALYST_PROMPT,
        }
        if agent_id in prompts:
            return prompts[agent_id]
        return COORDINATOR_SYSTEM_PROMPT

    def _base_system_prompt(
        self,
        allowed_tools: set[str] | None = None,
        agent_id: str = COORDINATOR_AGENT_ID,
        tenant_id: str | None = None,
        user_id: str | None = None,
        role: str | None = None,
    ) -> str:
        prompt = self._default_prompt_for(agent_id)
        if self.agent_registry is not None:
            config = self.agent_registry.get(agent_id)
            if config is not None:
                prompt = config.effective_system_prompt(prompt)
        if agent_id == ANALYST_AGENT_ID or agent_id in SPECIALIST_ANALYST_IDS:
            prompt += data_tool_usage_prompt(
                self._active_data_tools_for_prompt(allowed_tools)
            )
        else:
            principal_data_tools = None
            if self.access_control is not None and tenant_id and user_id:
                access = self.access_control.effective_access(
                    tenant_id, user_id, role
                )
                if access.allowed_tools is not None:
                    principal_data_tools = (
                        set(access.allowed_tools) & DATA_QUERY_TOOL_NAMES
                    )
            prompt += coordinator_delegation_prompt(
                self.agent_registry,
                self.settings.analyst_mode if self.settings is not None else "general",
                principal_data_tools,
            )
        return prompt

    def _agent_allowed_tools(
        self,
        agent_id: str,
        tenant_id: str | None = None,
        user_id: str | None = None,
        role: str | None = None,
    ) -> set[str] | None:
        if self.agent_registry is None or self.settings is None:
            return None
        config = self.agent_registry.get(agent_id)
        if config is None or not config.enabled:
            return set()
        allowed = resolve_agent_tool_allowlist(
            config,
            self.agent_registry,
            self.settings,
            self.registry,
            self.connection_registry,
            tenant_id,
        )
        if self.access_control is not None and tenant_id and user_id:
            decision = self.access_control.effective_access(tenant_id, user_id, role)
            if decision.allowed_tools is not None:
                allowed &= set(decision.allowed_tools)
        return allowed

    def _runtime_allowed_tools(self) -> set[str] | None:
        return self._agent_allowed_tools(COORDINATOR_AGENT_ID)

    @staticmethod
    def _check_control(state: RuntimeState) -> None:
        cancellation = state.get("cancellation_event")
        if cancellation is not None and cancellation.is_set():
            raise RuntimeError("agent execution cancelled")
        deadline = state.get("deadline")
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("agent execution deadline exceeded")

    @staticmethod
    def _visible_answer(result: dict[str, Any], fallback: str) -> str:
        answer = str(result.get("answer") or "").strip()
        deferred_markers = (
            "正在收集",
            "正在获取",
            "正在查询",
            "请稍等",
            "请稍候",
            "稍后反馈",
            "完成后反馈",
            "完成后汇总",
        )
        specialist_sections: list[str] = []
        for item in result.get("tool_results") or []:
            if (
                not isinstance(item, dict)
                or not item.get("ok")
                or item.get("tool_name") != "delegate_specialists"
            ):
                continue
            output = item.get("output")
            if not isinstance(output, dict):
                continue
            for task in output.get("tasks") or []:
                if not isinstance(task, dict):
                    continue
                task_answer = str(task.get("answer") or "").strip()
                if not task_answer:
                    continue
                label = str(task.get("agent_id") or "专业 Analyst")
                specialist_sections.append(f"### {label}\n{task_answer}")
        if specialist_sections and (
            not answer or any(marker in answer for marker in deferred_markers)
        ):
            return sanitize_public_text("\n\n".join(specialist_sections)[:12000])
        if answer:
            return sanitize_public_text(answer)
        for item in reversed(result.get("tool_results") or []):
            if not isinstance(item, dict) or not item.get("ok"):
                continue
            output = item.get("output")
            if output is None:
                continue
            if isinstance(output, str) and output.strip():
                return sanitize_public_text(output.strip()[:4000])
            text = json.dumps(output, ensure_ascii=False, default=str)
            if text and text not in {"{}", "[]", "null"}:
                return sanitize_public_text(text[:4000])
        return sanitize_public_text(fallback)

    def _context(self, state: RuntimeState) -> ToolExecutionContext:
        allowed = state.get("allowed_tools")
        return ToolExecutionContext(
            session_id=state["session_id"],
            tenant_id=state["tenant_id"],
            user_id=state["user_id"],
            role=state["role"],
            approved_call_ids=frozenset(state["approved_call_ids"]),
            allowed_tool_names=frozenset(allowed) if allowed is not None else None,
            delegation_depth=state["delegation_depth"],
            agent_id=state["agent_id"],
            model_id=state["model_id"],
            connection_ids=tuple(state["connection_ids"]),
            resource_scope={
                name: tuple(values)
                for name, values in state["resource_scope"].items()
            },
            connection_scope_enforced=state["connection_scope_enforced"],
            deadline=state["deadline"],
            cancellation_event=state["cancellation_event"],
            explicit_memory_consent=bool(state.get("explicit_memory_consent", False)),
            explicit_memory_forget=bool(state.get("explicit_memory_forget", False)),
            memory_snapshot=tuple(state.get("memory_snapshot") or []),
        )

    def _append_event(
        self,
        state: RuntimeState,
        event_type: str,
        payload: dict[str, Any],
    ) -> SessionEvent:
        event = self.event_store.append(
            session_id=state["session_id"],
            tenant_id=state["tenant_id"],
            user_id=state["user_id"],
            event_type=event_type,
            payload=payload,
        )
        self._emit_stream(
            {
                "type": event_type,
                "session_id": state["session_id"],
                "payload": payload,
                "created_at": event.created_at,
            }
        )
        return event

    def _emit_stream(self, payload: dict[str, Any]) -> None:
        session_id = str(payload.get("session_id") or "")
        if session_id:
            self.live_hub.publish(session_id, payload)
        listener = _stream_events.get()
        if listener is not None:
            listener(payload)

    @staticmethod
    def _assistant_message(turn: ModelTurn) -> dict[str, Any]:
        message: dict[str, Any] = {
            "role": "assistant",
            "content": turn.content or None,
        }
        if turn.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.call_id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(
                            call.arguments, ensure_ascii=False, default=str
                        ),
                    },
                }
                for call in turn.tool_calls
            ]
        elif message["content"] is None:
            message["content"] = ""
        if turn.reasoning_content:
            message["reasoning_content"] = turn.reasoning_content
        return message

    def _normalize_specialist_delegations(
        self,
        calls: list[ToolCall],
        context: ToolExecutionContext,
    ) -> tuple[list[ToolCall], int, int]:
        """Route legacy single delegations through bounded parallel batches."""
        try:
            self.registry.get("delegate_specialists", context)
        except (KeyError, PermissionError):
            return calls, 0, 0

        eligible: list[tuple[int, ToolCall]] = []
        for index, call in enumerate(calls):
            arguments = call.arguments
            if (
                call.name == "delegate_subagent"
                and arguments.get("agent_id") in SPECIALIST_ANALYST_IDS
                and not arguments.get("run_in_background", False)
            ):
                eligible.append((index, call))
        if not eligible:
            return calls, 0, 0

        batches: list[ToolCall] = []
        for start in range(0, len(eligible), 3):
            group = [call for _index, call in eligible[start : start + 3]]
            timeouts = [
                float(call.arguments["timeout_seconds"])
                for call in group
                if call.arguments.get("timeout_seconds") is not None
            ]
            budgets = [
                int(call.arguments["token_budget"])
                for call in group
                if call.arguments.get("token_budget") is not None
            ]
            arguments: dict[str, Any] = {
                "tasks": [
                    {
                        "agent_id": call.arguments["agent_id"],
                        "objective": call.arguments["objective"],
                    }
                    for call in group
                ]
            }
            if timeouts:
                arguments["timeout_seconds"] = min(170.0, max(timeouts))
            if budgets:
                arguments["token_budget"] = max(budgets)
            batches.append(
                ToolCall(
                    call_id=f"parallel-{group[0].call_id}",
                    name="delegate_specialists",
                    arguments=arguments,
                )
            )

        eligible_indexes = {index for index, _call in eligible}
        insertion_index = eligible[0][0]
        normalized: list[ToolCall] = []
        for index, call in enumerate(calls):
            if index == insertion_index:
                normalized.extend(batches)
            if index not in eligible_indexes:
                normalized.append(call)
        return normalized, len(eligible), len(batches)

    @staticmethod
    def _delegation_objective_text(value: Any) -> str:
        """Flatten provider-specific rich text into the Tool's string contract."""
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, dict):
            for key in ("content", "text", "objective", "value"):
                if key in value:
                    text = AgentRuntime._delegation_objective_text(value[key])
                    if text:
                        return text
            return ""
        if isinstance(value, list):
            parts = [AgentRuntime._delegation_objective_text(item) for item in value]
            return "\n".join(part for part in parts if part).strip()
        if value is None:
            return ""
        return str(value).strip()

    def _canonicalize_delegation_arguments(
        self,
        calls: list[ToolCall],
    ) -> tuple[list[ToolCall], int, int]:
        """Normalize delegation arguments and coalesce same-specialist work."""
        normalized_calls: list[ToolCall] = []
        normalized_objectives = 0
        merged_tasks = 0

        for call in calls:
            arguments = dict(call.arguments)
            if call.name == "delegate_subagent" and "objective" in arguments:
                objective = arguments["objective"]
                text = self._delegation_objective_text(objective)
                if text and not isinstance(objective, str):
                    arguments["objective"] = text[:4000]
                    normalized_objectives += 1

            elif call.name == "delegate_specialists" and isinstance(
                arguments.get("tasks"), list
            ):
                tasks_by_agent: dict[str, list[str]] = {}
                task_templates: dict[str, dict[str, Any]] = {}
                passthrough_tasks: list[Any] = []
                agent_order: list[str] = []
                for raw_task in arguments["tasks"]:
                    if not isinstance(raw_task, dict):
                        passthrough_tasks.append(raw_task)
                        continue
                    task = dict(raw_task)
                    raw_objective = task.get("objective")
                    objective = self._delegation_objective_text(raw_objective)
                    if objective and not isinstance(raw_objective, str):
                        normalized_objectives += 1
                    agent_id = task.get("agent_id")
                    if not isinstance(agent_id, str) or not objective:
                        if objective:
                            task["objective"] = objective[:4000]
                        passthrough_tasks.append(task)
                        continue
                    if agent_id not in tasks_by_agent:
                        agent_order.append(agent_id)
                        tasks_by_agent[agent_id] = []
                        task_templates[agent_id] = task
                    if objective not in tasks_by_agent[agent_id]:
                        tasks_by_agent[agent_id].append(objective)

                canonical_tasks: list[Any] = []
                for agent_id in agent_order:
                    objectives = tasks_by_agent[agent_id]
                    task = task_templates[agent_id]
                    if len(objectives) == 1:
                        combined = objectives[0]
                    else:
                        combined = "请在一次查询和分析中同时完成：\n" + "\n".join(
                            f"{index}. {objective}"
                            for index, objective in enumerate(objectives, start=1)
                        )
                        merged_tasks += len(objectives) - 1
                    task["objective"] = combined[:4000]
                    canonical_tasks.append(task)
                arguments["tasks"] = [*canonical_tasks, *passthrough_tasks]

            normalized_calls.append(call.model_copy(update={"arguments": arguments}))

        return normalized_calls, normalized_objectives, merged_tasks

    @staticmethod
    def _repair_delegation_mode(
        calls: list[ToolCall],
        visible_tool_names: set[str],
    ) -> tuple[list[ToolCall], int]:
        """Translate stale specialist calls after a session switches to general mode."""
        if (
            "delegate_subagent" not in visible_tool_names
            or "delegate_specialists" in visible_tool_names
        ):
            return calls, 0
        repaired: list[ToolCall] = []
        repaired_count = 0
        for call in calls:
            if call.name != "delegate_specialists":
                repaired.append(call)
                continue
            tasks = call.arguments.get("tasks")
            if not isinstance(tasks, list):
                repaired.append(call)
                continue
            objectives = [
                str(task.get("objective") or "").strip()
                for task in tasks
                if isinstance(task, dict)
                and str(task.get("objective") or "").strip()
            ]
            if not objectives:
                repaired.append(call)
                continue
            objective = (
                objectives[0]
                if len(objectives) == 1
                else "请在一次分析中同时完成：\n"
                + "\n".join(
                    f"{index}. {item}"
                    for index, item in enumerate(objectives, start=1)
                )
            )
            arguments: dict[str, Any] = {
                "agent_id": ANALYST_AGENT_ID,
                "objective": objective[:4000],
                "run_in_background": False,
            }
            for name in ("timeout_seconds", "token_budget"):
                if call.arguments.get(name) is not None:
                    arguments[name] = call.arguments[name]
            repaired.append(
                call.model_copy(
                    update={
                        "name": "delegate_subagent",
                        "arguments": arguments,
                    }
                )
            )
            repaired_count += 1
        return repaired, repaired_count

    @staticmethod
    def _merge_session_tool_snapshot(
        current_tools: set[str] | None,
        snapshotted_tools: set[str],
    ) -> set[str]:
        """Keep business grants frozen while refreshing built-in orchestration tools."""
        if current_tools is None:
            return set(snapshotted_tools)
        return current_tools & (snapshotted_tools | SYSTEM_DEFAULT_TOOL_NAMES)

    def _live_connector_scope(
        self,
        tenant_id: str,
        allowed_tools: set[str] | None,
    ) -> tuple[list[str], dict[str, list[str]]]:
        if self.connection_registry is None:
            return [], {}
        if self.tool_bindings is not None:
            return self.tool_bindings.execution_scope(
                tenant_id, allowed_tools, self.connection_registry
            )
        connections = self.connection_registry.list_for_tenant(tenant_id)
        return [item.id for item in connections], {
            name: list(values)
            for connection in connections
            for name, values in connection.resource_scopes.items()
        }

    @staticmethod
    def _merge_connector_scope(
        base_ids: list[str],
        base_scope: dict[str, list[str]],
        live_ids: list[str],
        live_scope: dict[str, list[str]],
    ) -> tuple[list[str], dict[str, list[str]]]:
        merged_scope = {
            str(name): [str(value) for value in values]
            for name, values in (base_scope or {}).items()
        }
        for name, values in (live_scope or {}).items():
            merged_scope[str(name)] = sorted(
                set(merged_scope.get(str(name), [])) | {str(item) for item in values}
            )
        return sorted({*base_ids, *live_ids}), merged_scope

    def _connection_scope_for_session(
        self,
        *,
        tenant_id: str,
        allowed_tools: set[str] | None,
        parent_session_id: str | None,
        created: SessionEvent | None,
        requested_ids: list[str] | None = None,
        requested_scope: dict[str, list[str]] | None = None,
    ) -> tuple[list[str], dict[str, list[str]]]:
        session_parent = parent_session_id
        if created and created.payload.get("parent_session_id"):
            session_parent = str(created.payload["parent_session_id"])
        if created and "connection_ids" in created.payload:
            connection_ids = [
                str(item) for item in created.payload.get("connection_ids") or []
            ]
            resource_scope = {
                str(name): [str(value) for value in values]
                for name, values in dict(
                    created.payload.get("resource_scope") or {}
                ).items()
            }
        elif requested_ids is not None:
            connection_ids = [str(item) for item in requested_ids]
            resource_scope = {
                str(name): [str(value) for value in values]
                for name, values in (requested_scope or {}).items()
            }
        elif self.connection_registry is not None:
            connection_ids, resource_scope = self._live_connector_scope(
                tenant_id, allowed_tools
            )
        else:
            connection_ids, resource_scope = [], {}
        if not session_parent and self.connection_registry is not None:
            live_ids, live_scope = self._live_connector_scope(tenant_id, allowed_tools)
            connection_ids, resource_scope = self._merge_connector_scope(
                connection_ids, resource_scope, live_ids, live_scope
            )
        return list(connection_ids or []), {
            str(name): [str(value) for value in values]
            for name, values in (resource_scope or {}).items()
        }

    @staticmethod
    def _message_text(message: dict[str, Any]) -> str:
        content = message.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(
                str(item.get("text", ""))
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            )
        return str(content or "")

    def _recover_claimed_delegation(
        self,
        turn: ModelTurn,
        state: RuntimeState,
        schemas: list[dict[str, Any]],
    ) -> ModelTurn:
        """Enforce a real blocking delegation for Coordinator data requests."""
        if turn.tool_calls or state["agent_id"] != COORDINATOR_AGENT_ID:
            return turn
        last_user_index = max(
            (
                index
                for index, message in enumerate(state["messages"])
                if message.get("role") == "user"
            ),
            default=-1,
        )
        messages_after_user = state["messages"][last_user_index + 1 :]
        if any(
            message.get("role") == "tool"
            and message.get("name") in {"delegate_subagent", "delegate_specialists"}
            for message in messages_after_user
        ):
            return turn
        objective = next(
            (
                self._message_text(message).strip()
                for message in reversed(state["messages"])
                if message.get("role") == "user"
                and self._message_text(message).strip()
            ),
            "完成用户的数据分析请求",
        )
        content = str(turn.content or "")
        role_claimed = any(
            marker in content for marker in ("分析师", "子代理", "子 Agent", "子Agent")
        )
        action_claimed = any(
            marker in content
            for marker in (
                "已经向", "已向", "已委派", "已经委派", "发送了请求", "正在收集"
            )
        )
        lowered_objective = objective.lower()
        data_subject = any(
            marker in lowered_objective
            for marker in (
                "msku", "sku", "asin", "amazon", "亚马逊", "利润", "毛利",
                "销售量", "销售额", "订单", "收入", "成本", "费用", "结算",
                "退款", "领星", "金蝶", "出库", "应收", "回款", "报表",
            )
        )
        data_operation = any(
            marker in lowered_objective
            for marker in (
                "查询", "汇总", "统计", "分析", "对比", "趋势", "按月", "按日",
                "top", "多少", "合计", "明细",
            )
        )
        data_request = data_subject and data_operation
        false_delegation_claim = role_claimed and action_claimed
        if not (false_delegation_claim or data_request):
            return turn
        available = {
            str(item.get("function", {}).get("name") or "") for item in schemas
        }
        if not ({"delegate_subagent", "delegate_specialists"} & available):
            return turn
        if "delegate_specialists" in available:
            lowered = objective.lower()
            allowed_specialists = set(SPECIALIST_ANALYST_IDS)
            access_control = getattr(self, "access_control", None)
            if access_control is not None:
                access = access_control.effective_access(
                    state["tenant_id"], state["user_id"], state["role"]
                )
                if access.allowed_tools is not None:
                    agent_registry = getattr(self, "agent_registry", None)
                    allowed_specialists = {
                        agent_id
                        for agent_id in SPECIALIST_ANALYST_IDS
                        if agent_registry is not None
                        and (agent := agent_registry.get(agent_id)) is not None
                        and bool(
                            (set(agent.allowed_tools) & DATA_QUERY_TOOL_NAMES)
                            & set(access.allowed_tools)
                        )
                    }
            if not allowed_specialists:
                return turn
            specialist_keywords = (
                (
                    PROFIT_ANALYST_ID,
                    ("利润", "毛利", "销售量", "销售额", "msku", "订单", "收入", "成本"),
                ),
                (
                    AMAZON_FINANCE_ANALYST_ID,
                    ("亚马逊", "amazon", "结算", "退款", "费用", "asin"),
                ),
                (ERP_ANALYST_ID, ("金蝶", "出库", "应收", "回款", "erp")),
            )
            matched_agent_ids = [
                agent_id
                for agent_id, keywords in specialist_keywords
                if any(keyword in lowered for keyword in keywords)
            ]
            agent_ids = [
                agent_id
                for agent_id in matched_agent_ids
                if agent_id in allowed_specialists
            ][:3]
            if matched_agent_ids and not agent_ids:
                return turn
            if not agent_ids:
                agent_ids = [sorted(allowed_specialists)[0]]
            call = ToolCall(
                call_id=f"call-recovered-{uuid.uuid4().hex[:12]}",
                name="delegate_specialists",
                arguments={
                    "tasks": [
                        {"agent_id": agent_id, "objective": objective}
                        for agent_id in agent_ids
                    ]
                },
            )
        else:
            call = ToolCall(
                call_id=f"call-recovered-{uuid.uuid4().hex[:12]}",
                name="delegate_subagent",
                arguments={
                    "agent_id": ANALYST_AGENT_ID,
                    "objective": objective,
                    "run_in_background": False,
                },
            )
        self._append_event(
            state,
            "model.tool_call_recovered",
            {
                "reason": (
                    "model_claimed_delegation_without_tool_call"
                    if false_delegation_claim
                    else "data_request_requires_delegation"
                ),
                "tool_name": call.name,
            },
        )
        return turn.model_copy(update={"content": "", "tool_calls": [call]})

    @staticmethod
    def _wants_web_search(text: str) -> bool:
        value = str(text or "").strip().lower()
        if not value:
            return False
        markers = (
            "网页搜索",
            "网上搜索",
            "网上搜",
            "搜索网页",
            "搜网页",
            "上网搜",
            "上网查",
            "最新新闻",
            "实时新闻",
            "公开网页",
            "web search",
        )
        return any(marker in value for marker in markers)

    def _recover_web_search(
        self,
        turn: ModelTurn,
        state: RuntimeState,
        schemas: list[dict[str, Any]],
    ) -> ModelTurn:
        if turn.tool_calls or state["agent_id"] != COORDINATOR_AGENT_ID:
            return turn
        visible = {
            str(item.get("function", {}).get("name") or "") for item in schemas
        }
        if "web_search" not in visible:
            return turn
        last_user_index = max(
            (
                index
                for index, message in enumerate(state["messages"])
                if message.get("role") == "user"
            ),
            default=-1,
        )
        if last_user_index < 0:
            return turn
        if any(
            message.get("role") == "tool" and message.get("name") == "web_search"
            for message in state["messages"][last_user_index + 1 :]
        ):
            return turn
        objective = self._message_text(state["messages"][last_user_index]).strip()
        if not self._wants_web_search(objective):
            return turn
        call = ToolCall(
            call_id=f"call-recovered-{uuid.uuid4().hex[:12]}",
            name="web_search",
            arguments={"query": objective[:500], "max_results": 5, "search_depth": "basic"},
        )
        self._append_event(
            state,
            "model.tool_call_recovered",
            {
                "reason": "web_search_request_requires_tool_call",
                "tool_name": "web_search",
            },
        )
        return turn.model_copy(update={"content": "", "tool_calls": [call]})

    def _model_node(self, state: RuntimeState) -> dict[str, Any]:
        self._check_control(state)
        context = self._context(state)
        schemas = self.registry.schemas(context)
        route = self.router.route(
            model_id=state["model_id"],
            required_modalities=state["required_modalities"],
        )
        self._append_event(
            state,
            "model.request",
            {
                "model_id": route.model_id,
                "provider": route.provider,
                "model": route.model,
                "message_count": len(state["messages"]),
                "tools": [item["function"]["name"] for item in schemas],
                "required_modalities": sorted(state["required_modalities"]),
            },
        )
        try:
            listener = _stream_events.get()
            stream_sanitizer = StreamingPublicTextSanitizer()
            reasoning_sanitizer = StreamingPublicTextSanitizer()

            def on_token(text: str, channel: str = "content") -> None:
                self._check_control(state)
                if channel == "reasoning":
                    safe_text = reasoning_sanitizer.feed(text)
                    event_type = "reasoning"
                else:
                    safe_text = stream_sanitizer.feed(text)
                    event_type = "token"
                if not safe_text:
                    return
                self._emit_stream(
                    {
                        "type": event_type,
                        "text": safe_text,
                        "provider": route.provider,
                        "model": route.model,
                        "session_id": state["session_id"],
                    }
                )

            with span("model.invoke", provider=route.provider, model=route.model):
                turn = self.router.invoke(
                    self._prepare_model_messages(state["messages"]),
                    schemas,
                    model_id=state["model_id"],
                    required_modalities=state["required_modalities"],
                    on_token=on_token if listener is not None else None,
                )
            if listener is not None:
                remaining_text = stream_sanitizer.flush()
                if remaining_text:
                    self._emit_stream(
                        {
                            "type": "token",
                            "text": remaining_text,
                            "provider": route.provider,
                            "model": route.model,
                            "session_id": state["session_id"],
                        }
                    )
                remaining_reasoning = reasoning_sanitizer.flush()
                if remaining_reasoning:
                    self._emit_stream(
                        {
                            "type": "reasoning",
                            "text": remaining_reasoning,
                            "provider": route.provider,
                            "model": route.model,
                            "session_id": state["session_id"],
                        }
                    )
        except ModelProviderError as exc:
            self._append_event(
                state,
                "model.error",
                exc.as_dict(),
            )
            raise
        self._check_control(state)
        turn = self._recover_web_search(turn, state, schemas)
        turn = self._recover_claimed_delegation(turn, state, schemas)
        calls = turn.tool_calls
        visible_tool_names = {
            str(item.get("function", {}).get("name") or "")
            for item in schemas
        }
        answer = sanitize_public_text(turn.content)
        tokens_used = state["tokens_used"] + int(
            turn.usage.get("total_tokens", 0) or 0
        )
        budget_exceeded = tokens_used > state["token_budget"]
        if budget_exceeded:
            calls = []
            answer = (
                f"已达到本次执行的 Token 预算 {state['token_budget']}，任务已停止。"
            )
        else:
            calls, canonicalized_count, merged_task_count = (
                self._canonicalize_delegation_arguments(calls)
            )
            if canonicalized_count or merged_task_count:
                self._append_event(
                    state,
                    "delegation.arguments_normalized",
                    {
                        "normalized_objectives": canonicalized_count,
                        "merged_tasks": merged_task_count,
                    },
                )
            calls, mode_repaired_count = self._repair_delegation_mode(
                calls, visible_tool_names
            )
            if mode_repaired_count:
                self._append_event(
                    state,
                    "delegation.mode_repaired",
                    {
                        "from_tool": "delegate_specialists",
                        "to_tool": "delegate_subagent",
                        "count": mode_repaired_count,
                        "analyst_mode": "general",
                    },
                )
            calls, normalized_count, batch_count = (
                self._normalize_specialist_delegations(calls, context)
            )
            if normalized_count:
                self._append_event(
                    state,
                    "delegation.parallelized",
                    {
                        "original_calls": normalized_count,
                        "parallel_batches": batch_count,
                        "max_parallel": 3,
                    },
                )
            hidden_calls = [
                call.name for call in calls if call.name not in visible_tool_names
            ]
            if hidden_calls:
                calls = [
                    call for call in calls if call.name in visible_tool_names
                ]
                self._append_event(
                    state,
                    "model.tool_call_rejected",
                    {
                        "reason": "tool_not_visible",
                        "tool_names": sorted(set(hidden_calls)),
                    },
                )
                if not calls:
                    answer = (
                        "当前 Agent 无权调用模型请求的工具："
                        + "、".join(sorted(set(hidden_calls)))
                        + "。请检查当前 Analyst 模式和权限组配置。"
                    )
        if calls and state["tool_steps"] >= self.max_tool_steps:
            calls = []
            answer = f"已达到最大工具调用轮数 {self.max_tool_steps}，任务已停止。"
        self._append_event(
            state,
            "model.response",
            {
                "provider": turn.provider,
                "model": turn.model,
                "content": answer,
                "reasoning_content": turn.reasoning_content,
                "tool_calls": [item.model_dump(mode="json") for item in calls],
                "usage": turn.usage,
            },
        )
        effective_turn = turn.model_copy(update={"content": answer, "tool_calls": calls})
        return {
            "messages": [*state["messages"], self._assistant_message(effective_turn)],
            "pending_calls": [item.model_dump(mode="json") for item in calls],
            "answer": answer,
            "provider": turn.provider,
            "model": turn.model,
            "tokens_used": tokens_used,
            "status": "budget_exceeded" if budget_exceeded else state["status"],
        }

    def _tools_node(self, state: RuntimeState) -> dict[str, Any]:
        self._check_control(state)
        messages = list(state["messages"])
        results = [ToolResult.model_validate(item) for item in state["tool_results"]]
        context = self._context(state)
        pending_approval_ids = list(state["pending_approval_ids"])
        waiting_approval = False
        for raw_call in state["pending_calls"]:
            call = ToolCall.model_validate(raw_call)
            try:
                definition = self.registry.get(call.name, context)
            except (KeyError, PermissionError) as exc:
                result = ToolResult(
                    call_id=call.call_id,
                    tool_name=call.name,
                    ok=False,
                    error=str(exc),
                )
                results.append(result)
                self._append_event(
                    state,
                    "tool.completed",
                    result.model_dump(mode="json"),
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.call_id,
                        "name": call.name,
                        "content": json.dumps(
                            {"error": result.error, "ok": False},
                            ensure_ascii=False,
                        ),
                    }
                )
                continue
            self._append_event(
                state,
                "tool.requested",
                {
                    "call_id": call.call_id,
                    "tool_name": call.name,
                    "arguments": call.arguments,
                },
            )
            if (
                definition.requires_approval
                and call.call_id not in context.approved_call_ids
            ):
                if self.governance_store is None:
                    raise RuntimeError("runtime approval store is not configured")
                approval = self.governance_store.create_approval(
                    session_id=state["session_id"],
                    tenant_id=state["tenant_id"],
                    user_id=state["user_id"],
                    role=state["role"],
                    call=call,
                )
                pending_approval_ids.append(approval.approval_id)
                waiting_approval = True
                self._append_event(
                    state,
                    "approval.requested",
                    {
                        "approval_id": approval.approval_id,
                        "call_id": call.call_id,
                        "tool_name": call.name,
                        "arguments": call.arguments,
                        "risk": definition.risk,
                    },
                )
                continue
            result = self.executor.execute(call, context)
            self._check_control(state)
            result = result.model_copy(
                update={
                    "output": sanitize_public_value(result.output),
                    "error": (
                        sanitize_public_text(result.error)
                        if result.error is not None
                        else None
                    ),
                }
            )
            if result.ok and self.result_store is not None:
                result = result.model_copy(
                    update={
                        "output": materialize_tool_output(
                            self.result_store,
                            result.output,
                            tenant_id=state["tenant_id"],
                            user_id=state["user_id"],
                            session_id=state["session_id"],
                            tool_name=call.name,
                            preview_rows=self._tool_compact_limits()[0],
                        )
                    }
                )
            results.append(result)
            self._append_event(
                state,
                "tool.completed",
                result.model_dump(mode="json"),
            )
            model_content = (
                result.output if result.ok else {"error": result.error, "ok": False}
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.call_id,
                    "name": call.name,
                    "content": json.dumps(
                        model_content, ensure_ascii=False, default=str
                    ),
                }
            )
        return {
            "messages": messages,
            "pending_calls": [],
            "tool_results": [item.model_dump(mode="json") for item in results],
            "tool_steps": state["tool_steps"] + 1,
            "waiting_approval": waiting_approval,
            "pending_approval_ids": pending_approval_ids,
            "answer": (
                "高风险工具正在等待逐次人工审批。"
                if waiting_approval else state["answer"]
            ),
            "status": "waiting_approval" if waiting_approval else state["status"],
        }

    @staticmethod
    def _route_after_model(state: RuntimeState) -> Literal["tools", "__end__"]:
        return "tools" if state["pending_calls"] else END

    @staticmethod
    def _route_after_tools(state: RuntimeState) -> Literal["model", "__end__"]:
        return END if state["waiting_approval"] else "model"

    def _build_graph(self):
        graph = StateGraph(RuntimeState)
        graph.add_node("model", self._model_node)
        graph.add_node("tools", self._tools_node)
        graph.add_edge(START, "model")
        graph.add_conditional_edges("model", self._route_after_model)
        graph.add_conditional_edges("tools", self._route_after_tools)
        return graph.compile()

    def _user_message(
        self,
        text: str,
        attachment_ids: list[str],
        *,
        tenant_id: str,
    ) -> tuple[dict[str, Any], list[AttachmentReference]]:
        if not attachment_ids:
            return {"role": "user", "content": text}, []
        if self.attachment_store is None:
            raise ValueError("attachment storage is not configured")
        content: list[dict[str, Any]] = [{"type": "text", "text": text}]
        references: list[AttachmentReference] = []
        for attachment_id in attachment_ids:
            reference, _raw = self.attachment_store.get(
                attachment_id, tenant_id=tenant_id
            )
            references.append(reference)
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": self.attachment_store.data_url(
                            attachment_id, tenant_id=tenant_id
                        )
                    },
                }
            )
        return {"role": "user", "content": content}, references

    @staticmethod
    def _compact_tool_content(
        content: Any, *, max_rows: int = 12, max_chars: int = 4000
    ) -> str:
        payload: Any
        if isinstance(content, str):
            try:
                payload = json.loads(content)
            except json.JSONDecodeError:
                sanitized = sanitize_public_text(content)
                return sanitized if len(sanitized) <= max_chars else sanitized[:max_chars] + "…"
        else:
            payload = content
        payload = sanitize_public_value(payload)
        if not isinstance(payload, dict):
            text = json.dumps(payload, ensure_ascii=False, default=str)
            return text if len(text) <= max_chars else text[:max_chars] + "…"
        compact = dict(payload)
        rows = compact.get("rows")
        if isinstance(rows, list) and len(rows) > max_rows:
            compact["row_count"] = len(rows)
            compact["rows"] = rows[:max_rows]
            compact["rows_truncated"] = True
        text = json.dumps(compact, ensure_ascii=False, default=str)
        if len(text) <= max_chars:
            return text
        compact.pop("rows", None)
        compact["rows_omitted"] = True
        if isinstance(rows, list) and rows:
            compact["row_preview"] = rows[:3]
        text = json.dumps(compact, ensure_ascii=False, default=str)
        return text if len(text) <= max_chars else text[:max_chars] + "…"

    def _prepare_model_messages(
        self, messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        max_rows, max_chars = self._tool_compact_limits()
        prepared: list[dict[str, Any]] = []
        for message in messages:
            item = dict(message)
            if item.get("role") != "user" and isinstance(item.get("content"), str):
                item["content"] = sanitize_public_text(item["content"])
            if (
                item.get("role") == "assistant"
                and item.get("tool_calls")
                and not str(item.get("content") or "").strip()
            ):
                item["content"] = None
            if item.get("role") == "tool":
                item["content"] = self._compact_tool_content(
                    item.get("content", ""),
                    max_rows=max_rows,
                    max_chars=max_chars,
                )
            prepared.append(item)
        return self._apply_context_window(prepared)

    def _tool_compact_limits(self) -> tuple[int, int]:
        if self.settings is None:
            return 12, 4000
        return (
            self.settings.context_tool_max_rows,
            self.settings.context_tool_max_chars,
        )

    @staticmethod
    def _message_chars(message: dict[str, Any]) -> int:
        content = message.get("content")
        if isinstance(content, str):
            size = len(content)
        elif isinstance(content, list):
            size = sum(
                len(json.dumps(item, ensure_ascii=False, default=str))
                for item in content
            )
        elif content is None:
            size = 0
        else:
            size = len(json.dumps(content, ensure_ascii=False, default=str))
        tool_calls = message.get("tool_calls")
        if tool_calls:
            size += len(json.dumps(tool_calls, ensure_ascii=False, default=str))
        return size

    @staticmethod
    def _drop_oldest_exchange(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if len(messages) <= 1:
            return messages
        role = messages[0].get("role")
        if role == "user":
            end = 1
            while end < len(messages) and messages[end].get("role") != "user":
                end += 1
            return messages[end:]
        if role == "assistant":
            end = 1
            while end < len(messages) and messages[end].get("role") == "tool":
                end += 1
            return messages[end:]
        return messages[1:]

    def _apply_context_window(
        self, messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        enabled = True
        keep_turns = 16
        max_messages = 64
        max_chars = 80_000
        if self.settings is not None:
            enabled = self.settings.context_window_enabled
            keep_turns = self.settings.context_keep_recent_user_turns
            max_messages = self.settings.context_max_messages
            max_chars = self.settings.context_max_chars
        if not enabled:
            return messages
        systems = [item for item in messages if item.get("role") == "system"]
        rest = [item for item in messages if item.get("role") != "system"]
        user_indices = [
            index for index, item in enumerate(rest) if item.get("role") == "user"
        ]
        if keep_turns and len(user_indices) > keep_turns:
            rest = rest[user_indices[-keep_turns]:]

        def over_limit(items: list[dict[str, Any]]) -> bool:
            total = sum(self._message_chars(item) for item in systems) + sum(
                self._message_chars(item) for item in items
            )
            return len(items) > max_messages or total > max_chars

        while over_limit(rest) and sum(1 for item in rest if item.get("role") == "user") > 1:
            nxt = self._drop_oldest_exchange(rest)
            if len(nxt) >= len(rest):
                break
            rest = nxt
        return systems + rest

    def _restore_messages(
        self, events: list[SessionEvent], *, tenant_id: str
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        for event in events:
            if event.event_type == "user.message":
                message, _references = self._user_message(
                    str(event.payload.get("content", "")),
                    list(event.payload.get("attachment_ids", [])),
                    tenant_id=tenant_id,
                )
                messages.append(message)
            elif event.event_type == "model.response":
                turn = ModelTurn(
                    provider=str(event.payload.get("provider", "")),
                    model=str(event.payload.get("model", "")),
                    content=str(event.payload.get("content", "")),
                    reasoning_content=str(event.payload.get("reasoning_content") or ""),
                    tool_calls=[
                        ToolCall.model_validate(item)
                        for item in event.payload.get("tool_calls", [])
                    ],
                    usage=dict(event.payload.get("usage", {})),
                )
                messages.append(AgentRuntime._assistant_message(turn))
            elif event.event_type == "tool.completed":
                result = ToolResult.model_validate(event.payload)
                content = result.output if result.ok else {"error": result.error, "ok": False}
                max_rows, max_chars = self._tool_compact_limits()
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": result.call_id,
                        "name": result.tool_name,
                        "content": self._compact_tool_content(
                            json.dumps(content, ensure_ascii=False, default=str),
                            max_rows=max_rows,
                            max_chars=max_chars,
                        ),
                    }
                )
        return messages

    def run(
        self,
        request: RuntimeAgentRequest,
        *,
        tenant_id: str,
        user_id: str,
        role: str = "admin",
        allowed_tools: set[str] | None = None,
        agent_id: str | None = None,
        delegation_depth: int = 0,
        parent_session_id: str | None = None,
        connection_ids: list[str] | None = None,
        resource_scope: dict[str, list[str]] | None = None,
        memory_snapshot: list[dict[str, Any]] | None = None,
        cancellation_event: threading.Event | None = None,
        interruption_is_resumable: bool = False,
        timeout_seconds: float | None = None,
        token_budget: int = 30_000,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        resume: bool = False,
    ) -> RuntimeAgentResponse:
        if request.session_id:
            existing_events = self.event_store.list_events(
                session_id=request.session_id, tenant_id=tenant_id
            )
            owner = next(
                (
                    event.user_id
                    for event in existing_events
                    if event.event_type == "session.created"
                ),
                existing_events[0].user_id if existing_events else None,
            )
            if existing_events and owner != user_id:
                raise KeyError("session not found")
        if resume and request.session_id:
            previous = existing_events
            created = next(
                (
                    event
                    for event in previous
                    if event.event_type == "session.created"
                ),
                None,
            )
            if created and created.payload.get("agent_id"):
                agent_id = str(created.payload["agent_id"])
        if self.agent_registry is not None:
            resolved_agent_id = agent_id or COORDINATOR_AGENT_ID
            config = self.agent_registry.get(resolved_agent_id)
            if config is None or not config.enabled:
                raise ValueError(f"Agent is disabled: {resolved_agent_id}")
        else:
            resolved_agent_id = agent_id or COORDINATOR_AGENT_ID
        configured_tools = self._agent_allowed_tools(
            resolved_agent_id, tenant_id, user_id, role
        )
        if allowed_tools is None:
            allowed_tools = configured_tools
        elif configured_tools is not None:
            allowed_tools = allowed_tools & configured_tools
        token = _stream_events.set(on_event)
        started = time.perf_counter()
        try:
            with span(
                "agent.turn",
                tenant_id=tenant_id,
                user_id=user_id,
                role=role,
                agent_id=resolved_agent_id,
            ):
                try:
                    result = self._run_turn(
                        request,
                        tenant_id=tenant_id,
                        user_id=user_id,
                        role=role,
                        allowed_tools=allowed_tools,
                        agent_id=resolved_agent_id,
                        delegation_depth=delegation_depth,
                        parent_session_id=parent_session_id,
                        connection_ids=connection_ids,
                        resource_scope=resource_scope,
                        memory_snapshot=memory_snapshot,
                        cancellation_event=cancellation_event,
                        interruption_is_resumable=interruption_is_resumable,
                        timeout_seconds=timeout_seconds,
                        token_budget=token_budget,
                        resume=resume,
                    )
                except ModelProviderError as exc:
                    self._observe(
                        session_id=request.session_id or "",
                        tenant_id=tenant_id,
                        user_id=user_id,
                        status="failed",
                        provider=exc.provider if hasattr(exc, "provider") else "",
                        model="",
                        latency_ms=(time.perf_counter() - started) * 1000,
                        error_code=getattr(exc, "code", type(exc).__name__),
                    )
                    raise
                self._observe_result(
                    result,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    latency_ms=(time.perf_counter() - started) * 1000,
                )
                return result
        finally:
            _stream_events.reset(token)

    def continue_session(
        self,
        *,
        session_id: str,
        tenant_id: str,
        user_id: str,
        role: str = "admin",
        token_budget: int = 30_000,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        interruption_is_resumable: bool = False,
    ) -> RuntimeAgentResponse:
        return self.run(
            RuntimeAgentRequest(
                question="continue",
                session_id=session_id,
            ),
            tenant_id=tenant_id,
            user_id=user_id,
            role=role,
            token_budget=token_budget,
            on_event=on_event,
            interruption_is_resumable=interruption_is_resumable,
            resume=True,
        )

    def _observe_result(
        self,
        result: RuntimeAgentResponse,
        *,
        tenant_id: str,
        user_id: str,
        latency_ms: float,
    ) -> None:
        self._observe(
            session_id=result.session_id,
            tenant_id=tenant_id,
            user_id=user_id,
            status=result.status,
            provider=result.provider,
            model=result.model,
            latency_ms=latency_ms,
        )

    def _observe(
        self,
        *,
        session_id: str,
        tenant_id: str,
        user_id: str,
        status: str,
        provider: str,
        model: str,
        latency_ms: float,
        error_code: str = "",
    ) -> None:
        if self.metrics_store is None:
            return
        usage = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "tool_calls": 0,
            "tool_errors": 0,
            "estimated_cost_usd": 0.0,
            "provider": provider,
            "model": model,
        }
        if session_id:
            events = self.event_store.list_events(
                session_id=session_id, tenant_id=tenant_id
            )
            usage = {**usage, **usage_from_events(events)}
            provider = usage.get("provider") or provider
            model = usage.get("model") or model
        self.metrics_store.record(
            TurnMetric(
                metric_id=str(uuid.uuid4()),
                session_id=session_id or str(uuid.uuid4()),
                tenant_id=tenant_id,
                user_id=user_id,
                provider=provider,
                model=model,
                status=status,
                prompt_tokens=int(usage["prompt_tokens"]),
                completion_tokens=int(usage["completion_tokens"]),
                total_tokens=int(usage["total_tokens"]),
                estimated_cost_usd=float(usage["estimated_cost_usd"]),
                latency_ms=round(latency_ms, 3),
                tool_calls=int(usage["tool_calls"]),
                tool_errors=int(usage["tool_errors"]),
                error_code=error_code,
                created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            )
        )

    def _resolve_model_id(
        self,
        request: RuntimeAgentRequest,
        *,
        previous_events: list[SessionEvent],
    ) -> str:
        if self.model_registry is not None:
            if request.model_id:
                return self.model_registry.resolve_model_id(request.model_id)
            created = next(
                (
                    event
                    for event in previous_events
                    if event.event_type == "session.created"
                ),
                None,
            )
            if created and created.payload.get("model_id"):
                return self.model_registry.resolve_model_id(
                    str(created.payload["model_id"])
                )
            return self.model_registry.resolve_model_id(None)
        if request.model_id:
            if request.model_id not in self.router.adapters:
                raise ValueError(f"unknown model id: {request.model_id}")
            return request.model_id
        return self.router.default_model_id

    def _run_turn(
        self,
        request: RuntimeAgentRequest,
        *,
        tenant_id: str,
        user_id: str,
        role: str = "admin",
        allowed_tools: set[str] | None = None,
        agent_id: str = COORDINATOR_AGENT_ID,
        delegation_depth: int = 0,
        parent_session_id: str | None = None,
        connection_ids: list[str] | None = None,
        resource_scope: dict[str, list[str]] | None = None,
        memory_snapshot: list[dict[str, Any]] | None = None,
        cancellation_event: threading.Event | None = None,
        interruption_is_resumable: bool = False,
        timeout_seconds: float | None = None,
        token_budget: int = 30_000,
        resume: bool = False,
    ) -> RuntimeAgentResponse:
        if resume and not request.session_id:
            raise ValueError("session_id is required to resume")
        session_id = request.session_id or str(uuid.uuid4())
        control = cancellation_event or threading.Event()
        self.live_hub.begin(session_id, control)
        try:
            return self._execute_turn(
                request,
                session_id=session_id,
                tenant_id=tenant_id,
                user_id=user_id,
                role=role,
                allowed_tools=allowed_tools,
                agent_id=agent_id,
                delegation_depth=delegation_depth,
                parent_session_id=parent_session_id,
                connection_ids=connection_ids,
                resource_scope=resource_scope,
                memory_snapshot=memory_snapshot,
                cancellation_event=control,
                interruption_is_resumable=interruption_is_resumable,
                timeout_seconds=timeout_seconds,
                token_budget=token_budget,
                resume=resume,
            )
        finally:
            self.live_hub.end(session_id)

    def _response_from_events(
        self, *, session_id: str, tenant_id: str, events: list[SessionEvent]
    ) -> RuntimeAgentResponse:
        completed = next(
            (
                event
                for event in reversed(events)
                if event.event_type == "turn.completed"
            ),
            None,
        )
        model = next(
            (
                event
                for event in reversed(events)
                if event.event_type == "model.response"
            ),
            None,
        )
        payload = completed.payload if completed else {}
        model_payload = model.payload if model else {}
        return RuntimeAgentResponse(
            session_id=session_id,
            answer=str(payload.get("answer") or "任务已完成。"),
            provider=str(model_payload.get("provider") or ""),
            model=str(model_payload.get("model") or ""),
            event_count=len(events),
            status=str(payload.get("status") or "completed"),
            pending_approval_ids=list(payload.get("pending_approval_ids") or []),
        )

    def _fill_missing_tool_results(
        self,
        messages: list[dict[str, Any]],
        *,
        state: RuntimeState,
    ) -> list[dict[str, Any]]:
        assistant = next(
            (
                message
                for message in reversed(messages)
                if message.get("role") == "assistant" and message.get("tool_calls")
            ),
            None,
        )
        if assistant is None:
            return messages
        have = {
            message.get("tool_call_id")
            for message in messages
            if message.get("role") == "tool"
        }
        context = self._context(state)
        extra: list[dict[str, Any]] = []
        for raw_call in assistant.get("tool_calls") or []:
            call_id = str(raw_call.get("id") or "")
            if not call_id or call_id in have:
                continue
            function = raw_call.get("function") or {}
            name = str(function.get("name") or raw_call.get("name") or "")
            arguments = function.get("arguments") or raw_call.get("arguments") or {}
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = {}
            if not isinstance(arguments, dict):
                arguments = {}
            result = self.executor.execute(
                ToolCall(call_id=call_id, name=name, arguments=arguments),
                context,
            )
            self._append_event(state, "tool.completed", result.model_dump(mode="json"))
            content = result.output if result.ok else {"error": result.error, "ok": False}
            extra.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": name,
                    "content": json.dumps(content, ensure_ascii=False, default=str),
                }
            )
        return [*messages, *extra]

    def _execute_turn(
        self,
        request: RuntimeAgentRequest,
        *,
        session_id: str,
        tenant_id: str,
        user_id: str,
        role: str,
        allowed_tools: set[str] | None,
        agent_id: str,
        delegation_depth: int,
        parent_session_id: str | None,
        connection_ids: list[str] | None,
        resource_scope: dict[str, list[str]] | None,
        memory_snapshot: list[dict[str, Any]] | None,
        cancellation_event: Any,
        interruption_is_resumable: bool,
        timeout_seconds: float | None,
        token_budget: int,
        resume: bool,
    ) -> RuntimeAgentResponse:
        self._emit_stream({"type": "session", "session_id": session_id})
        if len(request.attachment_ids) > self.max_attachments_per_message:
            raise ValueError(
                "too many attachments: "
                f"maximum is {self.max_attachments_per_message}"
            )
        previous_events = self.event_store.list_events(
            session_id=session_id, tenant_id=tenant_id
        )
        if resume:
            if not previous_events:
                raise KeyError("session not found")
            if not turn_is_open(previous_events):
                return self._response_from_events(
                    session_id=session_id, tenant_id=tenant_id, events=previous_events
                )
        created = next(
            (
                event
                for event in previous_events
                if event.event_type == "session.created"
            ),
            None,
        )
        if created and "allowed_tools" in created.payload:
            snapshot = created.payload.get("allowed_tools")
            if snapshot is not None:
                snapshotted_tools = {str(item) for item in snapshot}
                allowed_tools = self._merge_session_tool_snapshot(
                    allowed_tools, snapshotted_tools
                )
        connection_ids, resource_scope = self._connection_scope_for_session(
            tenant_id=tenant_id,
            allowed_tools=allowed_tools,
            parent_session_id=parent_session_id,
            created=created,
            requested_ids=connection_ids,
            requested_scope=resource_scope,
        )
        if (
            parent_session_id
            and created
            and isinstance(created.payload.get("memory_snapshot"), list)
        ):
            memory_snapshot = list(created.payload.get("memory_snapshot") or [])
        elif memory_snapshot is None and self.memory_service is not None:
            memory_snapshot = self.memory_service.build_snapshot(
                request.question,
                tenant_id=tenant_id,
                user_id=user_id,
                agent_id=agent_id,
            )
        memory_snapshot = list(memory_snapshot or [])
        connection_ids = list(connection_ids or [])
        resource_scope = {
            str(name): [str(value) for value in values]
            for name, values in (resource_scope or {}).items()
        }
        model_id = self._resolve_model_id(
            request, previous_events=previous_events
        )
        user_message, attachment_references = self._user_message(
            request.question,
            request.attachment_ids,
            tenant_id=tenant_id,
        )
        if not previous_events:
            self.event_store.append(
                session_id=session_id,
                tenant_id=tenant_id,
                user_id=user_id,
                event_type="session.created",
                payload={
                    "model_id": model_id,
                    "role": role,
                    "parent_session_id": parent_session_id,
                    "agent_id": agent_id,
                    "delegation_depth": delegation_depth,
                    "allowed_tools": (
                        sorted(allowed_tools) if allowed_tools is not None else None
                    ),
                    "connection_ids": connection_ids,
                    "resource_scope": resource_scope,
                    "token_budget": token_budget,
                    "memory_snapshot": memory_snapshot if parent_session_id else [],
                },
            )
        if not resume:
            self.event_store.append(
                session_id=session_id,
                tenant_id=tenant_id,
                user_id=user_id,
                event_type="user.message",
                payload={
                    "content": request.question,
                    "attachment_ids": request.attachment_ids,
                    "attachments": [
                        item.model_dump(mode="json")
                        for item in attachment_references
                    ],
                },
            )
            self._emit_stream(
                {
                    "type": "user.message",
                    "session_id": session_id,
                    "payload": {
                        "content": request.question,
                        "attachments": [
                            item.model_dump(mode="json")
                            for item in attachment_references
                        ],
                    },
                }
            )
        allowed_tool_set = (
            set(allowed_tools) if allowed_tools is not None else None
        )
        system_prompt = self._base_system_prompt(
            allowed_tool_set,
            agent_id=agent_id,
            tenant_id=tenant_id,
            user_id=user_id,
            role=role,
        )
        system_prompt += memory_prompt(memory_snapshot)
        if role != "admin" and self.access_control is not None:
            access = self.access_control.effective_access(tenant_id, user_id, role)
            if access.configured:
                if allowed_tool_set:
                    system_prompt += (
                        "\n权限边界：当前用户本轮仅可调用以下工具："
                        + "、".join(sorted(allowed_tool_set))
                        + "。如果完成用户目标需要列表之外的工具，不得用文本模拟工具调用，"
                        "必须明确告知用户缺少对应工具权限，并提示联系管理员配置权限组和规则。"
                    )
                else:
                    detail = access.denial_detail()
                    system_prompt += (
                        "\n权限边界：当前用户没有可用工具权限。若问题需要调用工具，"
                        f"请直接告知用户：{detail['message']} {detail.get('hint', '')}"
                    )
        if resume and any(
            event.event_type == "turn.interrupted" for event in previous_events
        ):
            system_prompt += (
                "\n上一次执行由用户主动中断。请从已有事件和工具结果恢复未完成目标；"
                "对取消、缺失或失败的子任务重新委派，不要把中断状态当作最终答案。"
            )
        if self.skill_registry is not None:
            system_prompt += self.skill_registry.catalog_prompt(
                include_names=skill_names_for_tools(allowed_tool_set),
            )
        if parent_session_id:
            system_prompt += (
                "\n你是受委派的子 Agent。只完成当前目标；工具权限已经锁定，"
                "不得尝试扩大权限或绕过人工审批。"
            )
        restored_messages = self._restore_messages(
            previous_events, tenant_id=tenant_id
        )
        has_previous_images = any(
            isinstance(message.get("content"), list)
            and any(
                isinstance(item, dict) and item.get("type") == "image_url"
                for item in message["content"]
            )
            for message in restored_messages
        )
        messages = [
            {"role": "system", "content": system_prompt},
            *restored_messages,
            *([] if resume else [user_message]),
        ]
        if resume and not any(message.get("role") == "user" for message in messages):
            raise ValueError("cannot resume a session without a user message")
        last = next((message for message in reversed(messages) if message.get("role") != "system"), None)
        if (
            resume
            and last
            and last.get("role") == "assistant"
            and str(last.get("content") or "").strip()
            and not last.get("tool_calls")
        ):
            answer = str(last.get("content") or "").strip()
            self.event_store.append(
                session_id=session_id,
                tenant_id=tenant_id,
                user_id=user_id,
                event_type="turn.completed",
                payload={"answer": answer, "status": "completed"},
            )
            events = self.event_store.list_events(
                session_id=session_id, tenant_id=tenant_id
            )
            return self._response_from_events(
                session_id=session_id, tenant_id=tenant_id, events=events
            )
        state: RuntimeState = {
            "messages": messages,
            "session_id": session_id,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "role": role,
            "model_id": model_id,
            "required_modalities": (
                {"text", "image"}
                if attachment_references or has_previous_images
                else {"text"}
            ),
            "pending_calls": [],
            "tool_results": [],
            "tool_steps": 0,
            "answer": "",
            "provider": "",
            "model": "",
            "approved_call_ids": [],
            "allowed_tools": sorted(allowed_tools) if allowed_tools is not None else None,
            "agent_id": agent_id,
            "delegation_depth": delegation_depth,
            "connection_ids": connection_ids,
            "resource_scope": resource_scope,
            "connection_scope_enforced": self.connection_registry is not None,
            "waiting_approval": False,
            "pending_approval_ids": [],
            "cancellation_event": cancellation_event,
            "interruption_is_resumable": interruption_is_resumable,
            "deadline": (
                time.monotonic() + timeout_seconds
                if timeout_seconds is not None
                else None
            ),
            "token_budget": token_budget,
            "tokens_used": 0,
            "status": "completed",
            "explicit_memory_consent": explicit_remember_requested(request.question),
            "explicit_memory_forget": explicit_forget_requested(request.question),
            "memory_snapshot": memory_snapshot,
        }
        if resume:
            state["messages"] = self._fill_missing_tool_results(
                list(state["messages"]), state=state
            )
        try:
            result = self.graph.invoke(
                state,
                config={"recursion_limit": self.max_tool_steps * 2 + 4},
            )
        except ModelProviderError:
            raise
        except TimeoutError as exc:
            self.event_store.append(
                session_id=session_id,
                tenant_id=tenant_id,
                user_id=user_id,
                event_type="turn.completed",
                payload={"status": "timed_out", "error": str(exc)},
            )
            events = self.event_store.list_events(
                session_id=session_id, tenant_id=tenant_id
            )
            return RuntimeAgentResponse(
                session_id=session_id,
                answer="任务已超时终止。",
                provider=state["provider"],
                model=state["model"],
                event_count=len(events),
                status="timed_out",
            )
        except RuntimeError as exc:
            if "cancelled" not in str(exc).lower():
                raise
            if state["interruption_is_resumable"]:
                self.event_store.append(
                    session_id=session_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    event_type="turn.interrupted",
                    payload={"status": "interrupted", "reason": "user_requested"},
                )
                events = self.event_store.list_events(
                    session_id=session_id, tenant_id=tenant_id
                )
                return RuntimeAgentResponse(
                    session_id=session_id,
                    answer="任务已中断，可继续执行。",
                    provider=state["provider"],
                    model=state["model"],
                    event_count=len(events),
                    status="interrupted",
                )
            self.event_store.append(
                session_id=session_id,
                tenant_id=tenant_id,
                user_id=user_id,
                event_type="turn.completed",
                payload={"status": "cancelled", "error": str(exc)},
            )
            events = self.event_store.list_events(
                session_id=session_id, tenant_id=tenant_id
            )
            return RuntimeAgentResponse(
                session_id=session_id,
                answer="任务已取消。",
                provider=state["provider"],
                model=state["model"],
                event_count=len(events),
                status="cancelled",
            )
        answer = self._visible_answer(result, "任务已完成。")
        if (
            self.memory_service is not None
            and self.settings is not None
            and self.settings.memory_auto_extract_enabled
            and agent_id == COORDINATOR_AGENT_ID
            and not resume
            and result["status"] == "completed"
        ):
            candidates = self.memory_service.extract_candidates(
                request.question,
                tenant_id=tenant_id,
                user_id=user_id,
                source_session_id=session_id,
            )
            if candidates:
                self._append_event(
                    state,
                    "memory.candidates_extracted",
                    {"memory_ids": [item.id for item in candidates], "count": len(candidates)},
                )
        self.event_store.append(
            session_id=session_id,
            tenant_id=tenant_id,
            user_id=user_id,
            event_type="turn.completed",
            payload={
                "answer": answer,
                "tool_result_count": len(result["tool_results"]),
                "status": result["status"],
                "tokens_used": result["tokens_used"],
            },
        )
        events = self.event_store.list_events(
            session_id=session_id, tenant_id=tenant_id
        )
        return RuntimeAgentResponse(
            session_id=session_id,
            answer=answer,
            provider=result["provider"],
            model=result["model"],
            tool_results=[
                ToolResult.model_validate(item) for item in result["tool_results"]
            ],
            event_count=len(events),
            status=result["status"],
            pending_approval_ids=result["pending_approval_ids"],
        )

    def decide_approval(
        self,
        *,
        approval_id: str,
        tenant_id: str,
        decided_by: str,
        approved: bool,
        comment: str = "",
    ) -> RuntimeAgentResponse:
        if self.governance_store is None:
            raise RuntimeError("runtime approval store is not configured")
        record = self.governance_store.decide_approval(
            approval_id=approval_id,
            tenant_id=tenant_id,
            approved=approved,
            decided_by=decided_by,
            comment=comment,
        )
        self.event_store.append(
            session_id=record.session_id,
            tenant_id=record.tenant_id,
            user_id=decided_by,
            event_type="approval.decided",
            payload={
                "approval_id": record.approval_id,
                "call_id": record.call.call_id,
                "approved": approved,
                "comment": comment,
            },
        )
        if approved:
            session_events = self.event_store.list_events(
                session_id=record.session_id, tenant_id=record.tenant_id
            )
            created = next(
                (
                    event
                    for event in session_events
                    if event.event_type == "session.created"
                ),
                None,
            )
            agent_id = str(
                (created.payload.get("agent_id") if created else None)
                or COORDINATOR_AGENT_ID
            )
            allowed_tool_set = self._agent_allowed_tools(agent_id, record.tenant_id)
            connection_ids, resource_scope_lists = self._connection_scope_for_session(
                tenant_id=record.tenant_id,
                allowed_tools=allowed_tool_set,
                parent_session_id=None,
                created=created,
            )
            connection_ids = tuple(connection_ids)
            resource_scope = {
                name: tuple(values) for name, values in resource_scope_lists.items()
            }
            context = ToolExecutionContext(
                session_id=record.session_id,
                tenant_id=record.tenant_id,
                user_id=record.user_id,
                role=record.role,
                approved_call_ids=frozenset({record.call.call_id}),
                connection_ids=connection_ids,
                resource_scope=resource_scope,
                connection_scope_enforced=self.connection_registry is not None,
            )
            tool_result = self.executor.execute(record.call, context)
        else:
            tool_result = ToolResult(
                call_id=record.call.call_id,
                tool_name=record.call.name,
                ok=False,
                error=f"tool call rejected by {decided_by}: {comment or 'no reason'}",
            )
        self.event_store.append(
            session_id=record.session_id,
            tenant_id=record.tenant_id,
            user_id=decided_by,
            event_type="tool.completed",
            payload=tool_result.model_dump(mode="json"),
        )
        remaining = [
            item
            for item in self.governance_store.list_pending_approvals(tenant_id)
            if item.session_id == record.session_id
        ]
        if remaining:
            events = self.event_store.list_events(
                session_id=record.session_id, tenant_id=record.tenant_id
            )
            return RuntimeAgentResponse(
                session_id=record.session_id,
                answer="仍有高风险工具调用等待逐次审批。",
                provider="",
                model="",
                tool_results=[tool_result],
                event_count=len(events),
                status="waiting_approval",
                pending_approval_ids=[item.approval_id for item in remaining],
            )
        events = self.event_store.list_events(
            session_id=record.session_id, tenant_id=record.tenant_id
        )
        restored_messages = self._restore_messages(
            events, tenant_id=record.tenant_id
        )
        has_images = any(
            isinstance(message.get("content"), list)
            and any(
                isinstance(item, dict) and item.get("type") == "image_url"
                for item in message["content"]
            )
            for message in restored_messages
        )
        created = next(
            (event for event in events if event.event_type == "session.created"),
            None,
        )
        agent_id = str(
            (created.payload.get("agent_id") if created else None)
            or COORDINATOR_AGENT_ID
        )
        allowed_tool_set = self._agent_allowed_tools(agent_id, record.tenant_id)
        state_connection_ids, state_resource_scope = self._connection_scope_for_session(
            tenant_id=record.tenant_id,
            allowed_tools=allowed_tool_set,
            parent_session_id=None,
            created=created,
        )
        system_prompt = self._base_system_prompt(
            allowed_tool_set,
            agent_id=agent_id,
            tenant_id=record.tenant_id,
            user_id=record.user_id,
            role=record.role,
        )
        if self.skill_registry is not None:
            system_prompt += self.skill_registry.catalog_prompt(
                include_names=skill_names_for_tools(allowed_tool_set),
            )
        model_id = self._resolve_model_id(
            RuntimeAgentRequest(question="continue", session_id=record.session_id),
            previous_events=events,
        )
        state: RuntimeState = {
            "messages": [
                {"role": "system", "content": system_prompt},
                *restored_messages,
            ],
            "session_id": record.session_id,
            "tenant_id": record.tenant_id,
            "user_id": record.user_id,
            "role": record.role,
            "model_id": model_id,
            "required_modalities": {"text", "image"} if has_images else {"text"},
            "pending_calls": [],
            "tool_results": [tool_result.model_dump(mode="json")],
            "tool_steps": 1,
            "answer": "",
            "provider": "",
            "model": "",
            "approved_call_ids": [record.call.call_id] if approved else [],
            "allowed_tools": (
                sorted(allowed_tool_set) if allowed_tool_set is not None else None
            ),
            "agent_id": agent_id,
            "delegation_depth": int(
                (created.payload.get("delegation_depth") if created else 0) or 0
            ),
            "connection_ids": state_connection_ids,
            "resource_scope": state_resource_scope,
            "connection_scope_enforced": self.connection_registry is not None,
            "waiting_approval": False,
            "pending_approval_ids": [],
            "cancellation_event": None,
            "deadline": None,
            "token_budget": 30_000,
            "tokens_used": 0,
            "status": "completed",
        }
        result = self.graph.invoke(
            state,
            config={"recursion_limit": self.max_tool_steps * 2 + 4},
        )
        answer = self._visible_answer(result, "审批结果已处理。")
        self.event_store.append(
            session_id=record.session_id,
            tenant_id=record.tenant_id,
            user_id=record.user_id,
            event_type="turn.completed",
            payload={
                "answer": answer,
                "tool_result_count": len(result["tool_results"]),
                "status": result["status"],
                "tokens_used": result["tokens_used"],
            },
        )
        final_events = self.event_store.list_events(
            session_id=record.session_id, tenant_id=record.tenant_id
        )
        return RuntimeAgentResponse(
            session_id=record.session_id,
            answer=answer,
            provider=result["provider"],
            model=result["model"],
            tool_results=[
                ToolResult.model_validate(item) for item in result["tool_results"]
            ],
            event_count=len(final_events),
            status=result["status"],
            pending_approval_ids=result["pending_approval_ids"],
        )
