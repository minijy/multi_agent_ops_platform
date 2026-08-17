from __future__ import annotations

from typing import Any

from ..config import Settings
from ..workflows.profit_report.domain import ProfitReportQueryPlan
from ..workflows.profit_report.query_tool import ProfitReportQueryTool
from .tools import ToolDefinition, ToolExecutionContext, ToolRegistry


def _summary(plan: ProfitReportQueryPlan, rows: list[dict[str, Any]], total: int) -> str:
    if not rows:
        return f"指定条件下没有利润报表记录（共 {total} 行原始数据）。"
    if plan.metric == "overview":
        row = rows[0]
        return (
            f"共 {row['row_count']} 行，结算毛利润 {row['gross_profit_total']} "
            f"{row.get('currency_code') or ''}，结算小计 {row['settlement_total']}。"
        )
    labels = {
        "daily": "个结算日",
        "store": "个店铺",
        "msku": "个 MSKU",
        "order": "个订单",
        "event_source": "种费用类型",
    }
    return f"返回 {len(rows)} {labels[plan.metric]}，原始数据共 {total} 行。"


def register_profit_report_tool(
    registry: ToolRegistry,
    settings: Settings,
) -> None:
    query_tool = ProfitReportQueryTool(
        settings.analytics_dsn,
        statement_timeout_ms=settings.analytics_statement_timeout_ms,
    )

    def execute(
        plan: ProfitReportQueryPlan,
        context: ToolExecutionContext,
    ) -> dict[str, Any]:
        if context.seller_id:
            plan = plan.model_copy(update={"store_name": context.seller_id})
        rows, total = query_tool.execute(plan)
        return {
            "plan": plan.model_dump(mode="json"),
            "columns": list(rows[0].keys()) if rows else [],
            "rows": rows,
            "summary": _summary(plan, rows, total),
            "total_rows": total,
            "data_scope": "lingxing_profit_order_transactions",
        }

    registry.register(
        ToolDefinition(
            name="profit_report_query",
            description=(
                "查询 PostgreSQL 中已导入的领星利润报表（订单 transaction）。"
                "支持总览、按结算日/店铺/MSKU/订单/费用类型汇总。"
                "可按 start_date、end_date、currency_code、store_name 过滤。"
                "start_date/end_date 必须在库内已有结算时间范围内；不确定时先用 metric=overview 查看时间范围。"
            ),
            arguments_model=ProfitReportQueryPlan,
            handler=execute,
            risk="low",
            requires_approval=False,
            timeout_seconds=max(1.0, settings.analytics_statement_timeout_ms / 1000 + 1),
            concurrency_safe=True,
            builtin=True,
        )
    )
