from __future__ import annotations

from typing import Any

from ...model_gateway import ModelGateway
from .domain import ProfitReportQueryPlan, ProfitReportQueryRequest, ProfitReportQueryResponse
from .query_tool import ProfitReportQueryTool


SYSTEM_PROMPT = """
你是利润报表查询 Agent。数据来自 PostgreSQL 表 lingxing_profit_order_transactions（领星订单维度 transaction 导出）。

把用户问题转换成受约束查询计划，不要生成 SQL。

metric 选择：
- overview：总体行数、结算毛利润、结算小计、销售额与时间范围
- daily：按结算日汇总
- store：按店铺汇总
- msku：按 MSKU 汇总
- order：按订单号汇总
- event_source：按费用类型汇总

仅提取用户明确给出的日期与币种。没有日期表示查询全部已导入数据。
limit 默认 20，最大 200。
若用户未给年份，优先使用库内已有数据的年份；不要猜测 2022 等过时年份。
查询前若不确定范围，可先用 metric=overview 且不带日期查看 first_posted_at / last_posted_at。
""".strip()


class ProfitReportAgent:
    def __init__(
        self,
        model: ModelGateway,
        query_tool: ProfitReportQueryTool,
        system_prompt: str = SYSTEM_PROMPT,
    ) -> None:
        self.model = model
        self.query_tool = query_tool
        self.system_prompt = system_prompt

    def set_system_prompt(self, prompt: str) -> None:
        self.system_prompt = prompt.strip() or SYSTEM_PROMPT

    @staticmethod
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

    def run(self, request: ProfitReportQueryRequest) -> ProfitReportQueryResponse:
        plan = self.model.structured(
            ProfitReportQueryPlan,
            system_prompt=self.system_prompt,
            payload={"objective": request.question},
        )
        if request.currency_code and not plan.currency_code:
            plan = plan.model_copy(update={"currency_code": request.currency_code.upper()})
        rows, total = self.query_tool.execute(plan)
        return ProfitReportQueryResponse(
            question=request.question,
            plan=plan,
            columns=list(rows[0].keys()) if rows else [],
            rows=rows,
            summary=self._summary(plan, rows, total),
            total_rows=total,
        )
