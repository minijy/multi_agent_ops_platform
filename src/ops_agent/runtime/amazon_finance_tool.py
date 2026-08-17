from __future__ import annotations

from typing import Any

from ..config import Settings
from ..workflows.amazon_finance.domain import AmazonFinanceQueryPlan
from ..workflows.amazon_finance.query_tool import AmazonFinanceQueryTool
from .tools import ToolDefinition, ToolExecutionContext, ToolRegistry


def _summary(plan: AmazonFinanceQueryPlan, rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "指定条件下没有 RELEASED 结算交易。"
    if plan.metric == "overview":
        row = rows[0]
        return (
            f"共查询到 {row['transaction_count']} 笔 RELEASED 交易，"
            f"净额 {row['net_amount']} {row.get('currency_code') or ''}。"
        )
    labels = {
        "daily": "个记账日",
        "transaction_type": "种交易类型",
        "fee": "种费用类型",
        "sku": "个 SKU",
        "settlement": "个结算批次",
    }
    return f"返回 {len(rows)} {labels[plan.metric]}，数据口径仅包含 RELEASED。"


def register_amazon_finance_tool(
    registry: ToolRegistry,
    settings: Settings,
) -> None:
    query_tool = AmazonFinanceQueryTool(
        settings.analytics_dsn,
        statement_timeout_ms=settings.analytics_statement_timeout_ms,
    )

    def execute(
        plan: AmazonFinanceQueryPlan,
        context: ToolExecutionContext,
    ) -> dict[str, Any]:
        seller_id, rows = query_tool.execute(plan, seller_id=context.seller_id)
        return {
            "seller_id": seller_id,
            "plan": plan.model_dump(mode="json"),
            "columns": list(rows[0].keys()) if rows else [],
            "rows": rows,
            "summary": _summary(plan, rows),
            "data_scope": "RELEASED only",
        }

    registry.register(
        ToolDefinition(
            name="amazon_finance_query",
            description=(
                "查询 Amazon RELEASED 结算数据。可查询总体概览、每日趋势、"
                "交易类型、费用、SKU 或结算批次。日期为空时查询全部已导入数据。"
                "若会话里已有同类查询结果，优先复用，不要为改列名或改展示再查一次。"
            ),
            arguments_model=AmazonFinanceQueryPlan,
            handler=execute,
            risk="low",
            requires_approval=False,
            timeout_seconds=max(1.0, settings.analytics_statement_timeout_ms / 1000 + 1),
            concurrency_safe=True,
            builtin=True,
        )
    )
