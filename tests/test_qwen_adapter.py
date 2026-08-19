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
from ops_agent.runtime.domain import ModelTurn, ToolCall
from ops_agent.runtime.model_errors import ModelProviderError
from ops_agent.runtime.model_router import ModelRouter, build_adapter_for_model
from ops_agent.runtime.qwen_adapter import (
    DEFAULT_QWEN_BASE_URL,
    QwenFunctionCallingAdapter,
    _history_without_reasoning,
    classify_qwen_error,
    qwen_supports_vision,
    qwen_thinking_forced,
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


def test_qwen_model_helpers():
    assert qwen_thinking_forced("qwen3-vl-235b-a22b-thinking") is True
    assert qwen_thinking_forced("qwen3.7-plus") is False
    assert qwen_supports_vision("qwen3.7-plus") is True
    assert qwen_supports_vision("qwen3-vl-235b-a22b-thinking") is True
    assert qwen_supports_vision("qwen-plus") is False


def test_history_without_reasoning_keeps_answers():
    prepared = _history_without_reasoning(
        [
            {"role": "user", "content": "第一问"},
            {
                "role": "assistant",
                "content": "第一答",
                "reasoning_content": "不该回传的思考",
            },
            {"role": "user", "content": "第二问"},
        ]
    )
    assert prepared[1]["content"] == "第一答"
    assert "reasoning_content" not in prepared[1]


def test_qwen_adapter_streams_thinking_and_strips_history(monkeypatch):
    captured: dict[str, Any] = {}

    def create(**options):
        captured["options"] = options
        return iter(
            [
                _FakeChunk(reasoning="先看历史再回答。"),
                _FakeChunk(content="第二答"),
                _FakeChunk(
                    usage=SimpleNamespace(
                        prompt_tokens=12,
                        completion_tokens=8,
                        total_tokens=20,
                        model_dump=lambda: {
                            "prompt_tokens": 12,
                            "completion_tokens": 8,
                            "total_tokens": 20,
                        },
                    )
                ),
            ]
        )

    class FakeClient:
        def __init__(self, **kwargs):
            captured["client"] = kwargs
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))

    import openai

    monkeypatch.setattr(openai, "OpenAI", FakeClient)
    tokens: list[tuple[str, str]] = []

    adapter = QwenFunctionCallingAdapter(
        _settings(),
        model_name="qwen3.7-plus",
        api_key="sk-test",
        enable_thinking=True,
        thinking_budget=2048,
    )
    turn = adapter.invoke(
        [
            {"role": "user", "content": "第一问"},
            {
                "role": "assistant",
                "content": "第一答",
                "reasoning_content": "旧思考",
            },
            {"role": "user", "content": "第二问"},
        ],
        [],
        on_token=lambda text, channel="content": tokens.append((channel, text)),
    )

    assert captured["client"]["base_url"] == DEFAULT_QWEN_BASE_URL
    assert captured["options"]["stream"] is True
    assert captured["options"]["extra_body"]["enable_thinking"] is True
    assert captured["options"]["extra_body"]["thinking_budget"] == 2048
    assert "reasoning_content" not in captured["options"]["messages"][1]
    assert turn.content == "第二答"
    assert turn.reasoning_content == "先看历史再回答。"
    assert tokens == [("reasoning", "先看历史再回答。"), ("content", "第二答")]


def test_thinking_only_model_forces_thinking_and_collects_tool_calls(monkeypatch):
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
                _FakeChunk(reasoning="要用工具。"),
                _FakeChunk(tool_calls=[tool_delta]),
            ]
        )

    class FakeClient:
        def __init__(self, **_kwargs):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))

    import openai

    monkeypatch.setattr(openai, "OpenAI", FakeClient)
    adapter = QwenFunctionCallingAdapter(
        _settings(),
        model_name="qwen3-vl-235b-a22b-thinking",
        api_key="sk-test",
        enable_thinking=False,
    )
    turn = adapter.invoke([{"role": "user", "content": "查一下"}], [])

    assert captured["options"]["extra_body"]["enable_thinking"] is True
    assert adapter.input_modalities == frozenset({"text", "image"})
    assert [call.name for call in turn.tool_calls] == ["echo"]
    assert turn.tool_calls[0].arguments == {"text": "hi"}
    assert turn.reasoning_content == "要用工具。"


