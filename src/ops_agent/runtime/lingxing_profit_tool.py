from __future__ import annotations

from typing import Any

from ..agent_registry import AgentRegistry
from ..workflows.lingxing_profit.domain import (
    LingXingIntegrationConfig,
    LingXingProfitQueryPlan,
)
from ..workflows.lingxing_profit.query_tool import LingXingProfitQueryTool
from ..integrations.lingxing.client import LingXingClient
from .tools import ToolDefinition, ToolExecutionContext, ToolRegistry


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


def _integration_from_registry(registry: AgentRegistry) -> LingXingIntegrationConfig:
    agent = registry.lingxing_profit_config()
    raw = agent.integration if isinstance(agent.integration, dict) else {}
    return LingXingIntegrationConfig.model_validate(raw)


def register_lingxing_profit_tool(
    registry: ToolRegistry,
    agent_registry: AgentRegistry,
    *,
    timeout_seconds: float = 30.0,
) -> None:
    query_tool = LingXingProfitQueryTool()

    def execute(
        plan: LingXingProfitQueryPlan,
        context: ToolExecutionContext,
    ) -> dict[str, Any]:
        integration = _integration_from_registry(agent_registry)
        if not integration.app_id or not integration.app_secret:
            raise ValueError("领星开放平台凭证未配置，请在 Agents 页编辑「领星利润报表 Agent」填写 App ID 与 App Secret")
        client = LingXingClient(
            integration.app_id,
            integration.app_secret,
            base_url=integration.base_url,
            timeout_seconds=timeout_seconds,
        )
        rows, total = query_tool.execute(client, plan)
        return {
            "plan": plan.model_dump(mode="json"),
            "columns": list(rows[0].keys()) if rows else [],
            "rows": rows,
            "summary": _summary(plan, rows, total),
            "total": total,
            "data_scope": "领星利润报表 · 订单维度 transaction 视图",
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
