from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import httpx


DEFAULT_TAVILY_BASE_URL = "https://api.tavily.com"


class TavilyError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def validate_tavily_base_url(value: Any) -> str:
    normalized = str(value or DEFAULT_TAVILY_BASE_URL).strip().rstrip("/")
    parsed = urlparse(normalized)
    if parsed.scheme != "https" or parsed.hostname != "api.tavily.com":
        raise ValueError("Tavily API Base URL 必须是 https://api.tavily.com")
    return normalized


class TavilyClient:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_TAVILY_BASE_URL,
        timeout_seconds: float = 20.0,
    ) -> None:
        key = str(api_key or "").strip()
        if not key:
            raise ValueError("Tavily API Key 未配置")
        self.api_key = key
        self.base_url = validate_tavily_base_url(base_url)
        self.timeout_seconds = timeout_seconds

    def search(
        self,
        query: str,
        *,
        max_results: int = 5,
        search_depth: str = "basic",
    ) -> dict[str, Any]:
        payload = {
            "api_key": self.api_key,
            "query": query,
            "max_results": max_results,
            "search_depth": search_depth,
            "include_answer": False,
        }
        try:
            response = httpx.post(
                f"{self.base_url}/search",
                json=payload,
                timeout=self.timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise TavilyError("Tavily 请求超时") from exc
        except httpx.HTTPError as exc:
            raise TavilyError(f"无法连接 Tavily：{exc}") from exc
        if response.status_code == 401:
            raise TavilyError("Tavily API Key 无效", status_code=401)
        if response.status_code == 429:
            raise TavilyError("Tavily 额度或速率已用尽", status_code=429)
        if response.status_code >= 400:
            detail = response.text.strip()[:300] or response.reason_phrase
            raise TavilyError(
                f"Tavily 返回 HTTP {response.status_code}：{detail}",
                status_code=response.status_code,
            )
        data = response.json()
        if not isinstance(data, dict):
            raise TavilyError("Tavily 返回了无法解析的结果")
        return data
