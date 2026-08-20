from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from .config import Settings
from .runtime.model_errors import ModelProviderError
from .connections import LocalSecretStore
from .persistence import write_json_atomic


ModelProvider = Literal["mock", "openai", "zhipu", "qwen", "deepseek"]
ConfigurableModelProvider = Literal["openai", "zhipu", "qwen", "deepseek"]
SECRET_MASK = "********"
QWEN_COMPATIBLE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEEPSEEK_COMPATIBLE_BASE_URL = "https://api.deepseek.com"


class ModelDefinition(BaseModel):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    name: str = Field(min_length=1, max_length=120)
    provider: ModelProvider
    model_name: str = Field(min_length=1, max_length=120)
    api_key: str = Field(default="", max_length=512)
    base_url: str = Field(default="", max_length=256)
    vision_model_name: str = Field(default="", max_length=120)
    supports_image_input: bool = False
    supports_audio_input: bool = False
    enabled: bool = True
    is_default: bool = False
    temperature: float | None = Field(default=None, ge=0, le=2)
    enable_thinking: bool | None = None
    thinking_budget: int | None = Field(default=None, ge=1, le=38912)
    reasoning_effort: Literal["low", "high", "max"] | None = None
    builtin: bool = False

    @field_validator("id")
    @classmethod
    def normalize_id(cls, value: str) -> str:
        return value.strip().lower()

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_vision_capability(cls, value: Any) -> Any:
        if isinstance(value, dict) and "supports_image_input" not in value:
            value = dict(value)
            value["supports_image_input"] = bool(value.get("vision_model_name"))
        return value

    @model_validator(mode="after")
    def apply_provider_defaults(self) -> "ModelDefinition":
        if self.provider == "qwen" and not str(self.base_url or "").strip():
            self.base_url = QWEN_COMPATIBLE_BASE_URL
        if self.provider == "deepseek" and not str(self.base_url or "").strip():
            self.base_url = DEEPSEEK_COMPATIBLE_BASE_URL
        return self

    def api_key_configured(self) -> bool:
        return self.provider == "mock" or bool(self.api_key.strip())

    def callable(self) -> bool:
        return self.enabled and self.api_key_configured()


class ModelCreateRequest(BaseModel):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    name: str = Field(min_length=1, max_length=120)
    provider: ConfigurableModelProvider
    model_name: str = Field(min_length=1, max_length=120)
    api_key: str = Field(default="", max_length=512)
    base_url: str = Field(default="", max_length=256)
    vision_model_name: str = Field(default="", max_length=120)
    supports_image_input: bool = False
    supports_audio_input: bool = False
    enabled: bool = True
    is_default: bool = False
    temperature: float | None = Field(default=None, ge=0, le=2)
    enable_thinking: bool | None = None
    thinking_budget: int | None = Field(default=None, ge=1, le=38912)
    reasoning_effort: Literal["low", "high", "max"] | None = None


class ModelUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    provider: ConfigurableModelProvider | None = None
    model_name: str | None = Field(default=None, min_length=1, max_length=120)
    api_key: str | None = Field(default=None, max_length=512)
    base_url: str | None = None
    vision_model_name: str | None = Field(default=None, max_length=120)
    supports_image_input: bool | None = None
    supports_audio_input: bool | None = None
    enabled: bool | None = None
    is_default: bool | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)
    enable_thinking: bool | None = None
    thinking_budget: int | None = Field(default=None, ge=1, le=38912)
    reasoning_effort: Literal["low", "high", "max"] | None = None


