from __future__ import annotations

import json
import random
import time
import uuid
from typing import Any, Callable

from ..config import Settings
from .domain import ModelTurn, ToolCall
from .model_errors import ModelProviderError, _retry_after

DEFAULT_QWEN_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_THINKING_BUDGET = 8192


def qwen_thinking_forced(model_name: str) -> bool:
    return "thinking" in str(model_name or "").lower()


def qwen_supports_vision(model_name: str) -> bool:
    name = str(model_name or "").lower()
    if "vl" in name:
        return True
    return name.startswith(("qwen3.5", "qwen3.6", "qwen3.7", "qwen3.8"))


def classify_qwen_error(exc: Exception) -> ModelProviderError:
    message = str(exc)
    status = int(getattr(exc, "status_code", 503) or 503)
    code = "upstream_error"
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error", body)
        if isinstance(error, dict):
            code = str(error.get("code") or error.get("type") or code)
            message = str(error.get("message") or message)
    lowered = message.lower()
    if "freetieronly" in lowered or "allocationquota" in lowered:
        return ModelProviderError(
            provider="qwen",
            code=str(code),
            user_message="通义千问免费额度已用完或已到期，请在百炼控制台充值或更换模型。",
            status_code=403,
            retry_after_seconds=300,
            automatic_retry=False,
        )
    if status == 401 or "invalid api-key" in lowered or "unauthorized" in lowered:
        return ModelProviderError(
            provider="qwen",
            code="invalid_api_key",
            user_message="通义千问 API Key 无效，请在系统设置中重新填写。",
            status_code=401,
            retry_after_seconds=1,
            automatic_retry=False,
        )
    if status == 429:
        return ModelProviderError(
            provider="qwen",
            code=str(code),
            user_message="通义千问请求过于频繁，请稍后再试。",
            status_code=429,
            retry_after_seconds=_retry_after(exc, 15),
            automatic_retry=False,
        )
    return ModelProviderError(
        provider="qwen",
        code=str(code),
        user_message="通义千问服务暂时不可用，请稍后重试。",
        status_code=status if status >= 400 else 503,
        retry_after_seconds=_retry_after(exc, 5),
        automatic_retry=status >= 500,
    )


def invoke_qwen_with_backoff(
    create: Callable[..., Any],
    options: dict[str, Any],
    *,
    max_retries: int,
    backoff_base_seconds: float,
) -> Any:
    from openai import APIConnectionError, APIStatusError, APITimeoutError

    for attempt in range(max_retries + 1):
        try:
            return create(**options)
        except (APIStatusError, APIConnectionError, APITimeoutError) as exc:
            classified = classify_qwen_error(exc)
            if not classified.automatic_retry or attempt >= max_retries:
                raise classified from exc
            ceiling = backoff_base_seconds * (2**attempt)
            time.sleep(random.uniform(0, max(0.0, ceiling)))
    raise AssertionError("unreachable")


class QwenFunctionCallingAdapter:
    """DashScope OpenAI-compatible adapter with thinking and multi-turn chat."""

    provider = "qwen"

    def __init__(
        self,
        settings: Settings,
        *,
        model_name: str,
        api_key: str,
        base_url: str | None = None,
        temperature: float | None = None,
        enable_thinking: bool | None = None,
        thinking_budget: int | None = None,
        input_modalities: frozenset[str] | None = None,
    ) -> None:
        from openai import OpenAI

        self.model_name = model_name
        self.input_modalities = input_modalities or (
            frozenset({"text", "image"})
            if qwen_supports_vision(model_name)
            else frozenset({"text"})
        )
        self.temperature = (
            temperature if temperature is not None else settings.model_temperature
        )
        self.enable_thinking = (
            True
            if qwen_thinking_forced(model_name)
            else (True if enable_thinking is None else bool(enable_thinking))
        )
        self.thinking_budget = thinking_budget or DEFAULT_THINKING_BUDGET
        self.max_retries = settings.model_max_retries
        self.backoff_base_seconds = settings.model_backoff_base_seconds
        timeout = settings.model_request_timeout_seconds
        if self.enable_thinking:
            timeout = max(timeout, 180)
        self.client = OpenAI(
            api_key=api_key,
            base_url=(base_url or DEFAULT_QWEN_BASE_URL).rstrip("/"),
            timeout=timeout,
            max_retries=0,
        )

    def invoke(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        on_token: Any | None = None,
    ) -> ModelTurn:
        options = self._request_options(messages, tools)
        response = invoke_qwen_with_backoff(
            self.client.chat.completions.create,
            options,
            max_retries=self.max_retries,
            backoff_base_seconds=self.backoff_base_seconds,
        )
        return self._consume_stream(response, on_token)

    def _request_options(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> dict[str, Any]:
        extra_body: dict[str, Any] = {"enable_thinking": self.enable_thinking}
        if self.enable_thinking:
            extra_body["thinking_budget"] = self.thinking_budget
        options: dict[str, Any] = {
            "model": self.model_name,
            "messages": _history_without_reasoning(messages),
            "stream": True,
            "stream_options": {"include_usage": True},
            "extra_body": extra_body,
        }
        if tools:
            options["tools"] = tools
            options["tool_choice"] = "auto"
        if self.temperature is not None:
            options["temperature"] = self.temperature
        return options

    def _consume_stream(self, response: Any, on_token: Any | None) -> ModelTurn:
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_acc: dict[int, dict[str, str]] = {}
        usage_object = None
        for chunk in response:
            usage_object = getattr(chunk, "usage", None) or usage_object
            choices = getattr(chunk, "choices", None) or []
            if not choices:
                continue
            delta = choices[0].delta
            reasoning = getattr(delta, "reasoning_content", None) or ""
            if reasoning:
                reasoning_parts.append(reasoning)
                _emit_token(on_token, reasoning, channel="reasoning")
            text = getattr(delta, "content", None) or ""
            if text:
                content_parts.append(text)
                _emit_token(on_token, text)
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
        from .model_router import sanitize_assistant_content

        raw_content = "".join(content_parts)
        content, recovered = sanitize_assistant_content(raw_content, calls)
        if not calls:
            calls = recovered
        usage = _usage_dict(usage_object)
        return ModelTurn(
            provider=self.provider,
            model=self.model_name,
            content=content,
            reasoning_content="".join(reasoning_parts),
            tool_calls=calls,
            usage=usage,
        )


def _emit_token(on_token: Any | None, text: str, *, channel: str = "content") -> None:
    if not on_token or not text:
        return
    try:
        on_token(text, channel=channel)
    except TypeError:
        if channel == "content":
            on_token(text)


def _usage_dict(usage_object: Any) -> dict[str, Any]:
    if usage_object is None:
        return {}
    if hasattr(usage_object, "model_dump"):
        return usage_object.model_dump()
    return {
        key: getattr(usage_object, key)
        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        if getattr(usage_object, key, None) is not None
    }


def _history_without_reasoning(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Qwen multi-turn docs: keep answers, do not send prior thinking traces."""
    prepared: list[dict[str, Any]] = []
    for message in messages:
        item = dict(message)
        item.pop("reasoning_content", None)
        prepared.append(item)
    return prepared
