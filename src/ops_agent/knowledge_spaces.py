from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from pydantic import BaseModel, Field

from .connections import ConnectionRegistry


class KnowledgeSpaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    connection_id: str = Field(min_length=1, max_length=200)
    collection_name: str = Field(min_length=1, max_length=255)
    embedding_model: str = Field(min_length=1, max_length=255)
    vector_dimension: int = Field(default=384, ge=1, le=65536)
    top_k: int = Field(default=5, ge=1, le=100)
    vector_field: str = Field(default="", max_length=128)
    text_field: str = Field(default="text", min_length=1, max_length=128)
    category_field: str = Field(default="category", min_length=1, max_length=128)
    tenant_field: str = Field(default="tenant_id", max_length=128)
    knowledge_base_field: str = Field(default="knowledge_base_id", max_length=128)
    knowledge_base_id: str = Field(default="default", max_length=160)
    enabled: bool = True


class KnowledgeSpaceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    connection_id: str | None = Field(default=None, min_length=1, max_length=200)
    collection_name: str | None = Field(default=None, min_length=1, max_length=255)
    embedding_model: str | None = Field(default=None, min_length=1, max_length=255)
    vector_dimension: int | None = Field(default=None, ge=1, le=65536)
    top_k: int | None = Field(default=None, ge=1, le=100)
    vector_field: str | None = Field(default=None, max_length=128)
    text_field: str | None = Field(default=None, min_length=1, max_length=128)
    category_field: str | None = Field(default=None, min_length=1, max_length=128)
    tenant_field: str | None = Field(default=None, max_length=128)
    knowledge_base_field: str | None = Field(default=None, max_length=128)
    knowledge_base_id: str | None = Field(default=None, max_length=160)
    enabled: bool | None = None


class KnowledgeSpace(KnowledgeSpaceCreate):
    id: str
    tenant_id: str
    created_at: str
    updated_at: str


class KnowledgeSpaceRegistry:
    def __init__(self, path: Path, connections: ConnectionRegistry) -> None:
        self.path = path.expanduser().resolve()
        self.connections = connections
        self._lock = threading.RLock()
        self._items: dict[str, KnowledgeSpace] = {}
        self.reload()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def reload(self) -> None:
        with self._lock:
            if not self.path.is_file():
                self._items = {}
                return
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, TypeError):
                raw = []
            self._items = {
                item.id: item
                for value in (raw if isinstance(raw, list) else [])
                if isinstance(value, dict)
                for item in [KnowledgeSpace.model_validate(value)]
            }

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                [item.model_dump(mode="json") for item in self._items.values()],
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def _validate_connection(self, tenant_id: str, connection_id: str) -> None:
        connection = self.connections.require_id(connection_id, tenant_id)
        if connection.connector_type not in {"qdrant", "milvus"}:
            raise ValueError("知识空间只能选择 Qdrant 或 Milvus 连接")

    def list(self, tenant_id: str) -> list[KnowledgeSpace]:
        return sorted(
            (item for item in self._items.values() if item.tenant_id == tenant_id),
            key=lambda item: (item.name, item.id),
        )

    def get(self, tenant_id: str, space_id: str) -> KnowledgeSpace | None:
        item = self._items.get(space_id)
        return item if item and item.tenant_id == tenant_id else None

    def create(self, tenant_id: str, request: KnowledgeSpaceCreate) -> KnowledgeSpace:
        self._validate_connection(tenant_id, request.connection_id)
        now = self._now()
        item = KnowledgeSpace(
            **request.model_dump(),
            id=f"{tenant_id}:knowledge:{uuid.uuid4().hex}",
            tenant_id=tenant_id,
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            self._items[item.id] = item
            self._save()
        return item

    def update(
        self, tenant_id: str, space_id: str, request: KnowledgeSpaceUpdate
    ) -> KnowledgeSpace:
        current = self.get(tenant_id, space_id)
        if current is None:
            raise KeyError("knowledge space not found")
        changes = request.model_dump(exclude_none=True)
        connection_id = str(changes.get("connection_id") or current.connection_id)
        self._validate_connection(tenant_id, connection_id)
        updated = KnowledgeSpace.model_validate(
            {**current.model_dump(), **changes, "updated_at": self._now()}
        )
        with self._lock:
            self._items[space_id] = updated
            self._save()
        return updated

    def delete(self, tenant_id: str, space_id: str) -> KnowledgeSpace:
        item = self.get(tenant_id, space_id)
        if item is None:
            raise KeyError("knowledge space not found")
        with self._lock:
            self._items.pop(space_id, None)
            self._save()
        return item

    def spaces_for_connection(self, tenant_id: str, connection_id: str) -> list[str]:
        return [
            item.id
            for item in self.list(tenant_id)
            if item.connection_id == connection_id
        ]


def create_knowledge_space_registry(
    path: Path, connections: ConnectionRegistry
) -> KnowledgeSpaceRegistry:
    return KnowledgeSpaceRegistry(path, connections)
