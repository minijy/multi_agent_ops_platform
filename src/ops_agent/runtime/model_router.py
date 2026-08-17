from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from ..config import Settings
from ..model_gateway import MockModelGateway
from ..workflows.amazon_finance.domain import AmazonFinanceQueryPlan
from .domain import ModelTurn, ToolCall
from .model_errors import invoke_zhipu_with_backoff


class FunctionCallingAdapter(Protocol):
    provider: str
    model_name: str
    input_modalities: frozenset[str]

    def invoke(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ModelTurn: ...


def _looks_like_completion_dump(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    if {"finish_reason", "tool_calls", "index"} & payload.keys():
        return True
    message = payload.get("message")
    return isinstance(message, dict) and (
        "tool_calls" in message or message.get("role") == "assistant"
    )


def _tool_calls_from_payload(payload: Any) -> list[ToolCall]:
    raw_calls: list[Any] = []
    if isinstance(payload, dict):
        if isinstance(payload.get("tool_calls"), list):
            raw_calls = payload["tool_calls"]
        message = payload.get("message")
        if not raw_calls and isinstance(message, dict):
            raw_calls = message.get("tool_calls") or []
    calls: list[ToolCall] = []
    for item in raw_calls:
        if not isinstance(item, dict):
            continue
        function = item.get("function") or {}
        name = function.get("name") or item.get("name")
        if not name:
            continue
        raw_arguments = function.get("arguments") or item.get("arguments") or "{}"
        if isinstance(raw_arguments, str):
            try:
                arguments = json.loads(raw_arguments)
            except json.JSONDecodeError:
                arguments = {"raw": raw_arguments}
        elif isinstance(raw_arguments, dict):
            arguments = raw_arguments
        else:
            arguments = {}
        calls.append(
            ToolCall(
                call_id=str(item.get("id") or f"call-{uuid.uuid4().hex[:12]}"),
                name=str(name),
                arguments=arguments if isinstance(arguments, dict) else {},
            )
        )
    return calls


def sanitize_assistant_content(
    content: str | None,
    existing_calls: list[ToolCall] | None = None,
) -> tuple[str, list[ToolCall]]:
    """Drop leaked ChatCompletion JSON that weaker models echo into content."""
    text = content or ""
    recovered: list[ToolCall] = []
    pieces: list[str] = []
    index = 0
    decoder = json.JSONDecoder()
    while index < len(text):
        brace = text.find("{", index)
        if brace < 0:
            pieces.append(text[index:])
            break
        pieces.append(text[index:brace])
        try:
            payload, end = decoder.raw_decode(text, brace)
        except json.JSONDecodeError:
            pieces.append(text[brace])
            index = brace + 1
            continue
        if _looks_like_completion_dump(payload):
            recovered.extend(_tool_calls_from_payload(payload))
            index = end
            continue
        pieces.append(text[brace:end])
        index = end
    cleaned = "".join(pieces).strip()
    return cleaned, list(existing_calls or recovered or [])


class MockFunctionCallingAdapter:
    """Offline adapter that emits the same tool-call contract as a real model."""

    provider = "mock"
    model_name = "mock-function-calling"
    input_modalities = frozenset({"text"})

    def __init__(self) -> None:
        self.structured_gateway = MockModelGateway()

    @staticmethod
    def _last_content(messages: list[dict[str, Any]], role: str) -> str:
        for message in reversed(messages):
            if message.get("role") == role:
                content = message.get("content", "")
                if isinstance(content, list):
                    return "\n".join(
                        str(item.get("text", ""))
                        for item in content
                        if isinstance(item, dict) and item.get("type") == "text"
                    )
                return str(content)
        return ""

    def invoke(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        on_token: Any | None = None,
    ) -> ModelTurn:
        tool_message = (
            str(messages[-1].get("content", ""))
            if messages and messages[-1].get("role") == "tool"
            else ""
        )
        if tool_message:
            try:
                result = json.loads(tool_message)
            except json.JSONDecodeError:
                result = {}
            summary = result.get("summary")
            content = str(summary or "工具查询已完成。")
            return self._maybe_stream(
                ModelTurn(
                    provider=self.provider,
                    model=self.model_name,
                    content=content,
                    usage=self._usage(content),
                ),
                on_token,
            )

        question = self._last_content(messages, "user")
        available = {item.get("function", {}).get("name") for item in tools}
        finance_words = (
            "amazon", "亚马逊", "结算", "费用", "交易", "sku", "asin",
            "shipment", "refund", "settlement",
        )
        if "amazon_finance_query" in available and any(
            word in question.lower() for word in finance_words
        ):
            plan = self.structured_gateway.structured(
                AmazonFinanceQueryPlan,
                system_prompt="mock tool planner",
                payload={"objective": question},
            )
            return ModelTurn(
                provider=self.provider,
                model=self.model_name,
                tool_calls=[
                    ToolCall(
                        call_id=f"call-{uuid.uuid4().hex[:12]}",
                        name="amazon_finance_query",
                        arguments=plan.model_dump(mode="json"),
                    )
                ],
            )
        return self._maybe_stream(
            ModelTurn(
                provider=self.provider,
                model=self.model_name,
                content="当前 Runtime 已启用 Function Calling；请提出 Amazon 结算查询问题。",
                usage=self._usage("当前 Runtime 已启用 Function Calling；请提出 Amazon 结算查询问题。"),
            ),
            on_token,
        )

    @staticmethod
    def _usage(content: str) -> dict[str, int]:
        completion = max(1, len(content) // 4)
        prompt = 24
        return {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
        }

    @staticmethod
    def _maybe_stream(turn: ModelTurn, on_token: Any | None) -> ModelTurn:
        if on_token and turn.content:
            on_token(turn.content)
        return turn


class OpenAIFunctionCallingAdapter:
    provider = "openai"
    input_modalities = frozenset({"text"})

    def __init__(
        self,
        settings: Settings,
        *,
        model_name: str | None = None,
        api_key: str | None = None,
        temperature: float | None = None,
    ) -> None:
        from langchain_openai import ChatOpenAI

        resolved_model = model_name or settings.model_name
        resolved_key = api_key if api_key is not None else settings.openai_api_key
        resolved_temperature = (
            temperature if temperature is not None else settings.model_temperature
        )
        options: dict[str, Any] = {
            "model": resolved_model,
            "api_key": resolved_key,
        }
        if resolved_temperature is not None:
            options["temperature"] = resolved_temperature
        self.model_name = resolved_model
        self.model = ChatOpenAI(**options)

    def invoke(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        on_token: Any | None = None,
    ) -> ModelTurn:
        response = self.model.bind_tools(tools).invoke(messages)
        calls = [
            ToolCall(
                call_id=str(item["id"]),
                name=str(item["name"]),
                arguments=dict(item.get("args", {})),
            )
            for item in getattr(response, "tool_calls", [])
        ]
        content = response.content
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False, default=str)
        if on_token and content:
            on_token(content)
        usage = getattr(response, "usage_metadata", None) or {}
        return ModelTurn(
            provider=self.provider,
            model=self.model_name,
            content=content,
            tool_calls=calls,
            usage=dict(usage),
        )


class ZhipuFunctionCallingAdapter:
    """Official zai-sdk adapter with native Function Calling."""

    provider = "zhipu"

    def __init__(
        self,
        settings: Settings,
        *,
        model_name: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        temperature: float | None = None,
        input_modalities: frozenset[str] = frozenset({"text"}),
    ) -> None:
        from zai import ZhipuAiClient

        self.model_name = model_name or settings.zhipu_model_name
        self.input_modalities = input_modalities
        self.temperature = (
            temperature if temperature is not None else settings.model_temperature
        )
        self.max_retries = settings.model_max_retries
        self.backoff_base_seconds = settings.model_backoff_base_seconds
        self.rate_limit_cooldown_seconds = (
            settings.model_rate_limit_cooldown_seconds
        )
        self.client = ZhipuAiClient(
            api_key=api_key if api_key is not None else settings.zai_api_key,
            base_url=base_url if base_url is not None else settings.zhipu_base_url,
            timeout=settings.model_request_timeout_seconds,
            max_retries=0,
        )

    def invoke(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        on_token: Any | None = None,
    ) -> ModelTurn:
        options: dict[str, Any] = {
            "model": self.model_name,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "stream": bool(on_token),
        }
        if self.temperature is not None:
            options["temperature"] = self.temperature
        response = invoke_zhipu_with_backoff(
            self.client.chat.completions.create,
            options,
            max_retries=self.max_retries,
            backoff_base_seconds=self.backoff_base_seconds,
            rate_limit_cooldown_seconds=self.rate_limit_cooldown_seconds,
        )
        if on_token:
            return self._invoke_stream(response, on_token)
        message = response.choices[0].message
        calls: list[ToolCall] = []
        for item in message.tool_calls or []:
            raw_arguments = item.function.arguments or "{}"
            arguments = (
                json.loads(raw_arguments)
                if isinstance(raw_arguments, str)
                else dict(raw_arguments)
            )
            calls.append(
                ToolCall(
                    call_id=str(item.id),
                    name=str(item.function.name),
                    arguments=arguments,
                )
            )
        raw_content = message.content
        if not isinstance(raw_content, str):
            raw_content = (
                json.dumps(raw_content, ensure_ascii=False, default=str)
                if raw_content
                else ""
            )
        return self._turn_from_parts(raw_content, calls, getattr(response, "usage", None))

    def _invoke_stream(self, response: Any, on_token: Any) -> ModelTurn:
        content_parts: list[str] = []
        tool_acc: dict[int, dict[str, str]] = {}
        usage_object = None
        for chunk in response:
            usage_object = getattr(chunk, "usage", None) or usage_object
            choices = getattr(chunk, "choices", None) or []
            if not choices:
                continue
            delta = choices[0].delta
            text = getattr(delta, "content", None) or ""
            if text:
                content_parts.append(text)
                on_token(text)
            for call in getattr(delta, "tool_calls", None) or []:
                slot = tool_acc.setdefault(
                    int(getattr(call, "index", 0) or 0),
                    {"id": "", "name": "", "arguments": ""},
                )
                if getattr(call, "id", None):
                    slot["id"] = str(call.id)
                function = getattr(call, "function", None)
                if function is None:
                    continue
                if getattr(function, "name", None):
                    slot["name"] += str(function.name)
                if getattr(function, "arguments", None):
                    slot["arguments"] += str(function.arguments)
        calls: list[ToolCall] = []
        for slot in tool_acc.values():
            raw_arguments = slot["arguments"] or "{}"
            try:
                arguments = json.loads(raw_arguments)
            except json.JSONDecodeError:
                arguments = {"raw": raw_arguments}
            if not isinstance(arguments, dict):
                arguments = {"raw": raw_arguments}
            calls.append(
                ToolCall(
                    call_id=slot["id"] or f"call-{uuid.uuid4().hex[:12]}",
                    name=slot["name"] or "unknown_tool",
                    arguments=arguments,
                )
            )
        return self._turn_from_parts("".join(content_parts), calls, usage_object)

    def _turn_from_parts(
        self,
        raw_content: str,
        calls: list[ToolCall],
        usage_object: Any,
    ) -> ModelTurn:
        content, recovered = sanitize_assistant_content(raw_content, calls)
        if not calls:
            calls = recovered
        usage = (
            usage_object.model_dump()
            if usage_object is not None and hasattr(usage_object, "model_dump")
            else {}
        )
        return ModelTurn(
            provider=self.provider,
            model=self.model_name,
            content=content,
            tool_calls=calls,
            usage=usage,
        )


@dataclass(frozen=True)
class ModelRoute:
    model_id: str
    adapter_key: str
    provider: str
    model: str
    reason: str


class ModelRouter:
    """Routes chat turns to configured model adapters by model_id."""

    def __init__(
        self,
        adapters: dict[str, FunctionCallingAdapter],
        *,
        default_model_id: str,
        vision_adapter_keys: dict[str, str] | None = None,
    ) -> None:
        if default_model_id not in adapters:
            raise ValueError(f"unknown default model id: {default_model_id}")
        self.adapters = dict(adapters)
        self.default_model_id = default_model_id
        self.vision_adapter_keys = dict(vision_adapter_keys or {})

    def route(
        self,
        *,
        model_id: str | None = None,
        required_modalities: set[str] | None = None,
    ) -> ModelRoute:
        required = required_modalities or {"text"}
        resolved_id = model_id or self.default_model_id
        adapter_key = resolved_id
        reason = f"model={resolved_id}"
        if "image" in required:
            vision_key = self.vision_adapter_keys.get(resolved_id)
            if vision_key and vision_key in self.adapters:
                adapter_key = vision_key
                reason = f"vision model for {resolved_id}"
            else:
                candidate = next(
                    (
                        key
                        for key, adapter in self.adapters.items()
                        if required.issubset(
                            getattr(adapter, "input_modalities", {"text"})
                        )
                        and key.startswith(resolved_id)
                    ),
                    None,
                )
                if candidate:
                    adapter_key = candidate
                    reason = f"supports {','.join(sorted(required))}"
        adapter = self.adapters.get(adapter_key)
        if adapter is None:
            raise ValueError(f"no adapter registered for model: {resolved_id}")
        if not required.issubset(getattr(adapter, "input_modalities", {"text"})):
            raise ValueError(
                f"model {resolved_id} does not support modalities: {sorted(required)}"
            )
        return ModelRoute(
            model_id=resolved_id,
            adapter_key=adapter_key,
            provider=adapter.provider,
            model=adapter.model_name,
            reason=reason,
        )

    def invoke(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        model_id: str | None = None,
        required_modalities: set[str] | None = None,
        on_token: Any | None = None,
    ) -> ModelTurn:
        route = self.route(
            model_id=model_id,
            required_modalities=required_modalities,
        )
        adapter = self.adapters[route.adapter_key]
        if on_token is None:
            return adapter.invoke(messages, tools)
        try:
            return adapter.invoke(messages, tools, on_token=on_token)
        except TypeError:
            turn = adapter.invoke(messages, tools)
            if turn.content:
                on_token(turn.content)
            return turn


def build_adapter_for_model(
    model: Any,
    settings: Settings,
    *,
    vision: bool = False,
) -> FunctionCallingAdapter:
    from ..model_registry import ModelDefinition

    profile = (
        model
        if isinstance(model, ModelDefinition)
        else ModelDefinition.model_validate(model)
    )
    if profile.provider == "openai":
        return OpenAIFunctionCallingAdapter(
            settings,
            model_name=profile.model_name,
            api_key=profile.api_key or None,
            temperature=profile.temperature,
        )
    if profile.provider == "zhipu":
        model_name = profile.vision_model_name if vision else profile.model_name
        if vision and not model_name:
            model_name = profile.model_name
        modalities = (
            frozenset({"text", "image"})
            if vision and profile.vision_model_name
            else frozenset({"text"})
        )
        return ZhipuFunctionCallingAdapter(
            settings,
            model_name=model_name,
            api_key=profile.api_key or None,
            base_url=profile.base_url or None,
            temperature=profile.temperature,
            input_modalities=modalities,
        )
    return MockFunctionCallingAdapter()


def create_model_router_from_registry(
    registry: Any,
    settings: Settings,
) -> ModelRouter:
    adapters: dict[str, FunctionCallingAdapter] = {}
    vision_keys: dict[str, str] = {}
    for model in registry.list(enabled_only=True):
        adapters[model.id] = build_adapter_for_model(model, settings)
        if model.vision_model_name:
            vision_key = f"{model.id}__vision"
            adapters[vision_key] = build_adapter_for_model(
                model, settings, vision=True
            )
            vision_keys[model.id] = vision_key
    if not adapters:
        fallback = build_adapter_for_model(
            registry.default_model(),
            settings,
        )
        default_id = registry.default_model_id()
        adapters[default_id] = fallback
    return ModelRouter(
        adapters,
        default_model_id=registry.default_model_id(),
        vision_adapter_keys=vision_keys,
    )


def create_model_router(settings: Settings) -> ModelRouter:
    from ..model_registry import create_model_registry

    registry = create_model_registry(settings.model_definitions_path, settings)
    return create_model_router_from_registry(registry, settings)
