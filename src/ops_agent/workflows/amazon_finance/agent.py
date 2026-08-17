from __future__ import annotations

from typing import Any

from ...model_gateway import ModelGateway
from .domain import (
    AmazonFinanceQueryPlan,
    AmazonFinanceQueryRequest,
    AmazonFinanceQueryResponse,
)
from .query_tool import AmazonFinanceQueryTool


SYSTEM_PROMPT = """
你是 Amazon Finance Query Agent。把用户问题转换成一个受约束的查询计划，不生成 SQL。
数据只包含 transactionStatus=RELEASED 的 Amazon Finances listTransactions 交易。

metric 选择：
- overview：总体交易数、净额和时间范围
- daily：按 UTC 记账日汇总
- transaction_type：按 Shipment、Refund、ProductAdsPayment 等交易类型汇总
- fee：按叶子费用类型汇总
- sku：按 SKU 汇总数量和净额
- settlement：按 SETTLEMENT_ID 汇总

仅提取用户明确给出的日期。没有日期就留空，表示查询当前已导入的全部数据。
limit 默认 20，最大 100。
""".strip()


class AmazonFinanceAgent:
    def __init__(
        self,
        model: ModelGateway,
        query_tool: AmazonFinanceQueryTool,
        system_prompt: str = SYSTEM_PROMPT,
    ) -> None:
        self.model = model
        self.query_tool = query_tool
        self.system_prompt = system_prompt

    def set_system_prompt(self, prompt: str) -> None:
        self.system_prompt = prompt.strip() or SYSTEM_PROMPT

    @staticmethod
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
        return f"按当前条件返回 {len(rows)} {labels[plan.metric]}，数据口径仅包含 RELEASED。"

    def run(self, request: AmazonFinanceQueryRequest) -> AmazonFinanceQueryResponse:
        plan = self.model.structured(
            AmazonFinanceQueryPlan,
            system_prompt=self.system_prompt,
            payload={"objective": request.question},
        )
        seller_id, rows = self.query_tool.execute(plan, seller_id=request.seller_id)
        return AmazonFinanceQueryResponse(
            question=request.question,
            seller_id=seller_id,
            plan=plan,
            columns=list(rows[0].keys()) if rows else [],
            rows=rows,
            summary=self._summary(plan, rows),
        )
