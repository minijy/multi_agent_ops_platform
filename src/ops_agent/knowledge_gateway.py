"""Server-side client and proxy routes for the 文枢 knowledge-management API."""

from __future__ import annotations

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
    def __init__(self, base_url: str = "", token: str = "", timeout_seconds: float = 60.0) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.token = (token or "").strip()
        self.timeout_seconds = timeout_seconds

    @classmethod
    def from_settings(cls, settings: Settings) -> "KnowledgeGateway":
        return cls(settings.knowledge_api_url, settings.knowledge_api_token)

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.token)

    def status(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "base_url": self.base_url,
            "backend": "wenshu" if self.configured else "unconfigured",
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
                    "message": "尚未配置文枢知识库 API。",
                    "hint": "在运营平台 .env 中设置 KNOWLEDGE_API_URL 和 KNOWLEDGE_API_TOKEN，并与文枢的 KNOWLEDGE_API_TOKEN 一致。",
                },
            )
        headers = {
            "Accept": "application/json",
            "X-Knowledge-Token": self.token,
            "X-Tenant-ID": tenant_id,
        }
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
                    "message": "无法连接文枢知识库服务。",
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
        payload = self.request("GET", "/v1/spaces", tenant_id=tenant_id) or {}
        return list(payload.get("items") or [])

    def search_space(
        self,
        tenant_id: str,
        space_id: str,
        *,
        query: str,
        top_k: int = 5,
        category_ids: list[str] | None = None,
    ) -> dict[str, Any]:
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
        return _call(_gateway(request), "GET", "/v1/spaces", principal.tenant_id)

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
        return _call(
            _gateway(request),
            "POST",
            f"/v1/spaces/{space_id}/search",
            principal.tenant_id,
            json=payload,
        )

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
