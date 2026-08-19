import json

import pytest

from ops_agent.config import Settings
from ops_agent.model_registry import (
    ModelCreateRequest,
    ModelUpdateRequest,
    create_model_registry,
)
from ops_agent.runtime.model_errors import ModelProviderError
from ops_agent.runtime.model_router import build_adapter_for_model


def test_fresh_registry_requires_page_configuration(tmp_path):
    definitions = tmp_path / "models.json"
    settings = Settings(
        _env_file=None,
        model_provider="mock",
        model_definitions_path=definitions,
    )

    registry = create_model_registry(definitions, settings)

    assert registry.list() == []
    assert json.loads(definitions.read_text(encoding="utf-8")) == {}
    with pytest.raises(ModelProviderError) as error:
        registry.resolve_model_id(None)
    assert error.value.code == "model_configuration_required"


def test_legacy_bootstrap_mock_is_removed(tmp_path):
    definitions = tmp_path / "models.json"
    definitions.write_text(
        json.dumps(
            {
                "mock-default": {
                    "name": "Mock 模型",
                    "provider": "mock",
                    "model_name": "mock-function-calling",
                    "enabled": True,
                    "is_default": True,
                    "builtin": True,
                }
            }
        ),
        encoding="utf-8",
    )
    registry = create_model_registry(
        definitions,
        Settings(_env_file=None, model_definitions_path=definitions),
    )

    assert registry.list() == []
    assert json.loads(definitions.read_text(encoding="utf-8")) == {}


def test_model_without_own_key_never_falls_back_to_global_key(tmp_path):
    definitions = tmp_path / "models.json"
    definitions.write_text(
        json.dumps(
            {
                "zhipu-default": {
                    "name": "No Key",
                    "provider": "zhipu",
                    "model_name": "glm-test",
                    "api_key": "",
                    "enabled": True,
                    "is_default": True,
                }
            }
        ),
        encoding="utf-8",
    )
    settings = Settings(
        _env_file=None,
        model_provider="zhipu",
        zai_api_key="global-key-must-not-be-used",
        model_definitions_path=definitions,
    )
    registry = create_model_registry(definitions, settings)
    model = registry.get("zhipu-default")

    assert model is not None
    assert {item.id for item in registry.list()} == {"zhipu-default"}
    assert model.api_key == ""
    with pytest.raises(ValueError, match="未配置 API Key"):
        registry.resolve_model_id("zhipu-default")
    adapter = build_adapter_for_model(model, settings)
    with pytest.raises(ModelProviderError) as error:
        adapter.invoke([], [])
    assert error.value.code == "model_api_key_missing"


def test_enabled_remote_model_requires_api_key(tmp_path):
    settings = Settings(
        _env_file=None,
        model_provider="mock",
        model_definitions_path=tmp_path / "models.json",
    )
    registry = create_model_registry(settings.model_definitions_path, settings)

    with pytest.raises(ValueError, match="必须配置 API Key"):
        registry.create(
            ModelCreateRequest(
                id="zhipu-no-key",
                name="No Key",
                provider="zhipu",
                model_name="glm-test",
                enabled=True,
            )
        )
    disabled = registry.create(
        ModelCreateRequest(
            id="zhipu-disabled",
            name="Disabled",
            provider="zhipu",
            model_name="glm-test",
            enabled=False,
        )
    )
    assert not disabled.callable()
    with pytest.raises(ValueError, match="必须配置 API Key"):
        registry.update(disabled.id, ModelUpdateRequest(enabled=True))


def test_model_registry_creates_qwen_from_page_payload(tmp_path):
    settings = Settings(
        _env_file=None,
        model_provider="mock",
        model_definitions_path=tmp_path / "models.json",
    )
    registry = create_model_registry(settings.model_definitions_path, settings)
    created = registry.create(
        ModelCreateRequest(
            id="qwen-ui",
            name="千问",
            provider="qwen",
            model_name="qwen3.7-plus",
            api_key="sk-from-page",
            supports_image_input=False,
            supports_audio_input=True,
            enable_thinking=True,
        )
    )
    adapter = build_adapter_for_model(created, settings)
    assert adapter.provider == "qwen"
    assert created.api_key == "sk-from-page"
    assert created.supports_image_input is False
    assert created.supports_audio_input is True
    assert adapter.input_modalities == frozenset({"text", "audio"})


def test_model_registry_creates_deepseek_from_page_payload(tmp_path):
    settings = Settings(
        _env_file=None,
        model_provider="mock",
        model_definitions_path=tmp_path / "models.json",
    )
    registry = create_model_registry(settings.model_definitions_path, settings)
    created = registry.create(
        ModelCreateRequest(
            id="deepseek-ui",
            name="DeepSeek",
            provider="deepseek",
            model_name="deepseek-chat",
            api_key="sk-from-page",
            enable_thinking=True,
            reasoning_effort="high",
        )
    )
    adapter = build_adapter_for_model(created, settings)
    assert adapter.provider == "deepseek"
    assert created.api_key == "sk-from-page"


def test_model_registry_creates_zhipu_thinking_from_page_payload(tmp_path):
    settings = Settings(
        _env_file=None,
        model_provider="mock",
        model_definitions_path=tmp_path / "models.json",
    )
    registry = create_model_registry(settings.model_definitions_path, settings)
    created = registry.create(
        ModelCreateRequest(
            id="zhipu-ui",
            name="智谱",
            provider="zhipu",
            model_name="glm-5.2",
            api_key="sk-from-page",
            enable_thinking=True,
            reasoning_effort="max",
        )
    )
    adapter = build_adapter_for_model(created, settings)
    assert adapter.provider == "zhipu"
    assert adapter.enable_thinking is True
    assert adapter.reasoning_effort == "max"
