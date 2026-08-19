"""Coordinator tool: public web search via the tenant Tavily connector."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from ..integrations.tavily.client import TavilyError
from .connectors import ConnectorRuntime
from .tools import ToolDefinition, ToolExecutionContext, ToolRegistry

_MAX_SNIPPET = 800


class WebSearchArguments(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    max_results: int = Field(default=5, ge=1, le=10)
    search_depth: Literal["basic", "advanced"] = "basic"


def _snippet(text: str) -> str:
    value = " ".join(str(text or "").split())
    if len(value) <= _MAX_SNIPPET:
        return value
    return value[: _MAX_SNIPPET - 1] + "…"


def _items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    for raw in payload.get("results") or []:
        if not isinstance(raw, dict):
            continue
        url = str(raw.get("url") or "").strip()
        title = str(raw.get("title") or "").strip() or url
        text = _snippet(str(raw.get("content") or raw.get("snippet") or ""))
        if not url or not text:
            continue
        hits.append(
            {
                "title": title,
                "url": url,
                "text": text,
                "score": float(raw.get("score") or 0.0),
            }
        )
    return hits


def _summary(items: list[dict[str, Any]]) -> str:
    if not items:
        return "网页搜索没有可用结果。"
    parts = [f"{index}. {item['title']}" for index, item in enumerate(items, start=1)]
    return "网页来源 " + "；".join(parts)


def register_web_search_tool(
    registry: ToolRegistry,
    connectors: ConnectorRuntime,
    *,
    timeout_seconds: float = 20.0,
) -> None:
    def execute(
        arguments: WebSearchArguments,
        context: ToolExecutionContext,
    ) -> dict[str, Any]:
        def operation(client, _connection):
            return client.search(
                arguments.query,
                max_results=arguments.max_results,
                search_depth=arguments.search_depth,
            )

        try:
            payload = connectors.execute_tool(
                context.tenant_id,
                "web_search",
                operation,
                retry_transient=True,
            )
        except PermissionError:
            return {
                "ok": False,
                "configured": False,
                "items": [],
                "summary": "尚未配置 Tavily。请在连接器页面添加 API Key，并在工具页把 web_search 绑到该连接。",
            }
        except TavilyError as exc:
            return {
                "ok": False,
                "configured": True,
                "items": [],
                "summary": str(exc),
            }
        except Exception as exc:
            return {
                "ok": False,
                "configured": True,
                "items": [],
                "summary": f"网页搜索失败：{type(exc).__name__}",
            }
        items = _items(payload if isinstance(payload, dict) else {})
        return {
            "ok": True,
            "configured": True,
            "query": arguments.query,
            "items": items,
            "summary": _summary(items),
        }

    registry.register(
        ToolDefinition(
            name="web_search",
            description=(
                "检索公开互联网（新闻、官网、公开政策、行业资料）。"
                "内部制度、手册、SOP 用 search_knowledge，不要用网页结果冒充公司文档。"
                "寒暄或上文已够用时不要调用。query 写成独立完整的检索句。"
                "回答必须带上来源标题和 URL。"
            ),
            arguments_model=WebSearchArguments,
            handler=execute,
            risk="low",
            requires_approval=False,
            timeout_seconds=max(5.0, timeout_seconds),
            concurrency_safe=True,
            source="tavily",
            builtin=True,
        )
    )
