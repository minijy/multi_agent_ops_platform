from __future__ import annotations

from typing import Any

from ..config import Settings
from ..workflows.amazon_finance.domain import AmazonFinanceQueryPlan
from ..workflows.amazon_finance.query_tool import AmazonFinanceQueryTool
from .tools import ToolDefinition, ToolExecutionContext, ToolRegistry
from .connectors import ConnectorRuntime
from ..query_policy import enforce_query_plan
from ..source_privacy import AMAZON_FINANCE_SOURCE


def _summary(plan: AmazonFinanceQueryPlan, rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "指定条件下没有 RELEASED 结算交易。"
    if plan.metric == "overview":
        if len(rows) > 1:
            currencies = "、".join(
                str(row.get("currency_code") or "未知币种") for row in rows
            )
            return f"按币种返回 {len(rows)} 组 RELEASED 交易汇总：{currencies}。"
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
    connectors: ConnectorRuntime,
) -> None:
    def execute(
        plan: AmazonFinanceQueryPlan,
        context: ToolExecutionContext,
    ) -> dict[str, Any]:
        resolved = enforce_query_plan("amazon_finance_query", plan, context.role)

        def query(client, connection):
            if connection.tenant_id != context.tenant_id:
                raise PermissionError("connector tenant does not match principal")
            marketplaces = connectors.scoped_tool_resources(
                connection,
                "amazon_finance_query",
                "marketplace_ids",
                context.resource_scope,
            )
            query_tool = AmazonFinanceQueryTool(
                client["dsn"],
                statement_timeout_ms=settings.analytics_statement_timeout_ms,
                engine=client.get("engine", "postgresql"),
            )
            return query_tool.execute(
                resolved,
                tenant_id=context.tenant_id,
                marketplace_ids=tuple(marketplaces),
            )

        rows = connectors.execute_tool(
            context.tenant_id, "amazon_finance_query", query
        )
        return {
            "plan": resolved.model_dump(mode="json"),
            "columns": list(rows[0].keys()) if rows else [],
            "rows": rows,
            "summary": _summary(resolved, rows),
            "data_scope": AMAZON_FINANCE_SOURCE,
            "data_source": AMAZON_FINANCE_SOURCE,
            "calculation": {
                "engine": connectors.connection_for_tool(
                    context.tenant_id, "amazon_finance_query"
                ).config.get("database_type", "postgresql"),
                "operation": "parameterized aggregate query",
                "metric": resolved.metric,
                "grouped_by": [] if resolved.metric == "overview" else [resolved.metric],
            },
        }

    registry.register(
        ToolDefinition(
            name="amazon_finance_query",
            description=(
                "查询 Amazon RELEASED 结算数据。可查询总体概览、每日趋势、"
                "交易类型、费用、SKU 或结算批次。租户与站点范围由系统注入，不能由模型指定。"
                "明细指标必须带日期；窗口最长 366 天。若会话里已有同类查询结果，优先复用。"
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
