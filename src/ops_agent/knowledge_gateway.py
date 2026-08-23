"""Server-side client and proxy routes for external knowledge services."""

from __future__ import annotations

import json as jsonlib
from typing import Any, Callable

import httpx
from fastapi import APIRouter, File, Form, Header, HTTPException, Request, UploadFile

from .config import Settings


class KnowledgeGatewayError(Exception):
    def __init__(self, status_code: int, detail: Any) -> None:
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


class KnowledgeGateway:
    GRAPH_SPACE_ID = "ecommerce-graphrag"

    def __init__(
        self,
        base_url: str = "",
        token: str = "",
        timeout_seconds: float = 60.0,
        backend: str = "ecommerce_graphrag",
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.token = (token or "").strip()
        self.timeout_seconds = timeout_seconds
        self.backend = backend.strip().lower() or "ecommerce_graphrag"
        if self.backend not in {"ecommerce_graphrag", "wenshu"}:
            raise ValueError(f"Unsupported knowledge API backend: {backend}")

    @classmethod
    def from_settings(cls, settings: Settings) -> "KnowledgeGateway":
        return cls(
            settings.knowledge_api_url,
            settings.knowledge_api_token,
            backend=settings.knowledge_api_backend,
        )

    @property
    def configured(self) -> bool:
        if self.backend == "wenshu":
            return bool(self.base_url and self.token)
        return bool(self.base_url)

    def status(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "base_url": self.base_url,
            "backend": self.backend if self.configured else "unconfigured",
        }

    def request(
        self,
        method: str,
        path: str,
        *,
        tenant_id: str,
        json: Any = None,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        files: Any = None,
        timeout: float | None = None,
    ) -> Any:
        if not self.configured:
            raise KnowledgeGatewayError(
                503,
                {
                    "code": "knowledge_api_not_configured",
                    "message": "尚未配置知识检索 API。",
                    "hint": "在运营平台 .env 中设置 KNOWLEDGE_API_BACKEND 和 KNOWLEDGE_API_URL。",
                },
            )
        headers = {"Accept": "application/json", "X-Tenant-ID": tenant_id}
        if self.token:
            if self.backend == "wenshu":
                headers["X-Knowledge-Token"] = self.token
            else:
                headers["Authorization"] = f"Bearer {self.token}"
        url = f"{self.base_url}{path}"
        try:
            response = httpx.request(
                method,
                url,
                headers=headers,
                json=json,
                params=params,
                data=data,
                files=files,
                timeout=timeout or self.timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise KnowledgeGatewayError(
                502,
                {
                    "code": "knowledge_api_unreachable",
                    "message": "无法连接知识检索服务。",
                    "hint": str(exc),
                },
            ) from exc
        if response.status_code >= 400:
            payload: Any
            try:
                payload = response.json()
            except ValueError:
                payload = {"error": response.text[:400]}
            detail = payload.get("error") or payload.get("detail") or payload
            raise KnowledgeGatewayError(response.status_code, detail)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    def list_spaces(self, tenant_id: str) -> list[dict[str, Any]]:
        if self.backend == "ecommerce_graphrag":
            if not self.configured:
                self.request("GET", "/health", tenant_id=tenant_id)
            return [
                {
                    "id": self.GRAPH_SPACE_ID,
                    "name": "电商 GraphRAG 知识库",
                    "tenant_id": tenant_id,
                    "backend": self.backend,
                }
            ]
        payload = self.request("GET", "/v1/spaces", tenant_id=tenant_id) or {}
        return list(payload.get("items") or [])

    @staticmethod
    def _evidence_text(source: str, data: dict[str, Any]) -> str:
        for key in ("content", "text", "answer", "summary", "description"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        if source in {"postgresql", "neo4j", "rule_engine"} and data:
            return jsonlib.dumps(data, ensure_ascii=False, default=str)
        return ""

    @classmethod
    def _normalize_graphrag_result(
        cls,
        payload: dict[str, Any],
        *,
        top_k: int,
    ) -> dict[str, Any]:
        citation_ids = {str(value) for value in payload.get("citation_ids") or []}
        items: list[dict[str, Any]] = []
        for evidence in payload.get("evidence") or []:
            if not isinstance(evidence, dict):
                continue
            evidence_id = str(evidence.get("evidence_id") or "")
            if citation_ids and evidence_id not in citation_ids:
                continue
            if evidence.get("trusted_for_generation") is False:
                continue
            data = evidence.get("data") if isinstance(evidence.get("data"), dict) else {}
            source = str(evidence.get("source") or "graphrag")
            text = cls._evidence_text(source, data)
            if not text:
                continue
            priority = float(evidence.get("priority") or 0.0)
            document_id = (
                data.get("document_id")
                or data.get("source_id")
                or data.get("path")
                or evidence_id
            )
            chunk_id = data.get("chunk_id") or data.get("id") or evidence_id
            items.append(
                {
                    "knowledge_space_id": cls.GRAPH_SPACE_ID,
                    "document_id": str(document_id),
                    "chunk_id": str(chunk_id),
                    "evidence_id": evidence_id,
                    "source": source,
                    "authority": evidence.get("authority"),
                    "title": str(evidence.get("title") or data.get("title") or "GraphRAG 证据"),
                    "page": data.get("page") or data.get("page_start"),
                    "category_id": data.get("topic") or data.get("category_id"),
                    # Unified retrieval priorities are comparable across heterogeneous
                    # evidence sources; raw OpenSearch scores are not.
                    "score": max(0.0, min(priority / 100.0, 1.0)),
                    "retrieval_score": data.get("retrieval_score") or data.get("score"),
                    "text": text,
                }
            )
        items.sort(key=lambda item: item["score"], reverse=True)
        return {
            "knowledge_space_id": cls.GRAPH_SPACE_ID,
            "query_id": payload.get("query_id"),
            "status": payload.get("status"),
            "query_expansion": payload.get("query_expansion") or {},
            "warnings": payload.get("warnings") or [],
            "items": items[:top_k],
        }

    def search_space(
        self,
        tenant_id: str,
        space_id: str,
        *,
        query: str,
        top_k: int = 5,
        category_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        if self.backend == "ecommerce_graphrag":
            if space_id and space_id != self.GRAPH_SPACE_ID:
                raise KnowledgeGatewayError(
                    404,
                    {"code": "knowledge_space_not_found", "message": "指定的 GraphRAG 知识空间不存在。"},
                )
            payload = self.request(
                "POST",
                "/v1/retrieve",
                tenant_id=tenant_id,
                json={"query": query, "include_debug": True},
            )
            if not isinstance(payload, dict):
                return {"items": []}
            result = self._normalize_graphrag_result(payload, top_k=top_k)
            if category_ids:
                result["warnings"] = [
                    *result.get("warnings", []),
                    "GraphRAG 统一检索暂不支持 category_ids 过滤。",
                ]
            return result
        payload = self.request(
            "POST",
            f"/v1/spaces/{space_id}/search",
            tenant_id=tenant_id,
            json={
                "query": query,
                "top_k": top_k,
                "category_ids": category_ids or [],
            },
        )
        return payload if isinstance(payload, dict) else {"items": []}


def _raise(exc: KnowledgeGatewayError) -> None:
    raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


def register_knowledge_library_routes(
    application,
    principal_from_headers: Callable[..., Any],
) -> None:
    router = APIRouter(prefix="/v1/knowledge/library", tags=["knowledge-library"])

    def _principal(
        request: Request,
        x_api_key: str | None,
        x_tenant_id: str | None,
        x_user_id: str | None,
        x_user_role: str | None,
    ):
        return principal_from_headers(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role, {"admin"}
        )

    def _gateway(request: Request) -> KnowledgeGateway:
        return request.app.state.knowledge_gateway

    def _call(gateway: KnowledgeGateway, method: str, path: str, tenant_id: str, **kwargs: Any):
        try:
            return gateway.request(method, path, tenant_id=tenant_id, **kwargs)
        except KnowledgeGatewayError as exc:
            _raise(exc)

    @router.get("/status")
    def library_status(
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _principal(request, x_api_key, x_tenant_id, x_user_id, x_user_role)
        return _gateway(request).status()

    @router.get("/spaces")
    def list_spaces(
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> Any:
        principal = _principal(request, x_api_key, x_tenant_id, x_user_id, x_user_role)
        try:
            items = _gateway(request).list_spaces(principal.tenant_id)
        except KnowledgeGatewayError as exc:
            _raise(exc)
        return {"items": items, "count": len(items)}

    @router.get("/catalog")
    def library_catalog(
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> Any:
        principal = _principal(request, x_api_key, x_tenant_id, x_user_id, x_user_role)
        return _call(_gateway(request), "GET", "/v1/catalog", principal.tenant_id)

    @router.post("/spaces")
    def create_space(
        payload: dict[str, Any],
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> Any:
        principal = _principal(request, x_api_key, x_tenant_id, x_user_id, x_user_role)
        return _call(
            _gateway(request),
            "POST",
            "/v1/spaces",
            principal.tenant_id,
            json=payload,
        )

    @router.get("/spaces/{space_id}")
    def get_space(
        space_id: str,
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> Any:
        principal = _principal(request, x_api_key, x_tenant_id, x_user_id, x_user_role)
        return _call(_gateway(request), "GET", f"/v1/spaces/{space_id}", principal.tenant_id)

    @router.get("/spaces/{space_id}/categories")
    def list_categories(
        space_id: str,
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> Any:
        principal = _principal(request, x_api_key, x_tenant_id, x_user_id, x_user_role)
        return _call(
            _gateway(request),
            "GET",
            f"/v1/spaces/{space_id}/categories",
            principal.tenant_id,
        )

    @router.post("/spaces/{space_id}/categories")
    def create_category(
        space_id: str,
        payload: dict[str, Any],
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> Any:
        principal = _principal(request, x_api_key, x_tenant_id, x_user_id, x_user_role)
        return _call(
            _gateway(request),
            "POST",
            f"/v1/spaces/{space_id}/categories",
            principal.tenant_id,
            json=payload,
        )

    @router.patch("/categories/{category_id}")
    def update_category(
        category_id: str,
        payload: dict[str, Any],
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> Any:
        principal = _principal(request, x_api_key, x_tenant_id, x_user_id, x_user_role)
        return _call(
            _gateway(request),
            "PATCH",
            f"/v1/categories/{category_id}",
            principal.tenant_id,
            json=payload,
        )

    @router.delete("/categories/{category_id}")
    def delete_category(
        category_id: str,
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> Any:
        principal = _principal(request, x_api_key, x_tenant_id, x_user_id, x_user_role)
        return _call(
            _gateway(request),
            "DELETE",
            f"/v1/categories/{category_id}",
            principal.tenant_id,
        )

    @router.get("/spaces/{space_id}/documents")
    def list_documents(
        space_id: str,
        request: Request,
        category_id: str = "",
        limit: int = 20,
        offset: int = 0,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> Any:
        principal = _principal(request, x_api_key, x_tenant_id, x_user_id, x_user_role)
        params: dict[str, Any] = {
            "limit": max(1, min(int(limit), 100)),
            "offset": max(0, int(offset)),
        }
        if category_id:
            params["category_id"] = category_id
        return _call(
            _gateway(request),
            "GET",
            f"/v1/spaces/{space_id}/documents",
            principal.tenant_id,
            params=params,
        )

    @router.post("/spaces/{space_id}/documents")
    async def upload_document(
        space_id: str,
        request: Request,
        file: UploadFile = File(...),
        title: str = Form(""),
        document_type: str = Form("manual"),
        tags: str = Form(""),
        category_id: str = Form(""),
        duplicate_policy: str = Form("skip"),
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> Any:
        principal = _principal(request, x_api_key, x_tenant_id, x_user_id, x_user_role)
        content = await file.read()
        files = {
            "file": (file.filename or "document", content, file.content_type or "application/octet-stream")
        }
        data = {
            "title": title,
            "document_type": document_type,
            "tags": tags,
            "category_id": category_id,
            "duplicate_policy": duplicate_policy,
        }
        return _call(
            _gateway(request),
            "POST",
            f"/v1/spaces/{space_id}/documents",
            principal.tenant_id,
            data=data,
            files=files,
            timeout=120.0,
        )

    @router.get("/documents/{document_id}")
    def get_document(
        document_id: str,
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> Any:
        principal = _principal(request, x_api_key, x_tenant_id, x_user_id, x_user_role)
        return _call(
            _gateway(request), "GET", f"/v1/documents/{document_id}", principal.tenant_id
        )

    @router.get("/documents/{document_id}/chunks")
    def list_chunks(
        document_id: str,
        request: Request,
        limit: int = 50,
        offset: int = 0,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> Any:
        principal = _principal(request, x_api_key, x_tenant_id, x_user_id, x_user_role)
        return _call(
            _gateway(request),
            "GET",
            f"/v1/documents/{document_id}/chunks",
            principal.tenant_id,
            params={"limit": limit, "offset": offset},
        )

    @router.get("/documents/{document_id}/jobs")
    def list_jobs(
        document_id: str,
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> Any:
        principal = _principal(request, x_api_key, x_tenant_id, x_user_id, x_user_role)
        return _call(
            _gateway(request),
            "GET",
            f"/v1/documents/{document_id}/jobs",
            principal.tenant_id,
        )

    @router.post("/documents/{document_id}/reparse")
    def reparse_document(
        document_id: str,
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> Any:
        principal = _principal(request, x_api_key, x_tenant_id, x_user_id, x_user_role)
        return _call(
            _gateway(request),
            "POST",
            f"/v1/documents/{document_id}/reparse",
            principal.tenant_id,
        )

    @router.post("/documents/{document_id}/reindex")
    def reindex_document(
        document_id: str,
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> Any:
        principal = _principal(request, x_api_key, x_tenant_id, x_user_id, x_user_role)
        return _call(
            _gateway(request),
            "POST",
            f"/v1/documents/{document_id}/reindex",
            principal.tenant_id,
        )

    @router.delete("/documents/{document_id}")
    def delete_document(
        document_id: str,
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> Any:
        principal = _principal(request, x_api_key, x_tenant_id, x_user_id, x_user_role)
        return _call(
            _gateway(request),
            "DELETE",
            f"/v1/documents/{document_id}",
            principal.tenant_id,
        )

    @router.post("/spaces/{space_id}/search")
    def search_space(
        space_id: str,
        payload: dict[str, Any],
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> Any:
        principal = _principal(request, x_api_key, x_tenant_id, x_user_id, x_user_role)
        try:
            return _gateway(request).search_space(
                principal.tenant_id,
                space_id,
                query=str(payload.get("query") or ""),
                top_k=max(1, min(int(payload.get("top_k") or 5), 20)),
                category_ids=list(payload.get("category_ids") or []),
            )
        except KnowledgeGatewayError as exc:
            _raise(exc)

    @router.post("/spaces/{space_id}/reindex")
    def reindex_space(
        space_id: str,
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> Any:
        principal = _principal(request, x_api_key, x_tenant_id, x_user_id, x_user_role)
        return _call(
            _gateway(request),
            "POST",
            f"/v1/spaces/{space_id}/reindex",
            principal.tenant_id,
        )

    application.include_router(router)
