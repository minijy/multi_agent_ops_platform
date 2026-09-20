from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .persistence import write_json_atomic

CONTEXT_WINDOW_API_TO_FIELD = {
    "enabled": "context_window_enabled",
    "keep_recent_user_turns": "context_keep_recent_user_turns",
    "max_messages": "context_max_messages",
    "max_chars": "context_max_chars",
    "tool_max_rows": "context_tool_max_rows",
    "tool_max_chars": "context_tool_max_chars",
}

INTENT_ROUTING_API_TO_FIELD = {
    "enabled": "intent_routing_enabled",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    app_env: str = "development"
    app_host: str = "127.0.0.1"
    app_port: int = 8100
    log_level: str = "INFO"
    app_api_key: str = ""
    app_replica_count: int = Field(default=1, ge=1, le=256)

    runtime_overrides_path: Path = Path("data/runtime_overrides.json")
    auth_secret_path: Path = Path("data/platform.auth-secret")
    agent_definitions_path: Path = Path("data/agent_definitions.json")
    connection_definitions_path: Path = Path("data/connections.json")
    connection_secrets_path: Path = Path("data/connection_secrets.json")
    tool_bindings_path: Path = Path("data/tool_bindings.json")
    knowledge_spaces_path: Path = Path("data/knowledge_spaces.json")
    knowledge_api_backend: Literal["ecommerce_graphrag", "wenshu"] = "ecommerce_graphrag"
    knowledge_api_url: str = ""
    knowledge_api_token: str = ""
    default_tenant_id: str = "tenant-a"
    model_definitions_path: Path = Path("data/model_definitions.json")
    model_secrets_path: Path | None = None
    attachment_path: Path = Path("data/attachments")
    attachment_max_image_bytes: int = Field(default=5 * 1024 * 1024, ge=1024)
    attachment_max_images_per_message: int = Field(default=20, ge=1, le=100)
    attachment_max_image_pixels: int = Field(default=40_000_000, ge=1_000_000)
    skills_paths: str = "skills"
    mcp_config_path: Path = Path("config/mcp_servers.json")
    postgres_dsn: str = "postgresql://ops_agent:ops_agent@127.0.0.1:5432/ops_agent"
    analytics_statement_timeout_ms: int = Field(default=5000, ge=100, le=60_000)

    # Provider credentials are configured in the control plane UI. This field
    # remains only for isolated legacy tests and must not bootstrap production.
    model_provider: Literal["unconfigured", "mock", "openai", "zhipu", "qwen", "deepseek"] = (
        "unconfigured"
    )
    model_name: str = "gpt-5.6-sol"
    openai_api_key: str = ""
    model_temperature: float | None = None
    model_request_timeout_seconds: float = Field(default=45, ge=5, le=300)
    model_max_retries: int = Field(default=1, ge=0, le=5)
    model_backoff_base_seconds: float = Field(default=1, ge=0.1, le=30)
    model_rate_limit_cooldown_seconds: int = Field(default=30, ge=1, le=3600)
    zai_api_key: str = ""
    zhipu_base_url: str = "https://open.bigmodel.cn/api/paas/v4/"
    zhipu_model_name: str = "glm-5.2"
    zhipu_vision_model_name: str = "glm-4.6v-flash"
    dashscope_api_key: str = ""
    qwen_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    qwen_model_name: str = "qwen3.7-plus"
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model_name: str = "deepseek-chat"

    max_tool_steps: int = Field(default=8, ge=1, le=30)
    run_token_budget: int = Field(default=30_000, ge=1000)
    context_window_enabled: bool = True
    context_keep_recent_user_turns: int = Field(default=16, ge=1, le=200)
    context_max_messages: int = Field(default=64, ge=8, le=500)
    context_max_chars: int = Field(default=80_000, ge=4_000, le=2_000_000)
    context_tool_max_rows: int = Field(default=12, ge=1, le=200)
    context_tool_max_chars: int = Field(default=4_000, ge=500, le=80_000)
    subagent_queue_backend: Literal["inline", "db"] = "inline"
    subagent_worker_count: int = Field(default=4, ge=1, le=32)
    subagent_max_depth: int = Field(default=3, ge=0, le=8)
    subagent_default_timeout_seconds: float = Field(default=120, ge=5, le=1800)
    subagent_default_token_budget: int = Field(default=8000, ge=256)
    subagent_lease_seconds: float = Field(default=30, ge=5, le=600)
    subagent_lease_renew_seconds: float = Field(default=10, ge=0.1, le=300)
    subagent_worker_poll_seconds: float = Field(default=0.5, ge=0.05, le=30)
    subagent_max_attempts: int = Field(default=3, ge=1, le=20)
    subagent_worker_id: str = ""
    intent_routing_enabled: bool = False
    intent_routing_base_url: str = "http://127.0.0.1:8001/v1"
    intent_routing_api_key: str = ""
    intent_routing_model: str = "qwen3-1.7b-intent-router"
    intent_routing_timeout_seconds: float = Field(default=3.0, ge=0.2, le=30)
    intent_routing_history_messages: int = Field(default=8, ge=0, le=32)
    intent_routing_parallel_limit: int = Field(default=3, ge=1, le=3)
    memory_enabled: bool = True
    memory_semantic_backend: Literal["local", "pgvector", "qdrant"] = "local"
    memory_embedding_provider: Literal["hash", "sentence_transformers"] = "hash"
    memory_embedding_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    memory_embedding_dimensions: int = Field(default=384, ge=64, le=4096)
    memory_qdrant_collection: str = "agent_memory_items_v1"
    qdrant_url: str = ""
    qdrant_api_key: str = ""
    memory_snapshot_limit: int = Field(default=8, ge=1, le=50)
    memory_snapshot_max_chars: int = Field(default=2400, ge=400, le=20000)
    memory_relevance_threshold: float = Field(default=0.12, ge=0, le=1)
    memory_default_expiry_days: int | None = Field(default=365, ge=1, le=3650)
    memory_auto_extract_enabled: bool = True
    memory_sensitive_data_policy: Literal["block", "review"] = "block"
    memory_decay_after_days: int = Field(default=180, ge=1, le=3650)
    memory_worker_poll_seconds: float = Field(default=60, ge=5, le=3600)
    sandbox_workspace_root: Path = Path(".")
    sandbox_timeout_seconds: float = Field(default=30, ge=1, le=600)
    sandbox_max_output_bytes: int = Field(default=65536, ge=1024)
    sandbox_full_access_enabled: bool = False
    agent_stream_max_concurrency: int = Field(default=32, ge=1, le=512)
    session_redis_url: str = ""
    session_lock_wait_seconds: float = Field(default=5.0, ge=0.05, le=120)
    session_lock_ttl_seconds: int = Field(default=600, ge=30, le=3600)
    session_idempotency_ttl_seconds: int = Field(default=86_400, ge=60, le=604_800)

    jwt_secret: str = ""
    jwt_issuer: str = ""
    jwt_audience: str = ""
    jwt_required: bool = False
    account_access_token_minutes: int = Field(default=15, ge=5, le=1440)
    account_refresh_token_days: int = Field(default=7, ge=1, le=90)
    account_max_login_attempts: int = Field(default=5, ge=3, le=20)
    account_lock_minutes: int = Field(default=15, ge=1, le=1440)
    account_bootstrap_token: str = Field(default="", min_length=0, max_length=512)
    otel_service_name: str = "ops-agent"
    otel_exporter: Literal["none", "console", "otlp"] = "none"
    otel_exporter_otlp_endpoint: str = ""

    @field_validator(
        "runtime_overrides_path",
        "auth_secret_path",
        "agent_definitions_path",
        "connection_definitions_path",
        "connection_secrets_path",
        "tool_bindings_path",
        "knowledge_spaces_path",
        "model_definitions_path",
        "attachment_path",
        "mcp_config_path",
        "sandbox_workspace_root",
    )
    @classmethod
    def make_path_absolute(cls, value: Path) -> Path:
        return value.expanduser().resolve()

    def validate_runtime(self) -> None:
        if not self.postgres_dsn.strip():
            raise ValueError("POSTGRES_DSN is required")
        if self.app_env == "production":
            if len(self.jwt_secret) < 32:
                raise ValueError("JWT_SECRET with at least 32 characters is required in production")
            if not self.jwt_required:
                raise ValueError("JWT_REQUIRED=true is required in production")
            if not self.jwt_issuer or not self.jwt_audience:
                raise ValueError("JWT_ISSUER and JWT_AUDIENCE are required in production")
            if self.subagent_queue_backend != "db":
                raise ValueError("SUBAGENT_QUEUE_BACKEND=db is required in production")
            if self.app_replica_count != 1:
                raise ValueError(
                    "APP_REPLICA_COUNT must remain 1 until configuration registries "
                    "use shared persistence"
                )
            if not self.session_redis_url.strip():
                raise ValueError("SESSION_REDIS_URL is required in production")
        if self.jwt_required and not self.jwt_secret:
            raise ValueError("JWT_SECRET is required when JWT_REQUIRED=true")
        # Qdrant is normally configured per tenant from the connector page.
        # Legacy environment fields remain an optional fallback for upgrades.
        if self.model_provider == "openai" and not self.openai_api_key:
            raise ValueError("OPENAI_API_KEY is required when MODEL_PROVIDER=openai")
        if self.model_provider == "zhipu" and not self.zai_api_key:
            raise ValueError("ZAI_API_KEY is required when MODEL_PROVIDER=zhipu")
        if self.model_provider == "qwen" and not self.dashscope_api_key:
            raise ValueError("DASHSCOPE_API_KEY is required when MODEL_PROVIDER=qwen")
        if self.model_provider == "deepseek" and not self.deepseek_api_key:
            raise ValueError("DEEPSEEK_API_KEY is required when MODEL_PROVIDER=deepseek")
        # Knowledge retrieval is delegated to KNOWLEDGE_API_BACKEND.
        # Legacy QDRANT_* environment fields are retained only for memory search.


def context_window_snapshot(settings: Settings) -> dict[str, Any]:
    return {
        api_name: getattr(settings, field)
        for api_name, field in CONTEXT_WINDOW_API_TO_FIELD.items()
    }


def apply_runtime_overrides(settings: Settings) -> Settings:
    path = settings.runtime_overrides_path
    if not path.is_file():
        return settings
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return settings
    if not isinstance(payload, dict):
        return settings
    overridable = {
        *CONTEXT_WINDOW_API_TO_FIELD.values(),
        *INTENT_ROUTING_API_TO_FIELD.values(),
    }
    updates = {field: payload[field] for field in overridable if field in payload}
    if not updates:
        return settings
    validated = Settings.model_validate({**settings.model_dump(), **updates})
    for field in overridable:
        setattr(settings, field, getattr(validated, field))
    return settings


def update_context_window(settings: Settings, updates: dict[str, Any]) -> dict[str, Any]:
    mapped = {
        CONTEXT_WINDOW_API_TO_FIELD[key]: value
        for key, value in updates.items()
        if key in CONTEXT_WINDOW_API_TO_FIELD and value is not None
    }
    if mapped:
        validated = Settings.model_validate({**settings.model_dump(), **mapped})
        for field in CONTEXT_WINDOW_API_TO_FIELD.values():
            setattr(settings, field, getattr(validated, field))
        path = settings.runtime_overrides_path
        path.parent.mkdir(parents=True, exist_ok=True)
        existing: dict[str, Any] = {}
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    existing = loaded
            except (OSError, json.JSONDecodeError):
                existing = {}
        existing.update(
            {field: getattr(settings, field) for field in CONTEXT_WINDOW_API_TO_FIELD.values()}
        )
        write_json_atomic(path, existing)
    return context_window_snapshot(settings)


def intent_routing_snapshot(settings: Settings) -> dict[str, Any]:
    return {
        "enabled": settings.intent_routing_enabled,
        "configured": bool(settings.intent_routing_base_url.strip()),
        "model": settings.intent_routing_model,
        "strategy": ["hard_match", "small_model", "coordinator"],
    }


def update_intent_routing(settings: Settings, updates: dict[str, Any]) -> dict[str, Any]:
    mapped = {
        INTENT_ROUTING_API_TO_FIELD[key]: value
        for key, value in updates.items()
        if key in INTENT_ROUTING_API_TO_FIELD and value is not None
    }
    if mapped:
        validated = Settings.model_validate({**settings.model_dump(), **mapped})
        for field in INTENT_ROUTING_API_TO_FIELD.values():
            setattr(settings, field, getattr(validated, field))
        path = settings.runtime_overrides_path
        path.parent.mkdir(parents=True, exist_ok=True)
        existing: dict[str, Any] = {}
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    existing = loaded
            except (OSError, json.JSONDecodeError):
                existing = {}
        existing.update(
            {field: getattr(settings, field) for field in INTENT_ROUTING_API_TO_FIELD.values()}
        )
        write_json_atomic(path, existing)
    return intent_routing_snapshot(settings)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    apply_runtime_overrides(settings)
    settings.validate_runtime()
    return settings
