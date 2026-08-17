from __future__ import annotations

from typing import Any

from ...integrations.lingxing.client import LingXingClient
from ...model_gateway import ModelGateway
from .domain import (
    LingXingIntegrationConfig,
    LingXingProfitQueryPlan,
    LingXingProfitQueryRequest,
    LingXingProfitQueryResponse,
)
from .query_tool import LingXingProfitQueryTool


SYSTEM_PROMPT = """
你是领星利润报表查询 Agent。把用户问题转换成一个受约束的查询计划，不要编造数据。

接口：领星开放平台「利润报表 - 订单维度 transaction 视图」。
必填：start_date、end_date（YYYY-MM-DD）。
可选：
- currency_code：USD/CNY/EUR 等；留空表示原币种
- length：返回条数，默认 20，最大 1000
- sids：店铺 ID 列表；用户未指定时留空
- search_date_field：默认 posted_date_locale（结算时间）
- order_status：默认 Disbursed（已发放）

仅提取用户明确给出的日期与币种。没有币种就留空。
""".strip()


class LingXingProfitAgent:
    def __init__(
        self,
        model: ModelGateway,
        query_tool: LingXingProfitQueryTool,
        integration: LingXingIntegrationConfig | None = None,
        system_prompt: str = SYSTEM_PROMPT,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.model = model
        self.query_tool = query_tool
        self.integration = integration or LingXingIntegrationConfig()
        self.system_prompt = system_prompt
        self.timeout_seconds = timeout_seconds

    def set_system_prompt(self, prompt: str) -> None:
        self.system_prompt = prompt.strip() or SYSTEM_PROMPT

    def set_integration(self, integration: LingXingIntegrationConfig) -> None:
        self.integration = integration

    def _client(self) -> LingXingClient:
        if not self.integration.app_id or not self.integration.app_secret:
            raise ValueError("领星开放平台凭证未配置")
        return LingXingClient(
            self.integration.app_id,
            self.integration.app_secret,
            base_url=self.integration.base_url,
            timeout_seconds=self.timeout_seconds,
        )

    @staticmethod
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

    def run(self, request: LingXingProfitQueryRequest) -> LingXingProfitQueryResponse:
        plan = self.model.structured(
            LingXingProfitQueryPlan,
            system_prompt=self.system_prompt,
            payload={"objective": request.question},
        )
        rows, total = self.query_tool.execute(self._client(), plan)
        return LingXingProfitQueryResponse(
            question=request.question,
            plan=plan,
            columns=list(rows[0].keys()) if rows else [],
            rows=rows,
            summary=self._summary(plan, rows, total),
            total=total,
        )
