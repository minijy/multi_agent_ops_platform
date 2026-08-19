from pathlib import Path

import pytest

from ops_agent.config import Settings, apply_runtime_overrides, update_context_window


def test_sqlite_is_default_and_paths_are_absolute(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = Settings(_env_file=None)
    assert settings.control_plane_backend == "sqlite"
    assert settings.session_event_backend == "sqlite"
    assert settings.platform_db_path.is_absolute()
    assert settings.session_event_path.is_absolute()
    assert settings.knowledge_spaces_path.is_absolute()


def test_postgres_requires_dsn():
    settings = Settings(
        _env_file=None, control_plane_backend="postgres", postgres_dsn=""
    )
    with pytest.raises(ValueError, match="POSTGRES_DSN"):
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
    snapshot = update_context_window(
        settings, {"keep_recent_user_turns": 4, "max_messages": 20}
    )
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