def default_models_from_settings(settings: Settings) -> list[ModelDefinition]:
    if settings.model_provider == "zhipu":
        return [
            ModelDefinition(
                id="zhipu-default",
                name="智谱默认模型",
                provider="zhipu",
                model_name=settings.zhipu_model_name,
                api_key=settings.zai_api_key,
                base_url=settings.zhipu_base_url,
                vision_model_name=settings.zhipu_vision_model_name,
                enabled=True,
                is_default=True,
                temperature=settings.model_temperature,
                enable_thinking=True,
                reasoning_effort="high",
                builtin=True,
            )
        ]
    if settings.model_provider == "openai":
        return [
            ModelDefinition(
                id="openai-default",
                name="OpenAI 默认模型",
                provider="openai",
                model_name=settings.model_name,
                api_key=settings.openai_api_key,
                enabled=True,
                is_default=True,
                temperature=settings.model_temperature,
                builtin=True,
            )
        ]
    if settings.model_provider == "qwen":
        return [
            ModelDefinition(
                id="qwen-default",
                name="通义千问默认模型",
                provider="qwen",
                model_name=settings.qwen_model_name,
                api_key=settings.dashscope_api_key,
                base_url=settings.qwen_base_url,
                enabled=True,
                is_default=True,
                temperature=settings.model_temperature,
                enable_thinking=True,
                builtin=True,
            )
        ]
    if settings.model_provider == "deepseek":
        return [
            ModelDefinition(
                id="deepseek-default",
                name="DeepSeek 默认模型",
                provider="deepseek",
                model_name=settings.deepseek_model_name,
                api_key=settings.deepseek_api_key,
                base_url=settings.deepseek_base_url,
                enabled=True,
                is_default=True,
                temperature=settings.model_temperature,
                enable_thinking=True,
                reasoning_effort="high",
                builtin=True,
            )
        ]
    if settings.model_provider == "mock":
        return [
            ModelDefinition(
                id="mock-default",
                name="Mock 模型",
                provider="mock",
                model_name="mock-function-calling",
                enabled=True,
                is_default=True,
                builtin=True,
            )
        ]
    raise ValueError("尚未配置模型，请通过系统设置页添加模型")


def mask_model_definition(model: ModelDefinition) -> dict[str, Any]:
    payload = model.model_dump(mode="json")
    secret = str(payload.get("api_key") or "")
    payload["api_key"] = SECRET_MASK if secret else ""
    payload["api_key_configured"] = bool(secret)
    payload["callable"] = model.callable()
    payload["supports_vision"] = model.supports_image_input
    payload["supports_image"] = model.supports_image_input
    payload["supports_audio"] = model.supports_audio_input
    return payload


