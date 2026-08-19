from __future__ import annotations

from typing import Any

from ..config import Settings
from ..workflows.profit_report.domain import ProfitReportQueryPlan
from ..workflows.profit_report.query_tool import ProfitReportQueryTool
from .tools import ToolDefinition, ToolExecutionContext, ToolRegistry
from .connectors import ConnectorRuntime
from ..source_privacy import PROFIT_WAREHOUSE_SOURCE


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
    connectors: ConnectorRuntime,
) -> None:
    def execute(
        plan: ProfitReportQueryPlan,
        context: ToolExecutionContext,
    ) -> dict[str, Any]:
        def query(client, connection):
            store_name = connectors.resolve_tool_resource(
                connection,
                "profit_report_query",
                "store_names",
                plan.store_name,
                context.resource_scope,
            )
            resolved_plan = (
                plan.model_copy(update={"store_name": store_name})
                if store_name
                else plan
            )
            query_tool = ProfitReportQueryTool(
                client["dsn"],
                statement_timeout_ms=settings.analytics_statement_timeout_ms,
                engine=client.get("engine", "postgresql"),
            )
            rows, total = query_tool.execute(resolved_plan)
            return resolved_plan, rows, total

        resolved_plan, rows, total = connectors.execute_tool(
            context.tenant_id, "profit_report_query", query
        )
        return {
            "plan": resolved_plan.model_dump(mode="json"),
            "columns": list(rows[0].keys()) if rows else [],
            "rows": rows,
            "summary": _summary(resolved_plan, rows, total),
            "total_rows": total,
            "data_scope": PROFIT_WAREHOUSE_SOURCE,
            "data_source": PROFIT_WAREHOUSE_SOURCE,
            "calculation": {
                "engine": connectors.connection_for_tool(
                    context.tenant_id, "profit_report_query"
                ).config.get("database_type", "postgresql"),
                "operation": "parameterized aggregate query",
                "metric": resolved_plan.metric,
                "grouped_by": (
                    [] if resolved_plan.metric == "overview" else [resolved_plan.metric]
                ),
                "source_rows": total,
            },
        }

    registry.register(
        ToolDefinition(
            name="profit_report_query",
            description=(
                "查询数据库中已导入的领星利润报表（订单 transaction）。"
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
