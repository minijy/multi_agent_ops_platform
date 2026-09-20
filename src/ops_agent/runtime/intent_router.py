from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from ..agent_roles import (
    AMAZON_FINANCE_ANALYST_ID,
    ERP_ANALYST_ID,
    PROFIT_ANALYST_ID,
)
from ..config import Settings
from .domain import ToolCall


Decision = Literal["direct_answer", "clarify", "tool_plan", "abstain"]
Layer = Literal["disabled", "hard_match", "small_model", "coordinator"]

_INTENT_SCHEMA_KEYS = frozenset(
    {
        "$defs",
        "$ref",
        "additionalProperties",
        "allOf",
        "anyOf",
        "const",
        "default",
        "description",
        "enum",
        "format",
        "items",
        "maximum",
        "maxItems",
        "maxLength",
        "minimum",
        "minItems",
        "minLength",
        "nullable",
        "oneOf",
        "pattern",
        "properties",
        "required",
        "type",
    }
)


INTENT_SYSTEM_PROMPT = """
你是多 Agent 系统的前置意图路由器。你不回答问题，只输出一个紧凑 JSON 对象。
输入包含 query、history 和本次动态提供的 visible_tools。
decision 只能是 direct_answer、clarify、tool_plan、abstain。
- direct_answer：普通问答或无需工具的任务，交给 Coordinator。
- clarify：用户明确要调用可见工具，但缺少该工具 Schema 的必填参数。
- tool_plan：可见工具能够完成请求，且必填参数完整。
- abstain：能力不明确、需要复杂规划或没有可靠匹配，交给 Coordinator。
工具名称必须来自 visible_tools；arguments 和 missing_slots 只能使用对应 Schema 的真实字段。
clarify 必须提供 candidate_tool，actions 必须为空；其他决策 candidate_tool 必须为 null。
用户未提供且 Schema 未声明 default 的可选参数禁止输出。只输出一次完整 JSON，不要 Markdown。
输出结构：{"decision":"tool_plan","candidate_tool":null,"actions":[{"tool":"工具名","arguments":{}}],"missing_slots":[]}
/no_think
""".strip()


@dataclass(frozen=True)
class IntentRoute:
    layer: Layer
    decision: Decision | None = None
    calls: tuple[ToolCall, ...] = ()
    candidate_tool: str | None = None
    missing_slots: tuple[str, ...] = ()
    reason: str = ""
    latency_ms: float = 0.0
    provider: str = ""
    model: str = ""

    @property
    def uses_coordinator(self) -> bool:
        return self.layer in {"disabled", "coordinator"} or self.decision in {
            "direct_answer",
            "abstain",
            None,
        }


def _tool_names(schemas: list[dict[str, Any]]) -> set[str]:
    return {
        str(item.get("function", {}).get("name") or "")
        for item in schemas
        if item.get("function", {}).get("name")
    }


def _compact_schema_node(value: Any) -> Any:
    """Keep tool-selection semantics while removing prompt-only JSON Schema noise."""
    if isinstance(value, list):
        return [_compact_schema_node(item) for item in value]
    if not isinstance(value, dict):
        return value
    compact: dict[str, Any] = {}
    for key, item in value.items():
        if key not in _INTENT_SCHEMA_KEYS:
            continue
        if key in {"properties", "$defs"} and isinstance(item, dict):
            compact[key] = {
                str(name): _compact_schema_node(schema)
                for name, schema in item.items()
            }
            continue
        if key == "description" and isinstance(item, str):
            normalized = " ".join(item.split())
            if normalized:
                compact[key] = normalized[:160]
            continue
        compact[key] = _compact_schema_node(item)
    return compact


