from __future__ import annotations

from typing import Any


class QdrantVectorClient:
    def __init__(self, url: str, api_key: str = "", timeout_seconds: float = 10.0) -> None:
        from qdrant_client import QdrantClient

        self.client = QdrantClient(
            url=url,
            api_key=api_key or None,
            timeout=timeout_seconds,
        )

    def check_collection(self, collection_name: str) -> dict[str, Any]:
        exists = bool(self.client.collection_exists(collection_name))
        result: dict[str, Any] = {
            "backend": "qdrant",
            "collection": collection_name,
            "exists": exists,
        }
        if exists:
            info = self.client.get_collection(collection_name)
            result["points_count"] = getattr(info, "points_count", None)
            result["status"] = str(getattr(info, "status", "ready"))
        return result

    def search(
        self,
        collection_name: str,
        vector: list[float],
        *,
        limit: int,
        query_filter: Any = None,
        vector_name: str | None = None,
    ) -> list[Any]:
        options: dict[str, Any] = {
            "collection_name": collection_name,
            "query": vector,
            "query_filter": query_filter,
            "limit": limit,
            "with_payload": True,
        }
        if vector_name:
            options["using"] = vector_name
        response = self.client.query_points(
            **options
        )
        return list(response.points)

    def list_contents(
        self,
        collection_name: str,
        *,
        limit: int,
        cursor: str | int | None,
        text_field: str,
        category_field: str,
    ) -> dict[str, Any]:
        offset: str | int | None = cursor
        if isinstance(offset, str) and offset.isdigit():
            offset = int(offset)
        points, next_offset = self.client.scroll(
            collection_name=collection_name,
            limit=limit,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        total = self.client.count(
            collection_name=collection_name, exact=True
        ).count
        return {
            "items": [
                _content_item(
                    point.id,
                    dict(point.payload or {}),
                    text_field,
                    category_field,
                )
                for point in points
            ],
            "next_cursor": str(next_offset) if next_offset is not None else None,
            "total": int(total),
        }


class MilvusVectorClient:
    def __init__(
        self,
        uri: str,
        token: str = "",
        db_name: str = "default",
        timeout_seconds: float = 10.0,
    ) -> None:
        try:
            from pymilvus import MilvusClient
        except ImportError as exc:  # pragma: no cover - deployment dependency
            raise RuntimeError(
                "Milvus 连接需要安装 pymilvus，请重新安装项目依赖"
            ) from exc

        options: dict[str, Any] = {
            "uri": uri,
            "db_name": db_name or "default",
            "timeout": timeout_seconds,
        }
        if token:
            options["token"] = token
        self.client = MilvusClient(**options)

    def check_collection(self, collection_name: str) -> dict[str, Any]:
        exists = bool(self.client.has_collection(collection_name=collection_name))
        result: dict[str, Any] = {
            "backend": "milvus",
            "collection": collection_name,
            "exists": exists,
        }
        if exists:
            description = self.client.describe_collection(
                collection_name=collection_name
            )
            result["collection_id"] = description.get("collection_id")
            result["fields"] = [
                item.get("name") for item in description.get("fields", [])
            ]
        return result

    def search(
        self,
        collection_name: str,
        vector: list[float],
        *,
        limit: int,
        filter_expression: str = "",
        vector_field: str | None = None,
        output_fields: list[str] | None = None,
    ) -> list[Any]:
        options: dict[str, Any] = {
            "collection_name": collection_name,
            "data": [vector],
            "limit": limit,
            "output_fields": output_fields or ["*"],
        }
        if filter_expression:
            options["filter"] = filter_expression
        if vector_field:
            options["anns_field"] = vector_field
        return list(self.client.search(**options))

    def list_contents(
        self,
        collection_name: str,
        *,
        limit: int,
        cursor: str | int | None,
        text_field: str,
        category_field: str,
    ) -> dict[str, Any]:
        description = self.client.describe_collection(
            collection_name=collection_name
        )
        fields = list(description.get("fields") or [])
        primary = next(
            (str(item.get("name")) for item in fields if item.get("is_primary")),
            "id",
        )
        vector_fields = {
            str(item.get("name"))
            for item in fields
            if "VECTOR" in str(item.get("type") or "")
            or int(item.get("type") or 0) in {100, 101, 102, 103}
        }
        output_fields = [
            str(item.get("name"))
            for item in fields
            if str(item.get("name") or "") not in vector_fields
        ]
        offset = max(int(cursor or 0), 0)
        rows = self.client.query(
            collection_name=collection_name,
            filter="",
            output_fields=output_fields or ["*"],
            limit=limit,
            offset=offset,
        )
        stats = self.client.get_collection_stats(collection_name=collection_name)
        total = int(stats.get("row_count") or stats.get("rowCount") or len(rows))
        next_offset = offset + len(rows) if offset + len(rows) < total else None
        return {
            "items": [
                _content_item(
                    row.get(primary),
                    dict(row),
                    text_field,
                    category_field,
                    excluded={primary},
                )
                for row in rows
            ],
            "next_cursor": str(next_offset) if next_offset is not None else None,
            "total": total,
        }


def _content_item(
    point_id: Any,
    payload: dict[str, Any],
    text_field: str,
    category_field: str,
    *,
    excluded: set[str] | None = None,
) -> dict[str, Any]:
    hidden = {text_field, category_field, *(excluded or set())}
    content = payload.get(text_field)
    category = payload.get(category_field)
    return {
        "id": str(point_id),
        "content": str(content or ""),
        "category": str(category or "未分类"),
        "metadata": {
            str(key): value
            for key, value in payload.items()
            if key not in hidden and _safe_metadata_value(value)
        },
    }


def _safe_metadata_value(value: Any) -> bool:
    if value is None or isinstance(value, (str, int, float, bool)):
        return True
    if isinstance(value, list):
        return len(value) <= 50 and all(
            item is None or isinstance(item, (str, int, float, bool))
            for item in value
        )
    return False
