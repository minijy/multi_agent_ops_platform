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
from .model_errors import ModelProviderError
from .governance import RuntimeGovernanceStore
from .observability import MetricsStore, TurnMetric, usage_from_events
from .session_events import SessionEvent, SessionEventStore
from .skills import SkillRegistry
from .tools import ToolExecutionContext, ToolExecutor, ToolRegistry
from .agent_tool_policy import (
    active_data_query_tools,
    data_tool_usage_prompt,
    runtime_tool_allowlist,
    skill_names_for_tools,
)
from .tracing import span

if TYPE_CHECKING:
    from ..agent_registry import AgentRegistry
    from ..model_registry import ModelRegistry


SYSTEM_PROMPT = """
你是企业数据与运维助手。用简洁中文直接回答用户，不要输出 JSON、tool_calls 或内部协议。

多轮对话：
- 必须阅读完整会话。用户说「上面」「底下」「改成」「继续」「按上次」时，是在改已有答案，不是新任务。
- 若会话里已有数据查询工具结果且能回答当前问题，直接基于上文改写；不要重复查询。
- 严格遵守用户最新的展示要求（列名、分组、排序、语言），不要照搬工具英文字段名。

工具使用规则：
- 普通知识、概念解释、某模型是否免费：直接回答，不要调用工具。
- 数据查询：仅调用系统末尾「当前可用的数据查询工具」中列出的工具；未列出的工具不可调用。
- 只有用户明确要求在本机执行命令时，才使用 sandbox_read_only 或 sandbox_workspace_write。
- 不要为了查网页、查价格或“确认一下”去调用沙箱或 curl。
- 用户要下载表格、费用或导出数据时，必须生成 UTF-8 的 .csv，不要用 .txt。
- CSV 第一行是中文表头，字段用逗号分隔；单元格含逗号或引号时用双引号包裹。不要给整段正文再套一层引号。
- 生成文件后，用 Markdown 链接给出工作区内的文件名，例如 [下载 report.csv](report.csv)；不要使用 sandbox: 协议或本机绝对路径。
- echo 没有重定向时不会写文件；把 CSV 正文放在倒数第二个参数、文件名放在最后（必须以 .csv 结尾），运行时会落盘。也可以用 python 直接写入工作区文件。
- 工具参数必须遵循 Schema。
""".strip()

_stream_events: ContextVar[Callable[[dict[str, Any]], None] | None] = ContextVar(
    "agent_stream_events", default=None
)


