from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from ops_agent.config import Settings
from ops_agent.model_registry import (
    ModelCreateRequest,
    ModelDefinition,
    create_model_registry,
)
from ops_agent.runtime.agent_loop import AgentRuntime
from ops_agent.runtime.deepseek_adapter import (
    DEFAULT_DEEPSEEK_BASE_URL,
    DeepSeekFunctionCallingAdapter,
    classify_deepseek_error,
    deepseek_thinking_forced,
    history_for_deepseek,
)
from ops_agent.runtime.domain import ModelTurn, ToolCall
from ops_agent.runtime.model_errors import ModelProviderError
from ops_agent.runtime.model_router import (
    ModelRouter,
    build_adapter_for_model,
    strip_reasoning_content,
)


def _settings(**overrides) -> Settings:
    values = dict(_env_file=None, model_provider="mock")
    values.update(overrides)
    return Settings(**values)


class _FakeChunk:
    def __init__(
        self,
        *,
        content: str | None = None,
        reasoning: str | None = None,
        tool_calls: list[Any] | None = None,
        usage: Any | None = None,
    ) -> None:
        if content is None and reasoning is None and tool_calls is None:
            self.choices = []
        else:
            self.choices = [
                SimpleNamespace(
                    delta=SimpleNamespace(
                        content=content,
                        reasoning_content=reasoning,
                        tool_calls=tool_calls,
                    )
                )
            ]
        self.usage = usage


def test_deepseek_thinking_forced_for_reasoner():
    assert deepseek_thinking_forced("deepseek-reasoner") is True
    assert deepseek_thinking_forced("deepseek-chat") is False


def test_history_keeps_reasoning_when_tools_are_present():
    messages = [
        {"role": "user", "content": "查天气"},
        {
            "role": "assistant",
            "content": None,
            "reasoning_content": "先调工具",
            "tool_calls": [{"id": "c1", "type": "function"}],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "晴"},
    ]
    kept = history_for_deepseek(messages, keep_reasoning=True)
    dropped = history_for_deepseek(messages, keep_reasoning=False)
    assert kept[1]["reasoning_content"] == "先调工具"
    assert "reasoning_content" not in dropped[1]


def test_deepseek_adapter_streams_thinking_and_keeps_tool_history(monkeypatch):
    captured: dict[str, Any] = {}
    tool_delta = SimpleNamespace(
        index=0,
        id="call_1",
        function=SimpleNamespace(name="echo", arguments='{"text":"hi"}'),
    )

    def create(**options):
        captured["options"] = options
        return iter(
            [
                _FakeChunk(reasoning="继续上一轮工具调用。"),
                _FakeChunk(tool_calls=[tool_delta]),
            ]
        )

    class FakeClient:
        def __init__(self, **kwargs):
            captured["client"] = kwargs
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))

    import openai

    monkeypatch.setattr(openai, "OpenAI", FakeClient)
    tokens: list[tuple[str, str]] = []
    adapter = DeepSeekFunctionCallingAdapter(
        _settings(),
        model_name="deepseek-chat",
        api_key="sk-test",
        enable_thinking=True,
        reasoning_effort="max",
    )
    turn = adapter.invoke(
        [
            {"role": "user", "content": "查一下"},
            {
                "role": "assistant",
                "content": None,
                "reasoning_content": "旧思考",
                "tool_calls": [{"id": "c0", "type": "function"}],
            },
            {"role": "tool", "tool_call_id": "c0", "content": "ok"},
        ],
        [{"type": "function", "function": {"name": "echo"}}],
        on_token=lambda text, channel="content": tokens.append((channel, text)),
    )

    assert captured["client"]["base_url"] == DEFAULT_DEEPSEEK_BASE_URL
    assert captured["options"]["extra_body"]["thinking"]["type"] == "enabled"
    assert captured["options"]["reasoning_effort"] == "max"
    assert captured["options"]["messages"][1]["reasoning_content"] == "旧思考"
    assert "temperature" not in captured["options"]
    assert turn.reasoning_content == "继续上一轮工具调用。"
    assert [call.name for call in turn.tool_calls] == ["echo"]
    assert tokens[0] == ("reasoning", "继续上一轮工具调用。")


