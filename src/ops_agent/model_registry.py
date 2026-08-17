from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from .config import Settings


ModelProvider = Literal["mock", "openai", "zhipu"]
SECRET_MASK = "********"


class ModelDefinition(BaseModel):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    name: str = Field(min_length=1, max_length=120)
    provider: ModelProvider
    model_name: str = Field(min_length=1, max_length=120)
    api_key: str = Field(default="", max_length=512)
    base_url: str = Field(default="", max_length=256)
    vision_model_name: str = Field(default="", max_length=120)
    enabled: bool = True
    is_default: bool = False
    temperature: float | None = Field(default=None, ge=0, le=2)
    builtin: bool = False

    @field_validator("id")
    @classmethod
    def normalize_id(cls, value: str) -> str:
        return value.strip().lower()


class ModelCreateRequest(BaseModel):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    name: str = Field(min_length=1, max_length=120)
    provider: ModelProvider
    model_name: str = Field(min_length=1, max_length=120)
    api_key: str = Field(default="", max_length=512)
    base_url: str = Field(default="", max_length=256)
    vision_model_name: str = Field(default="", max_length=120)
    enabled: bool = True
    is_default: bool = False
    temperature: float | None = Field(default=None, ge=0, le=2)


class ModelUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    provider: ModelProvider | None = None
    model_name: str | None = Field(default=None, min_length=1, max_length=120)
    api_key: str | None = Field(default=None, max_length=512)
    base_url: str | None = None
    vision_model_name: str | None = Field(default=None, max_length=120)
    enabled: bool | None = None
    is_default: bool | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)


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


def mask_model_definition(model: ModelDefinition) -> dict[str, Any]:
    payload = model.model_dump(mode="json")
    secret = str(payload.get("api_key") or "")
    payload["api_key"] = SECRET_MASK if secret else ""
    payload["api_key_configured"] = bool(secret)
    payload["supports_vision"] = bool(model.vision_model_name)
    return payload


class ModelRegistry:
    def __init__(self, path: Path, settings: Settings) -> None:
        self.path = path
        self.settings = settings
        self._models: dict[str, ModelDefinition] = {}
        self.reload()

    def reload(self) -> None:
        defaults = {item.id: item for item in default_models_from_settings(self.settings)}
        stored = self._read_file()
        merged: dict[str, ModelDefinition] = {}
        for model_id, default in defaults.items():
            override = stored.get(model_id, {})
            if override:
                merged[model_id] = default.model_copy(
                    update={
                        key: value
                        for key, value in override.items()
                        if key in ModelDefinition.model_fields and key != "id"
                    }
                )
            else:
                merged[model_id] = default
        for model_id, override in stored.items():
            if model_id in merged:
                continue
            try:
                merged[model_id] = ModelDefinition.model_validate(
                    {"id": model_id, **override}
                )
            except Exception:
                continue
        self._ensure_default(merged)
        self._models = merged
        if not self.path.is_file():
            self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            model_id: model.model_dump(mode="json")
            for model_id, model in self._models.items()
        }
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

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
            return model.id
        return self.default_model_id()

    def create(self, payload: ModelCreateRequest) -> ModelDefinition:
        model_id = payload.id.strip().lower()
        if model_id in self._models:
            raise ValueError(f"model already exists: {model_id}")
        created = ModelDefinition.model_validate(
            {**payload.model_dump(), "id": model_id, "builtin": False}
        )
        if created.is_default:
            self._clear_default()
        self._models[model_id] = created
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
        updated = current.model_copy(update=patch)
        if patch.get("is_default"):
            self._clear_default(except_id=model_id)
        self._models[model_id] = updated
        self._ensure_default(self._models)
        self.save()
        return self._models[model_id]

    def delete(self, model_id: str) -> None:
        current = self._models.get(model_id)
        if current is None:
            raise KeyError(model_id)
        if current.builtin and len(self._models) == 1:
            raise ValueError("cannot delete the only model")
        del self._models[model_id]
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