class SessionLiveHub:
    """Let a refreshed browser attach to an in-flight agent turn."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._inflight: set[str] = set()
        self._subscribers: dict[str, list[Queue[dict[str, Any] | None]]] = {}

    def begin(self, session_id: str) -> None:
        with self._lock:
            self._inflight.add(session_id)

    def is_inflight(self, session_id: str) -> bool:
        with self._lock:
            return session_id in self._inflight

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
    seller_id: str | None
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
    delegation_depth: int
    waiting_approval: bool
    pending_approval_ids: list[str]
    cancellation_event: Any
    deadline: float | None
    token_budget: int
    tokens_used: int
    status: str


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
        self.live_hub = SessionLiveHub()
        self.graph = self._build_graph()

    def reload_router(self, router: ModelRouter) -> None:
        self.router = router

    def _active_data_tools_for_prompt(
        self, allowed_tools: set[str] | None
    ) -> frozenset[str]:
        if self.agent_registry is None or self.settings is None:
            return frozenset()
        active = active_data_query_tools(self.agent_registry, self.settings)
        if allowed_tools is None:
            return active
        return frozenset(tool for tool in active if tool in allowed_tools)

    def _base_system_prompt(self, allowed_tools: set[str] | None = None) -> str:
        prompt = SYSTEM_PROMPT
        if self.agent_registry is not None:
            config = self.agent_registry.runtime_config()
            prompt = config.effective_system_prompt(SYSTEM_PROMPT)
        prompt += data_tool_usage_prompt(self._active_data_tools_for_prompt(allowed_tools))
        return prompt

    def _runtime_allowed_tools(self) -> set[str] | None:
        if self.agent_registry is None or self.settings is None:
            return None
        config = self.agent_registry.runtime_config()
        if not config.enabled:
            return set()
        return runtime_tool_allowlist(
            self.agent_registry,
            self.settings,
            self.registry,
            config.allowed_tools,
        )

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
        if answer:
            return answer
        for item in reversed(result.get("tool_results") or []):
            if not isinstance(item, dict) or not item.get("ok"):
                continue
            output = item.get("output")
            if output is None:
                continue
            if isinstance(output, str) and output.strip():
                return output.strip()[:4000]
            text = json.dumps(output, ensure_ascii=False, default=str)
            if text and text not in {"{}", "[]", "null"}:
                return text[:4000]
        return fallback

    @staticmethod
    def _context(state: RuntimeState) -> ToolExecutionContext:
        allowed = state.get("allowed_tools")
        return ToolExecutionContext(
            session_id=state["session_id"],
            tenant_id=state["tenant_id"],
            user_id=state["user_id"],
            role=state["role"],
            seller_id=state["seller_id"],
            approved_call_ids=frozenset(state["approved_call_ids"]),
            allowed_tool_names=frozenset(allowed) if allowed is not None else None,
            delegation_depth=state["delegation_depth"],
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
        return message

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

            def on_token(text: str) -> None:
                if not text:
                    return
                self._emit_stream(
                    {
                        "type": "token",
                        "text": text,
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
        except ModelProviderError as exc:
            self._append_event(
                state,
                "model.error",
                exc.as_dict(),
            )
            raise
        self._check_control(state)
        calls = turn.tool_calls
        answer = turn.content
        tokens_used = state["tokens_used"] + int(
            turn.usage.get("total_tokens", 0) or 0
        )
        budget_exceeded = tokens_used > state["token_budget"]
        if budget_exceeded:
            calls = []
            answer = (
                f"已达到本次执行的 Token 预算 {state['token_budget']}，任务已停止。"
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
                    seller_id=state["seller_id"],
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
                return content if len(content) <= max_chars else content[:max_chars] + "…"
        else:
            payload = content
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
        last_user = 0
        for index, message in enumerate(messages):
            if message.get("role") == "user":
                last_user = index
        max_rows, max_chars = self._tool_compact_limits()
        prepared: list[dict[str, Any]] = []
        for index, message in enumerate(messages):
            item = dict(message)
            if (
                item.get("role") == "assistant"
                and item.get("tool_calls")
                and not str(item.get("content") or "").strip()
            ):
                item["content"] = None
            if item.get("role") == "tool" and index < last_user:
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
        delegation_depth: int = 0,
        parent_session_id: str | None = None,
        cancellation_event: threading.Event | None = None,
        timeout_seconds: float | None = None,
        token_budget: int = 30_000,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        resume: bool = False,
    ) -> RuntimeAgentResponse:
        if self.agent_registry is not None and not self.agent_registry.runtime_config().enabled:
            raise ValueError("Function Calling Agent is disabled")
        configured_tools = self._runtime_allowed_tools()
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
            ):
                try:
                    result = self._run_turn(
                        request,
                        tenant_id=tenant_id,
                        user_id=user_id,
                        role=role,
                        allowed_tools=allowed_tools,
                        delegation_depth=delegation_depth,
                        parent_session_id=parent_session_id,
                        cancellation_event=cancellation_event,
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
        seller_id: str | None = None,
        token_budget: int = 30_000,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> RuntimeAgentResponse:
        return self.run(
            RuntimeAgentRequest(
                question="continue",
                session_id=session_id,
                seller_id=seller_id,
            ),
            tenant_id=tenant_id,
            user_id=user_id,
            role=role,
            token_budget=token_budget,
            on_event=on_event,
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
        delegation_depth: int = 0,
        parent_session_id: str | None = None,
        cancellation_event: threading.Event | None = None,
        timeout_seconds: float | None = None,
        token_budget: int = 30_000,
        resume: bool = False,
    ) -> RuntimeAgentResponse:
        if resume and not request.session_id:
            raise ValueError("session_id is required to resume")
        session_id = request.session_id or str(uuid.uuid4())
        self.live_hub.begin(session_id)
        try:
            return self._execute_turn(
                request,
                session_id=session_id,
                tenant_id=tenant_id,
                user_id=user_id,
                role=role,
                allowed_tools=allowed_tools,
                delegation_depth=delegation_depth,
                parent_session_id=parent_session_id,
                cancellation_event=cancellation_event,
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
        delegation_depth: int,
        parent_session_id: str | None,
        cancellation_event: Any,
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
        seller_id = request.seller_id or (
            str(created.payload.get("seller_id") or "") or None if created else None
        )
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
                    "seller_id": request.seller_id,
                    "model_id": model_id,
                    "role": role,
                    "parent_session_id": parent_session_id,
                    "delegation_depth": delegation_depth,
                    "token_budget": token_budget,
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
        system_prompt = self._base_system_prompt(allowed_tool_set)
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
            "seller_id": seller_id,
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
            "delegation_depth": delegation_depth,
            "waiting_approval": False,
            "pending_approval_ids": [],
            "cancellation_event": cancellation_event,
            "deadline": (
                time.monotonic() + timeout_seconds
                if timeout_seconds is not None
                else None
            ),
            "token_budget": token_budget,
            "tokens_used": 0,
            "status": "completed",
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
            context = ToolExecutionContext(
                session_id=record.session_id,
                tenant_id=record.tenant_id,
                user_id=record.user_id,
                role=record.role,
                seller_id=record.seller_id,
                approved_call_ids=frozenset({record.call.call_id}),
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
        allowed_tool_set = self._runtime_allowed_tools()
        system_prompt = self._base_system_prompt(allowed_tool_set)
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
            "seller_id": record.seller_id,
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
            "delegation_depth": 0,
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