def test_qwen_missing_key_uses_hard_stop():
    model = ModelDefinition(
        id="qwen-no-key",
        name="Qwen",
        provider="qwen",
        model_name="qwen3.7-plus",
        api_key="",
        enabled=False,
    )
    adapter = build_adapter_for_model(model, _settings())
    with pytest.raises(ModelProviderError) as error:
        adapter.invoke([], [])
    assert error.value.code == "model_api_key_missing"


def test_qwen_registry_page_config_defaults_base_url(tmp_path):
    settings = _settings(model_definitions_path=tmp_path / "models.json")
    registry = create_model_registry(settings.model_definitions_path, settings)
    created = registry.create(
        ModelCreateRequest(
            id="qwen-plus",
            name="千问 Plus",
            provider="qwen",
            model_name="qwen3.7-plus",
            api_key="sk-page",
            supports_image_input=True,
            supports_audio_input=True,
            enable_thinking=True,
            thinking_budget=4096,
        )
    )
    assert created.base_url == DEFAULT_QWEN_BASE_URL
    masked = registry.catalog_items()
    item = next(entry for entry in masked if entry["id"] == "qwen-plus")
    assert item["api_key"] == "********"
    assert item["enable_thinking"] is True
    assert item["thinking_budget"] == 4096
    assert item["supports_vision"] is True
    assert item["supports_image"] is True
    assert item["supports_audio"] is True


def test_assistant_message_does_not_echo_reasoning():
    message = AgentRuntime._assistant_message(
        ModelTurn(
            provider="qwen",
            model="qwen3.7-plus",
            content="可见回答",
            reasoning_content="内部思考",
            tool_calls=[ToolCall(call_id="c1", name="echo", arguments={"text": "x"})],
        )
    )
    assert message["content"] == "可见回答"
    assert message["reasoning_content"] == "内部思考"


def test_qwen_native_vision_route(monkeypatch):
    class FakeClient:
        def __init__(self, **_kwargs):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=lambda **_o: []))

    import openai

    monkeypatch.setattr(openai, "OpenAI", FakeClient)
    model = ModelDefinition(
        id="qwen-vl",
        name="Qwen VL",
        provider="qwen",
        model_name="qwen3-vl-235b-a22b-thinking",
        api_key="sk-test",
        supports_image_input=True,
    )
    adapter = build_adapter_for_model(model, _settings())
    router = ModelRouter({"qwen-vl": adapter}, default_model_id="qwen-vl")
    route = router.route(model_id="qwen-vl", required_modalities={"text", "image"})
    assert route.adapter_key == "qwen-vl"


def test_qwen_image_route_requires_explicit_capability(monkeypatch):
    class FakeClient:
        def __init__(self, **_kwargs):
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=lambda **_options: [])
            )

    import openai

    monkeypatch.setattr(openai, "OpenAI", FakeClient)
    model = ModelDefinition(
        id="qwen-text",
        name="Qwen Text",
        provider="qwen",
        model_name="qwen3.7-plus",
        api_key="sk-test",
        supports_image_input=False,
    )
    adapter = build_adapter_for_model(model, _settings())
    router = ModelRouter({"qwen-text": adapter}, default_model_id="qwen-text")

    with pytest.raises(ModelProviderError) as error:
        router.route(
            model_id="qwen-text", required_modalities={"text", "image"}
        )
    assert error.value.code == "model_input_modality_unsupported"


def test_classify_qwen_quota_and_auth_errors():
    class QuotaError(Exception):
        status_code = 403
        body = {"error": {"code": "AllocationQuota.FreeTierOnly", "message": "expired"}}

        def __str__(self) -> str:
            return "AllocationQuota.FreeTierOnly"

    class AuthError(Exception):
        status_code = 401
        body = None

        def __str__(self) -> str:
            return "Invalid API-key provided"

    quota = classify_qwen_error(QuotaError())
    assert quota.status_code == 403
    assert quota.automatic_retry is False
    auth = classify_qwen_error(AuthError())
    assert auth.code == "invalid_api_key"
    assert auth.status_code == 401
