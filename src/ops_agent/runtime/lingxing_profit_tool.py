from __future__ import annotations

from typing import Any

from ..workflows.lingxing_profit.domain import LingXingProfitQueryPlan
from ..workflows.lingxing_profit.query_tool import LingXingProfitQueryTool
from .tools import ToolDefinition, ToolExecutionContext, ToolRegistry
from .connectors import ConnectorRuntime
from ..source_privacy import LINGXING_LIVE_SOURCE


def _summary(plan: LingXingProfitQueryPlan, rows: list[dict[str, Any]], total: int) -> str:
    currency = plan.currency_code or "原币种"
    if not rows:
        return (
            f"{plan.start_date} 至 {plan.end_date} 无利润报表记录"
            f"（{currency}，共 {total} 条）。"
        )
    gross = sum(float(row.get("settlementGrossProfit") or 0) for row in rows)
    return (
        f"返回 {len(rows)} / {total} 条订单维度利润记录，"
        f"区间 {plan.start_date} ~ {plan.end_date}，币种 {currency}，"
        f"当前页结算毛利润合计 {gross:.2f}。"
    )


def register_lingxing_profit_tool(
    registry: ToolRegistry,
    connectors: ConnectorRuntime,
    *,
    timeout_seconds: float = 30.0,
) -> None:
    query_tool = LingXingProfitQueryTool()

    def execute(
        plan: LingXingProfitQueryPlan,
        context: ToolExecutionContext,
    ) -> dict[str, Any]:
        def query(client, connection):
            resolved_plan = plan
            allowed_sids = connectors.scoped_tool_resources(
                connection,
                "lingxing_profit_query",
                "sids",
                context.resource_scope,
            )
            if allowed_sids and "*" not in allowed_sids:
                allowed = {int(item) for item in allowed_sids}
                if plan.sids and not set(plan.sids).issubset(allowed):
                    raise PermissionError(
                        "one or more LingXing sids are not authorized"
                    )
                if not plan.sids:
                    resolved_plan = plan.model_copy(update={"sids": sorted(allowed)})
            rows, total = query_tool.execute(client, resolved_plan)
            return resolved_plan, rows, total

        resolved_plan, rows, total = connectors.execute_tool(
            context.tenant_id, "lingxing_profit_query", query
        )
        return {
            "plan": resolved_plan.model_dump(mode="json"),
            "columns": list(rows[0].keys()) if rows else [],
            "rows": rows,
            "summary": _summary(resolved_plan, rows, total),
            "total": total,
            "data_scope": LINGXING_LIVE_SOURCE,
            "data_source": LINGXING_LIVE_SOURCE,
            "calculation": {
                "engine": "lingxing-openapi",
                "operation": "authorized paged retrieval",
                "grouped_by": [],
                "source_rows": total,
            },
        }

    registry.register(
        ToolDefinition(
            name="lingxing_profit_query",
            description=(
                "查询领星 ERP 利润报表（订单维度 transaction 视图）。"
                "必填 start_date、end_date；可选 currency_code（USD/CNY 等）、"
                "sids、length。若会话已有同类结果，优先复用，不要重复查询。"
            ),
            arguments_model=LingXingProfitQueryPlan,
            handler=execute,
            risk="low",
            requires_approval=False,
            timeout_seconds=max(1.0, timeout_seconds + 1),
            concurrency_safe=True,
            builtin=True,
        )
    )