def test_reasoner_forces_thinking_and_plain_chat_drops_cot(monkeypatch):
    captured: dict[str, Any] = {}

    def create(**options):
        captured["options"] = options
        return iter([_FakeChunk(reasoning="想一下", content="42")])

    class FakeClient:
        def __init__(self, **_kwargs):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))

    import openai

    monkeypatch.setattr(openai, "OpenAI", FakeClient)
    adapter = DeepSeekFunctionCallingAdapter(
        _settings(),
        model_name="deepseek-reasoner",
        api_key="sk-test",
        enable_thinking=False,
    )
    turn = adapter.invoke(
        [
            {"role": "user", "content": "第一问"},
            {
                "role": "assistant",
                "content": "第一答",
                "reasoning_content": "不该在无工具时回传",
            },
            {"role": "user", "content": "第二问"},
        ],
        [],
    )
    assert captured["options"]["extra_body"]["thinking"]["type"] == "enabled"
    assert "reasoning_content" not in captured["options"]["messages"][1]
    assert turn.content == "42"
    assert turn.reasoning_content == "想一下"


def test_deepseek_missing_key_uses_hard_stop():
    model = ModelDefinition(
        id="deepseek-no-key",
        name="DeepSeek",
        provider="deepseek",
        model_name="deepseek-chat",
        api_key="",
        enabled=False,
    )
    adapter = build_adapter_for_model(model, _settings())
    with pytest.raises(ModelProviderError) as error:
        adapter.invoke([], [])
    assert error.value.code == "model_api_key_missing"


def test_deepseek_registry_page_config(tmp_path):
    settings = _settings(model_definitions_path=tmp_path / "models.json")
    registry = create_model_registry(settings.model_definitions_path, settings)
    created = registry.create(
        ModelCreateRequest(
            id="deepseek-ui",
            name="DeepSeek Chat",
            provider="deepseek",
            model_name="deepseek-chat",
            api_key="sk-page",
            enable_thinking=True,
            reasoning_effort="high",
        )
    )
    assert created.base_url == DEFAULT_DEEPSEEK_BASE_URL
    item = next(entry for entry in registry.catalog_items() if entry["id"] == "deepseek-ui")
    assert item["api_key"] == "********"
    assert item["enable_thinking"] is True
    assert item["reasoning_effort"] == "high"


def test_assistant_message_keeps_reasoning_for_tool_replay():
    message = AgentRuntime._assistant_message(
        ModelTurn(
            provider="deepseek",
            model="deepseek-chat",
            content="可见回答",
            reasoning_content="内部思考",
            tool_calls=[ToolCall(call_id="c1", name="echo", arguments={"text": "x"})],
        )
    )
    assert message["content"] == "可见回答"
    assert message["reasoning_content"] == "内部思考"


def test_router_strips_reasoning_for_non_deepseek_providers():
    class FakeAdapter:
        provider = "fake"
        model_name = "fake"
        input_modalities = frozenset({"text"})
        seen: list[dict[str, Any]] | None = None

        def invoke(self, messages, _tools, on_token=None):
            self.seen = messages
            return ModelTurn(provider="fake", model="fake", content="ok")

    adapter = FakeAdapter()
    router = ModelRouter({"fake": adapter}, default_model_id="fake")
    router.invoke(
        [{"role": "assistant", "content": "a", "reasoning_content": "hidden"}],
        [],
    )
    assert adapter.seen is not None
    assert "reasoning_content" not in adapter.seen[0]
    assert strip_reasoning_content(
        [{"role": "assistant", "content": "a", "reasoning_content": "x"}]
    )[0]["content"] == "a"


def test_classify_deepseek_auth_and_balance_errors():
    class AuthError(Exception):
        status_code = 401
        body = None

        def __str__(self) -> str:
            return "Invalid API key"

    class BalanceError(Exception):
        status_code = 402
        body = {"error": {"message": "Insufficient Balance"}}

        def __str__(self) -> str:
            return "Insufficient Balance"

    auth = classify_deepseek_error(AuthError())
    assert auth.code == "invalid_api_key"
    balance = classify_deepseek_error(BalanceError())
    assert balance.status_code == 402
    assert balance.automatic_retry is False