def compact_tool_schemas(schemas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compacted: list[dict[str, Any]] = []
    for schema in schemas:
        function = schema.get("function") or {}
        name = function.get("name")
        if not name:
            continue
        description = " ".join(str(function.get("description") or "").split())[:160]
        compact_function: dict[str, Any] = {
            "name": str(name),
            "parameters": _compact_schema_node(function.get("parameters") or {}),
        }
        if description:
            compact_function["description"] = description
        compacted.append({"type": "function", "function": compact_function})
    return compacted


def bounded_history(
    history: list[dict[str, str]],
    *,
    max_messages: int,
    max_chars: int,
) -> list[dict[str, str]]:
    if max_messages <= 0 or max_chars <= 0:
        return []
    selected: list[dict[str, str]] = []
    remaining = max_chars
    for message in reversed(history[-max_messages:]):
        if remaining <= 0:
            break
        content = str(message.get("content") or "")
        if not content:
            continue
        clipped = content[-remaining:]
        selected.append(
            {
                "role": str(message.get("role") or "user"),
                "content": clipped,
            }
        )
        remaining -= len(clipped)
    selected.reverse()
    return selected


def _first_json_object(text: str) -> dict[str, Any]:
    start = text.find("{")
    if start < 0:
        raise ValueError("intent model returned no JSON object")
    depth = 0
    in_string = False
    escaped = False
    for index, char in enumerate(text[start:], start=start):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                value = json.loads(text[start : index + 1])
                if not isinstance(value, dict):
                    raise ValueError("intent model output must be an object")
                return value
    raise ValueError("intent model returned truncated JSON")


def _required_fields(schema: dict[str, Any]) -> set[str]:
    function = schema.get("function") or {}
    parameters = function.get("parameters") or {}
    return {str(item) for item in parameters.get("required") or []}


def _properties(schema: dict[str, Any]) -> dict[str, Any]:
    function = schema.get("function") or {}
    parameters = function.get("parameters") or {}
    properties = parameters.get("properties") or {}
    return properties if isinstance(properties, dict) else {}


def optional_arguments_are_grounded(
    call: ToolCall,
    schemas: list[dict[str, Any]],
    source_text: str,
) -> bool:
    """Reject guessed optional values before a routed Tool can execute.

    Orchestration Tools are exempt because agent selection and objectives are
    routing metadata rather than user-supplied business parameters.
    """
    if call.name in {"delegate_subagent", "delegate_specialists"}:
        return True
    schema = next(
        (item for item in schemas if str(item.get("function", {}).get("name") or "") == call.name),
        None,
    )
    if schema is None:
        return False
    required = _required_fields(schema)
    properties = _properties(schema)
    normalized_source = source_text.casefold()
    for name, value in call.arguments.items():
        if name in required:
            continue
        field_schema = properties.get(name)
        if isinstance(field_schema, dict) and "default" in field_schema:
            continue
        candidates: list[str]
        if isinstance(value, list):
            candidates = [str(item) for item in value]
        elif isinstance(value, dict):
            candidates = [json.dumps(value, ensure_ascii=False, sort_keys=True)]
        else:
            candidates = [str(value)]
        if not candidates or any(item.casefold() not in normalized_source for item in candidates):
            return False
    return True


def _validate_model_route(payload: dict[str, Any], schemas: list[dict[str, Any]]) -> IntentRoute:
    allowed_keys = {"decision", "candidate_tool", "actions", "missing_slots"}
    if set(payload) != allowed_keys:
        raise ValueError("intent output has unexpected top-level fields")
    decision = str(payload.get("decision") or "")
    if decision not in {"direct_answer", "clarify", "tool_plan", "abstain"}:
        raise ValueError("invalid intent decision")
    by_name = {
        str(item.get("function", {}).get("name") or ""): item
        for item in schemas
        if item.get("function", {}).get("name")
    }
    actions = payload.get("actions")
    slots = payload.get("missing_slots")
    if not isinstance(actions, list) or not isinstance(slots, list):
        raise ValueError("actions and missing_slots must be arrays")
    candidate = payload.get("candidate_tool")
    calls: list[ToolCall] = []
    if decision == "clarify":
        if not isinstance(candidate, str) or candidate not in by_name or actions:
            raise ValueError("clarify requires one visible candidate_tool and no actions")
        allowed_slots = _required_fields(by_name[candidate])
        normalized_slots = tuple(str(item) for item in slots)
        if not normalized_slots or not set(normalized_slots).issubset(allowed_slots):
            raise ValueError("missing_slots must be real required fields")
        return IntentRoute(
            layer="small_model",
            decision="clarify",
            candidate_tool=candidate,
            missing_slots=normalized_slots,
        )
    if candidate is not None or slots:
        raise ValueError("non-clarify output cannot contain candidate_tool or missing_slots")
    if decision != "tool_plan":
        if actions:
            raise ValueError("non-tool decision cannot contain actions")
        return IntentRoute(layer="small_model", decision=decision)  # type: ignore[arg-type]
    if not actions:
        raise ValueError("tool_plan requires at least one action")
    for action in actions:
        if not isinstance(action, dict) or set(action) != {"tool", "arguments"}:
            raise ValueError("invalid action object")
        name = str(action.get("tool") or "")
        arguments = action.get("arguments")
        if name not in by_name or not isinstance(arguments, dict):
            raise ValueError("action references an invisible tool")
        properties = _properties(by_name[name])
        if not set(arguments).issubset(properties):
            raise ValueError("action contains fields outside the tool schema")
        if not _required_fields(by_name[name]).issubset(arguments):
            raise ValueError("action is missing required fields")
        calls.append(
            ToolCall(
                call_id=f"intent-{uuid.uuid4().hex[:12]}",
                name=name,
                arguments=arguments,
            )
        )
    return IntentRoute(layer="small_model", decision="tool_plan", calls=tuple(calls))


class HardIntentMatcher:
    """High-precision business routing only; ambiguous input intentionally falls through."""

    _OPERATIONS = (
        "查询",
        "统计",
        "汇总",
        "top",
        "明细",
        "多少",
    )
    _DOMAINS: tuple[tuple[str, tuple[str, ...]], ...] = (
        (
            AMAZON_FINANCE_ANALYST_ID,
            ("amazon", "亚马逊", "结算", "asin", "退款", "平台费"),
        ),
        (
            PROFIT_ANALYST_ID,
            ("利润", "毛利", "领星", "msku", "订单收入", "订单成本"),
        ),
        (ERP_ANALYST_ID, ("金蝶", "erp", "出库", "应收", "回款")),
    )

    def match(self, query: str, schemas: list[dict[str, Any]]) -> IntentRoute | None:
        text = query.strip().lower()
        if not text or not any(marker in text for marker in self._OPERATIONS):
            return None
        available = _tool_names(schemas)
        matched = [
            agent_id
            for agent_id, markers in self._DOMAINS
            if any(marker in text for marker in markers)
        ]
        if not matched:
            return None
        if len(matched) > 1 and "delegate_specialists" in available:
            return IntentRoute(
                layer="hard_match",
                decision="tool_plan",
                calls=(
                    ToolCall(
                        call_id=f"hard-{uuid.uuid4().hex[:12]}",
                        name="delegate_specialists",
                        arguments={
                            "tasks": [
                                {"agent_id": agent_id, "objective": query}
                                for agent_id in matched[:3]
                            ]
                        },
                    ),
                ),
                reason="matched multiple explicit business domains",
            )
        if len(matched) == 1 and "delegate_subagent" in available:
            return IntentRoute(
                layer="hard_match",
                decision="tool_plan",
                calls=(
                    ToolCall(
                        call_id=f"hard-{uuid.uuid4().hex[:12]}",
                        name="delegate_subagent",
                        arguments={
                            "agent_id": matched[0],
                            "objective": query,
                            "run_in_background": False,
                        },
                    ),
                ),
                reason=f"matched explicit business domain: {matched[0]}",
            )
        return None


class SmallModelIntentClient:
    def __init__(self, settings: Settings) -> None:
        self.base_url = settings.intent_routing_base_url.rstrip("/")
        self.api_key = settings.intent_routing_api_key
        self.model = settings.intent_routing_model
        self.timeout = settings.intent_routing_timeout_seconds
        self.max_history_messages = settings.intent_routing_history_messages
        self.max_history_chars = settings.intent_routing_history_max_chars
        self.compact_schemas = settings.intent_routing_compact_schemas
        self._client = httpx.Client(
            timeout=httpx.Timeout(self.timeout),
            limits=httpx.Limits(
                max_connections=32,
                max_keepalive_connections=16,
                keepalive_expiry=30,
            ),
            trust_env=False,
        )

    def close(self) -> None:
        self._client.close()

    def classify(
        self,
        query: str,
        history: list[dict[str, str]],
        schemas: list[dict[str, Any]],
        *,
        tenant_id: str = "",
        user_id: str = "",
    ) -> IntentRoute:
        if not self.base_url:
            raise RuntimeError("intent router base URL is not configured")
        routed_history = bounded_history(
            history,
            max_messages=self.max_history_messages,
            max_chars=self.max_history_chars,
        )
        routed_schemas = compact_tool_schemas(schemas) if self.compact_schemas else schemas
        request_body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": INTENT_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            # Put the stable, usually shared prefix first so vLLM's
                            # automatic prefix cache can reuse it across user queries.
                            "visible_tools": routed_schemas,
                            "history": routed_history,
                            "query": query,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
            ],
            "temperature": 0,
            # Repeated JSON punctuation and field names are legitimate. A penalty
            # above 1.0 can corrupt the fixed response contract.
            "repetition_penalty": 1.0,
            "max_tokens": 128,
            "chat_template_kwargs": {"enable_thinking": False},
            "stream": False,
        }
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        if tenant_id:
            headers["x-tenant-id"] = tenant_id
        if user_id:
            headers["x-user-id"] = user_id
        response = self._client.post(
            f"{self.base_url}/chat/completions",
            headers=headers,
            json=request_body,
            timeout=self.timeout,
        )
        response.raise_for_status()
        data = response.json()
        content = str(data["choices"][0]["message"]["content"] or "")
        route = _validate_model_route(_first_json_object(content), schemas)
        return IntentRoute(
            **{
                **route.__dict__,
                "provider": "openai-compatible",
                "model": self.model,
            }
        )


