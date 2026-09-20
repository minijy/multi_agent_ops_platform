from pathlib import Path

import pytest

from ops_agent.config import (
    Settings,
    apply_runtime_overrides,
    update_context_window,
    update_intent_routing,
)


def test_postgres_is_required_and_paths_are_absolute(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = Settings(_env_file=None)
    assert settings.postgres_dsn
    assert settings.runtime_overrides_path.is_absolute()
    assert settings.knowledge_spaces_path.is_absolute()
    assert settings.auth_secret_path.is_absolute()


def test_postgres_requires_dsn():
    settings = Settings(_env_file=None, postgres_dsn="")
    with pytest.raises(ValueError, match="POSTGRES_DSN"):
        settings.validate_runtime()


def test_production_rejects_insecure_or_local_runtime():
    with pytest.raises(ValueError, match="JWT_REQUIRED"):
        Settings(
            _env_file=None,
            app_env="production",
            jwt_secret="shared-secret-at-least-thirty-two-characters",
        ).validate_runtime()

    with pytest.raises(ValueError, match="SUBAGENT_QUEUE_BACKEND"):
        Settings(
            _env_file=None,
            app_env="production",
            jwt_secret="shared-secret-at-least-thirty-two-characters",
            jwt_required=True,
            jwt_issuer="issuer",
            jwt_audience="audience",
            account_bootstrap_token="bootstrap-token-that-is-long-enough",
        ).validate_runtime()


def test_production_configuration_closes_shared_state_requirements():
    settings = Settings(
        _env_file=None,
        app_env="production",
        jwt_secret="shared-secret-at-least-thirty-two-characters",
        jwt_required=True,
        jwt_issuer="issuer",
        jwt_audience="audience",
        account_bootstrap_token="bootstrap-token-that-is-long-enough",
        subagent_queue_backend="db",
    )
    settings.validate_runtime()


def test_zhipu_requires_api_key():
    settings = Settings(
        _env_file=None,
        model_provider="zhipu",
        zai_api_key="",
    )
    with pytest.raises(ValueError, match="ZAI_API_KEY"):
        settings.validate_runtime()


def test_qwen_requires_api_key_only_when_selected_as_env_provider():
    settings = Settings(
        _env_file=None,
        model_provider="qwen",
        dashscope_api_key="",
    )
    with pytest.raises(ValueError, match="DASHSCOPE_API_KEY"):
        settings.validate_runtime()
    page_configured = Settings(_env_file=None, model_provider="mock")
    page_configured.validate_runtime()


def test_deepseek_requires_api_key_only_when_selected_as_env_provider():
    settings = Settings(
        _env_file=None,
        model_provider="deepseek",
        deepseek_api_key="",
    )
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        settings.validate_runtime()


def test_context_window_overrides_roundtrip(tmp_path: Path):
    settings = Settings(
        _env_file=None,
        runtime_overrides_path=tmp_path / "overrides.json",
        context_keep_recent_user_turns=16,
    )
    snapshot = update_context_window(settings, {"keep_recent_user_turns": 4, "max_messages": 20})
    assert snapshot["keep_recent_user_turns"] == 4
    assert snapshot["max_messages"] == 20
    reloaded = Settings(
        _env_file=None,
        runtime_overrides_path=tmp_path / "overrides.json",
        context_keep_recent_user_turns=16,
    )
    apply_runtime_overrides(reloaded)
    assert reloaded.context_keep_recent_user_turns == 4
    assert reloaded.context_max_messages == 20


def test_intent_routing_override_roundtrip(tmp_path: Path):
    settings = Settings(
        _env_file=None,
        runtime_overrides_path=tmp_path / "overrides.json",
        intent_routing_enabled=False,
    )
    snapshot = update_intent_routing(settings, {"enabled": True})
    assert snapshot["enabled"] is True
    assert snapshot["strategy"] == ["hard_match", "small_model", "coordinator"]

    reloaded = Settings(
        _env_file=None,
        runtime_overrides_path=tmp_path / "overrides.json",
        intent_routing_enabled=False,
    )
    apply_runtime_overrides(reloaded)
    assert reloaded.intent_routing_enabled is True