class ModelRegistry:
    def __init__(self, path: Path, settings: Settings) -> None:
        self.path = path
        self.settings = settings
        secrets_path = settings.model_secrets_path or path.with_name("model_secrets.json")
        self.secrets = LocalSecretStore(secrets_path)
        self._models: dict[str, ModelDefinition] = {}
        self.reload()

    @staticmethod
    def _secret_ref(model_id: str) -> str:
        return f"model/{model_id}"

    def reload(self) -> None:
        stored = self._read_file()
        merged: dict[str, ModelDefinition] = {}
        migrated = False
        for model_id, override in stored.items():
            try:
                override = dict(override)
                embedded_secret = str(override.pop("api_key", "") or "").strip()
                if embedded_secret:
                    self.secrets.put(
                        self._secret_ref(model_id), {"api_key": embedded_secret}
                    )
                    migrated = True
                stored_secret = self.secrets.get(self._secret_ref(model_id)).get(
                    "api_key", ""
                )
                model = ModelDefinition.model_validate(
                    {"id": model_id, **override, "api_key": stored_secret}
                )
                # Remove the bootstrap entry created by older releases. Mock
                # remains an internal test adapter, never a page model.
                if model.provider == "mock" and (
                    model.id == "mock-default" or model.builtin
                ):
                    migrated = True
                    continue
                if model.builtin:
                    model = model.model_copy(update={"builtin": False})
                    migrated = True
                merged[model_id] = model
            except Exception:
                continue
        # An empty registry is a valid first-run state. Production must never
        # silently become usable through the offline Mock adapter: an admin has
        # to create a real provider configuration from the settings page.
        self._ensure_default(merged)
        self._models = merged
        if not self.path.is_file() or migrated:
            self.save()

    def save(self) -> None:
        payload = {
            model_id: model.model_dump(mode="json", exclude={"api_key"})
            for model_id, model in self._models.items()
        }
        write_json_atomic(self.path, payload)

    def _read_file(self) -> dict[str, dict[str, Any]]:
        if not self.path.is_file():
            return {}
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return {}
        if not isinstance(loaded, dict):
            return {}
        return {
            key: value
            for key, value in loaded.items()
            if isinstance(key, str) and isinstance(value, dict)
        }

    @staticmethod
    def _ensure_default(models: dict[str, ModelDefinition]) -> None:
        enabled = [item for item in models.values() if item.enabled]
        if not enabled:
            return
        defaults = [item for item in enabled if item.is_default]
        if len(defaults) == 1:
            return
        chosen = defaults[0] if defaults else enabled[0]
        for model_id, model in models.items():
            models[model_id] = model.model_copy(
                update={"is_default": model_id == chosen.id}
            )

    def list(self, *, enabled_only: bool = False) -> list[ModelDefinition]:
        items = list(self._models.values())
        if enabled_only:
            items = [item for item in items if item.enabled]
        return sorted(items, key=lambda item: (not item.is_default, item.name.lower()))

    def get(self, model_id: str) -> ModelDefinition | None:
        return self._models.get(model_id)

    def default_model(self) -> ModelDefinition:
        for item in self.list(enabled_only=True):
            if item.is_default:
                return item
        enabled = self.list(enabled_only=True)
        if not enabled:
            raise ValueError("no enabled model configured")
        return enabled[0]

    def default_model_id(self) -> str:
        return self.default_model().id

    def resolve_model_id(self, model_id: str | None) -> str:
        if model_id:
            model = self.get(model_id)
            if model is None:
                raise KeyError(model_id)
            if not model.enabled:
                raise ValueError(f"model is disabled: {model_id}")
            if not model.api_key_configured():
                raise ValueError(
                    f"模型 {model_id} 未配置 API Key，无法调用"
                )
            return model.id
        callable_models = [item for item in self.list() if item.callable()]
        if not callable_models:
            raise ModelProviderError(
                provider="configuration",
                code="model_configuration_required",
                user_message=(
                    "尚未配置可用模型，请管理员前往系统设置 → "
                    "模型配置添加并启用模型。"
                ),
                status_code=503,
                retry_after_seconds=1,
                automatic_retry=False,
            )
        default = next(
            (item for item in callable_models if item.is_default),
            callable_models[0],
        )
        return default.id

    @staticmethod
    def _validate_activation(model: ModelDefinition) -> None:
        if model.enabled and not model.api_key_configured():
            raise ValueError("启用模型前必须配置 API Key")

    def create(self, payload: ModelCreateRequest) -> ModelDefinition:
        model_id = payload.id.strip().lower()
        if model_id in self._models:
            raise ValueError(f"model already exists: {model_id}")
        created = ModelDefinition.model_validate(
            {**payload.model_dump(), "id": model_id, "builtin": False}
        )
        self._validate_activation(created)
        if created.is_default:
            self._clear_default()
        self._models[model_id] = created
        if created.api_key:
            self.secrets.put(
                self._secret_ref(model_id), {"api_key": created.api_key}
            )
        self._ensure_default(self._models)
        self.save()
        return self._models[model_id]

    def update(self, model_id: str, payload: ModelUpdateRequest) -> ModelDefinition:
        current = self._models.get(model_id)
        if current is None:
            raise KeyError(model_id)
        patch = payload.model_dump(exclude_unset=True)
        if "api_key" in patch and not str(patch["api_key"] or "").strip():
            patch.pop("api_key")
        updated = ModelDefinition.model_validate(
            {**current.model_dump(), **patch, "id": model_id}
        )
        self._validate_activation(updated)
        if patch.get("is_default"):
            self._clear_default(except_id=model_id)
        self._models[model_id] = updated
        if "api_key" in patch and str(patch.get("api_key") or "").strip():
            self.secrets.put(
                self._secret_ref(model_id), {"api_key": str(patch["api_key"])}
            )
        self._ensure_default(self._models)
        self.save()
        return self._models[model_id]

    def delete(self, model_id: str) -> None:
        current = self._models.get(model_id)
        if current is None:
            raise KeyError(model_id)
        del self._models[model_id]
        self.secrets.delete(self._secret_ref(model_id))
        self._ensure_default(self._models)
        self.save()

    def _clear_default(self, *, except_id: str | None = None) -> None:
        for model_id, model in self._models.items():
            if except_id is not None and model_id == except_id:
                continue
            if model.is_default:
                self._models[model_id] = model.model_copy(update={"is_default": False})

    def catalog_items(self) -> list[dict[str, Any]]:
        return [mask_model_definition(item) for item in self.list()]


def create_model_registry(path: Path, settings: Settings) -> ModelRegistry:
    return ModelRegistry(path.expanduser().resolve(), settings)