class ThreeLayerIntentRouter:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.hard_matcher = HardIntentMatcher()
        self.small_model = SmallModelIntentClient(settings)

    def route(
        self,
        *,
        query: str,
        history: list[dict[str, str]],
        schemas: list[dict[str, Any]],
        tenant_id: str = "",
        user_id: str = "",
    ) -> IntentRoute:
        if not self.settings.intent_routing_enabled:
            return IntentRoute(layer="disabled", reason="intent routing is disabled")
        started = time.perf_counter()
        hard = self.hard_matcher.match(query, schemas)
        if hard is not None:
            return IntentRoute(
                **{**hard.__dict__, "latency_ms": (time.perf_counter() - started) * 1000}
            )
        try:
            route = self.small_model.classify(
                query,
                history,
                schemas,
                tenant_id=tenant_id,
                user_id=user_id,
            )
            return IntentRoute(
                **{**route.__dict__, "latency_ms": (time.perf_counter() - started) * 1000}
            )
        except Exception as exc:
            return IntentRoute(
                layer="coordinator",
                reason=f"small model fallback: {type(exc).__name__}: {exc}"[:500],
                latency_ms=(time.perf_counter() - started) * 1000,
            )


def create_intent_router(settings: Settings) -> ThreeLayerIntentRouter:
    return ThreeLayerIntentRouter(settings)
