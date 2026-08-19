"""Coordinator tool: retrieve published knowledge slices from 文枢."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ..knowledge_gateway import KnowledgeGateway, KnowledgeGatewayError
from .tools import ToolDefinition, ToolExecutionContext, ToolRegistry

_MAX_SNIPPET = 800


class SearchKnowledgeArguments(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    space_id: str = Field(default="", max_length=80)
    category_ids: list[str] = Field(default_factory=list, max_length=20)
    top_k: int = Field(default=5, ge=1, le=20)


def _snippet(text: str) -> str:
    value = " ".join(str(text or "").split())
    if len(value) <= _MAX_SNIPPET:
        return value
    return value[: _MAX_SNIPPET - 1] + "…"


def _citation(item: dict[str, Any], space_id: str) -> dict[str, Any]:
    page = item.get("page")
    if page is None:
        page = item.get("page_start")
    return {
        "knowledge_space_id": item.get("knowledge_space_id") or space_id,
        "document_id": str(item.get("document_id") or ""),
        "chunk_id": str(item.get("chunk_id") or ""),
        "title": str(item.get("title") or "未命名文档"),
        "page": page,
        "category_id": item.get("category_id") or None,
        "score": float(item.get("score") or 0.0),
        "text": _snippet(str(item.get("text") or "")),
    }


def _summary(items: list[dict[str, Any]]) -> str:
    if not items:
        return "知识库没有命中已发布切片。"
    parts = []
    for index, item in enumerate(items, start=1):
        page = f" 第 {item['page']} 页" if item.get("page") not in (None, "") else ""
        parts.append(f"{index}. 《{item['title']}》{page}")
    return "命中 " + "；".join(parts)


def register_search_knowledge_tool(
    registry: ToolRegistry,
    gateway: KnowledgeGateway,
    *,
    timeout_seconds: float = 45.0,
) -> None:
    def execute(
        arguments: SearchKnowledgeArguments,
        context: ToolExecutionContext,
    ) -> dict[str, Any]:
        if not gateway.configured:
            return {
                "ok": False,
                "configured": False,
                "items": [],
                "summary": "尚未连接文枢知识库，无法检索文档。",
            }
        try:
            spaces = gateway.list_spaces(context.tenant_id)
        except KnowledgeGatewayError as exc:
            return {
                "ok": False,
                "configured": True,
                "items": [],
                "summary": f"知识库不可用：{exc.detail}",
            }
        space_ids = [str(item.get("id") or "") for item in spaces if item.get("id")]
        requested = arguments.space_id.strip()
        if requested:
            if requested not in space_ids:
                return {
                    "ok": False,
                    "configured": True,
                    "items": [],
                    "summary": "指定的知识空间不存在，或不属于当前租户。",
                }
            space_ids = [requested]
        if not space_ids:
            return {
                "ok": True,
                "configured": True,
                "items": [],
                "summary": "当前租户没有可检索的知识空间。",
            }

        hits: list[dict[str, Any]] = []
        errors: list[str] = []
        per_space = max(arguments.top_k, 1)
        for space_id in space_ids:
            try:
                result = gateway.search_space(
                    context.tenant_id,
                    space_id,
                    query=arguments.query,
                    top_k=per_space,
                    category_ids=arguments.category_ids or None,
                )
            except KnowledgeGatewayError as exc:
                errors.append(f"{space_id}: {exc.detail}")
                continue
            for item in result.get("items") or []:
                citation = _citation(item if isinstance(item, dict) else {}, space_id)
                if citation["text"]:
                    hits.append(citation)
        hits.sort(key=lambda item: item["score"], reverse=True)
        selected = hits[: arguments.top_k]
        summary = _summary(selected)
        if errors and not selected:
            summary = "知识检索失败：" + "；".join(errors[:3])
        return {
            "ok": True,
            "configured": True,
            "query": arguments.query,
            "tenant_id": context.tenant_id,
            "items": selected,
            "summary": summary,
        }

    registry.register(
        ToolDefinition(
            name="search_knowledge",
            description=(
                "需要引用已发布制度、手册、故障码、SOP，或解释运营/平台政策术语时调用"
                "（包括用户只问「VAT 是什么意思」这类定义）。"
                "寒暄、与文档无关的百科、以及上文切片已够用时不要调用。"
                "query 写成独立完整的检索句，不要原样丢用户短追问。"
                "返回标题、页码和正文摘录。不要用它查个人记忆或业务数据库。"
                "可选 space_id、category_ids、top_k。"
            ),
            arguments_model=SearchKnowledgeArguments,
            handler=execute,
            risk="low",
            requires_approval=False,
            timeout_seconds=max(5.0, timeout_seconds),
            concurrency_safe=True,
            source="knowledge",
            builtin=True,
        )
    )
