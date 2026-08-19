from pathlib import Path

from fastapi.testclient import TestClient

from ops_agent.api.app import create_app
from tests.test_api import _settings


def test_knowledge_library_reports_unconfigured(tmp_path: Path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        status = client.get("/v1/knowledge/library/status")
        assert status.status_code == 200
        assert status.json()["configured"] is False
        listed = client.get("/v1/knowledge/library/spaces")
        assert listed.status_code == 503
        assert listed.json()["detail"]["code"] == "knowledge_api_not_configured"


def test_knowledge_library_proxies_wenshu_search(tmp_path: Path):
    settings = _settings(
        tmp_path,
        knowledge_api_url="http://127.0.0.1:8000",
        knowledge_api_token="test-service-token",
    )
    captured: dict = {}

    with TestClient(create_app(settings)) as client:
        def fake_request(method, path, *, tenant_id, json=None, params=None, **_kwargs):
            captured.update(
                {
                    "method": method,
                    "path": path,
                    "tenant_id": tenant_id,
                    "json": json,
                    "params": params,
                }
            )
            if path == "/v1/catalog":
                return {
                    "embedding_models": [
                        {
                            "id": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
                            "label": "多语言 MiniLM",
                            "vector_size": 384,
                            "distance": "COSINE",
                        }
                    ],
                    "default_embedding_model": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
                }
            if method == "POST" and path == "/v1/spaces":
                return {"id": "kb-new", "name": (json or {}).get("name"), "document_count": 0}
            if path == "/v1/spaces":
                return {
                    "items": [
                        {
                            "id": "kb-1",
                            "name": "技术文档",
                            "tenant_id": tenant_id,
                            "document_count": 3,
                        }
                    ],
                    "count": 1,
                }
            if path.endswith("/search"):
                return {
                    "knowledge_space_id": "kb-1",
                    "tenant_id": tenant_id,
                    "query": (json or {}).get("query"),
                    "items": [
                        {
                            "title": "认证故障手册",
                            "text": "AUTH-1003 需要清理失效会话。",
                            "page": 4,
                            "score": 0.91,
                        }
                    ],
                }
            return {}

        client.app.state.knowledge_gateway.request = fake_request  # type: ignore[method-assign]
        listed = client.get("/v1/knowledge/library/spaces")
        assert listed.status_code == 200
        assert listed.json()["items"][0]["name"] == "技术文档"
        searched = client.post(
            "/v1/knowledge/library/spaces/kb-1/search",
            json={"query": "AUTH-1003", "top_k": 5},
        )
        assert searched.status_code == 200
        assert searched.json()["items"][0]["page"] == 4
        assert captured["method"] == "POST"
        assert captured["path"] == "/v1/spaces/kb-1/search"
        assert captured["tenant_id"] == "tenant-a"
        assert captured["json"]["query"] == "AUTH-1003"

        catalog = client.get("/v1/knowledge/library/catalog")
        assert catalog.status_code == 200
        created = client.post(
            "/v1/knowledge/library/spaces",
            json={"name": "售后手册", "embedding_model": catalog.json()["default_embedding_model"]},
        )
        assert created.status_code == 200
        assert created.json()["id"] == "kb-new"
        assert captured["path"] == "/v1/spaces"
        assert captured["json"]["name"] == "售后手册"
