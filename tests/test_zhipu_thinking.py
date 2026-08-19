from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from ops_agent.config import Settings
from ops_agent.model_registry import (
    ModelCreateRequest,
    ModelDefinition,
    create_model_registry,
)
from ops_agent.runtime.domain import ModelTurn
from ops_agent.runtime.model_router import (
    ModelRouter,
    ZhipuFunctionCallingAdapter,
    build_adapter_for_model,
    history_for_zhipu,
    zhipu_supports_reasoning_effort,
    zhipu_supports_thinking,
    zhipu_thinking_forced,
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


def test_zhipu_thinking_model_detection():
    assert zhipu_supports_thinking("glm-4-flash") is False
    assert zhipu_supports_thinking("glm-4.5-flash") is True
    assert zhipu_supports_thinking("glm-4.6v-flash") is True
    assert zhipu_supports_thinking("glm-4.7-flash") is True
    assert zhipu_supports_thinking("glm-5.2") is True
    assert zhipu_thinking_forced("glm-4.7-flash") is True
    assert zhipu_thinking_forced("glm-5.3") is True
    assert zhipu_thinking_forced("glm-5.2") is False
    assert zhipu_supports_reasoning_effort("glm-4.7") is False
    assert zhipu_supports_reasoning_effort("glm-5.1") is False
    assert zhipu_supports_reasoning_effort("glm-5.2") is True


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
    kept = history_for_zhipu(messages, keep_reasoning=True)
    dropped = history_for_zhipu(messages, keep_reasoning=False)
    assert kept[1]["reasoning_content"] == "先调工具"
    assert "reasoning_content" not in dropped[1]


def test_zhipu_adapter_streams_thinking_and_keeps_tool_history(monkeypatch):
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

    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    import zai

    monkeypatch.setattr(zai, "ZhipuAiClient", lambda **kwargs: captured.update(client=kwargs) or fake_client)
    tokens: list[tuple[str, str]] = []
    adapter = ZhipuFunctionCallingAdapter(
        _settings(),
        model_name="glm-5.2",
        api_key="test-key",
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

    assert captured["options"]["thinking"]["type"] == "enabled"
    assert captured["options"]["thinking"]["clear_thinking"] is False
    assert captured["options"]["reasoning_effort"] == "max"
    assert captured["options"]["messages"][1]["reasoning_content"] == "旧思考"
    assert captured["client"]["timeout"] == 180
    assert turn.reasoning_content == "继续上一轮工具调用。"
    assert [call.name for call in turn.tool_calls] == ["echo"]
    assert tokens[0] == ("reasoning", "继续上一轮工具调用。")


def test_glm47_forces_thinking_and_plain_chat_drops_cot(monkeypatch):
    captured: dict[str, Any] = {}

    def create(**options):
        captured["options"] = options
        return iter([_FakeChunk(reasoning="想一下", content="42")])

    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    import zai

    monkeypatch.setattr(zai, "ZhipuAiClient", lambda **_kwargs: fake_client)
    adapter = ZhipuFunctionCallingAdapter(
        _settings(),
        model_name="glm-4.7-flash",
        api_key="test-key",
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
        on_token=lambda text, channel="content": None,
    )
    assert captured["options"]["thinking"]["type"] == "enabled"
    assert "clear_thinking" not in captured["options"]["thinking"]
    assert "reasoning_effort" not in captured["options"]
    assert "reasoning_content" not in captured["options"]["messages"][1]
    assert turn.content == "42"
    assert turn.reasoning_content == "想一下"


def test_glm4_flash_does_not_send_thinking(monkeypatch):
    captured: dict[str, Any] = {}

    def create(**options):
        captured["options"] = options
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="直接答",
                        reasoning_content=None,
                        tool_calls=[],
                    )
                )
            ],
            usage=None,
        )

    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    import zai

    monkeypatch.setattr(zai, "ZhipuAiClient", lambda **kwargs: captured.update(client=kwargs) or fake_client)
    adapter = ZhipuFunctionCallingAdapter(
        _settings(),
        model_name="glm-4-flash",
        api_key="test-key",
        enable_thinking=True,
    )
    turn = adapter.invoke([{"role": "user", "content": "hi"}], [])
    assert "thinking" not in captured["options"]
    assert captured["client"]["timeout"] == 45
    assert turn.content == "直接答"
    assert turn.reasoning_content == ""


def test_router_keeps_reasoning_for_zhipu():
    class FakeAdapter:
        provider = "zhipu"
        model_name = "glm-5.2"
        input_modalities = frozenset({"text"})
        seen: list[dict[str, Any]] | None = None

        def invoke(self, messages, _tools, on_token=None):
            self.seen = messages
            return ModelTurn(provider="zhipu", model="glm-5.2", content="ok")

    adapter = FakeAdapter()
    router = ModelRouter({"zhipu": adapter}, default_model_id="zhipu")
    router.invoke(
        [{"role": "assistant", "content": "a", "reasoning_content": "kept"}],
        [],
    )
    assert adapter.seen is not None
    assert adapter.seen[0]["reasoning_content"] == "kept"


def test_zhipu_registry_page_config(tmp_path):
    settings = _settings(model_definitions_path=tmp_path / "models.json")
    registry = create_model_registry(settings.model_definitions_path, settings)
    created = registry.create(
        ModelCreateRequest(
            id="zhipu-ui",
            name="智谱 GLM",
            provider="zhipu",
            model_name="glm-5.2",
            api_key="sk-page",
            enable_thinking=True,
            reasoning_effort="max",
        )
    )
    adapter = build_adapter_for_model(created, settings)
    assert adapter.provider == "zhipu"
    assert adapter.enable_thinking is True
    assert adapter.reasoning_effort == "max"
    item = next(entry for entry in registry.catalog_items() if entry["id"] == "zhipu-ui")
    assert item["api_key"] == "********"
    assert item["enable_thinking"] is True
    assert item["reasoning_effort"] == "max"


def test_default_zhipu_settings_enable_thinking():
    model = ModelDefinition(
        id="zhipu-default",
        name="智谱默认模型",
        provider="zhipu",
        model_name="glm-5.2",
        api_key="k",
        enable_thinking=True,
        reasoning_effort="high",
    )
    adapter = build_adapter_for_model(model, _settings(zai_api_key="k"))
    assert adapter.enable_thinking is True
    assert adapter.reasoning_effort == "high"
