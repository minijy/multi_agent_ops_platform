from __future__ import annotations

from typing import Any

from ...integrations.kingdee.client import KingdeeClient, KingdeeCredentials
from ...model_gateway import ModelGateway
from .domain import (
    KingdeeIntegrationConfig,
    KingdeeQueryPlan,
    KingdeeQueryRequest,
    KingdeeQueryResponse,
)
from .query_tool import KingdeeQueryTool, document_label


SYSTEM_PROMPT = """
你是金蝶云星空查询 Agent。把用户问题转换成一个受约束的 ExecuteBillQuery 查询计划，不要编造数据。

支持的单据类型 document_type：
- sale_order：销售订单（FormId SAL_SaleOrder）
- sale_outstock：销售出库单（FormId SAL_OUTSTOCK）
- ar_receivable：普通应收单（FormId AR_receivable）
- ar_expense_receivable：费用应收单（FormId AR_OtherRecAble）

必填：document_type、start_date、end_date（YYYY-MM-DD）。
可选：
- bill_no：指定单据编号时填写
- limit：返回条数，默认 50，最大 1000
- start_row：分页起始行，默认 0
- extra_filter：仅在用户明确要求额外过滤时填写合法金蝶过滤表达式

根据用户意图选择最匹配的单据类型。用户说「出库」选 sale_outstock；说「应收/欠款」选 ar_receivable；
说「费用应收/其他应收」选 ar_expense_receivable；说「销售订单/SO」选 sale_order。
仅提取用户明确给出的日期与单号，不要猜测过时年份。
""".strip()


class KingdeeCloudAgent:
    def __init__(
        self,
        model: ModelGateway,
        query_tool: KingdeeQueryTool,
        integration: KingdeeIntegrationConfig | None = None,
        system_prompt: str = SYSTEM_PROMPT,
        timeout_seconds: float = 45.0,
    ) -> None:
        self.model = model
        self.query_tool = query_tool
        self.integration = integration or KingdeeIntegrationConfig()
        self.system_prompt = system_prompt
        self.timeout_seconds = timeout_seconds

    def set_system_prompt(self, prompt: str) -> None:
        self.system_prompt = prompt.strip() or SYSTEM_PROMPT

    def set_integration(self, integration: KingdeeIntegrationConfig) -> None:
        self.integration = integration

    def _client(self) -> KingdeeClient:
        cfg = self.integration
        if not (
            cfg.server_url.strip()
            and cfg.acct_id.strip()
            and cfg.app_id.strip()
            and cfg.app_secret.strip()
            and cfg.username.strip()
        ):
            raise ValueError("金蝶云星空 WebAPI 凭证未配置")
        return KingdeeClient(
            KingdeeCredentials(
                server_url=cfg.server_url.strip(),
                acct_id=cfg.acct_id.strip(),
                app_id=cfg.app_id.strip(),
                app_secret=cfg.app_secret.strip(),
                username=cfg.username.strip(),
                lcid=cfg.lcid,
            ),
            timeout_seconds=self.timeout_seconds,
        )

    @staticmethod
    def _summary(
        plan: KingdeeQueryPlan,
        label: str,
        rows: list[dict[str, Any]],
    ) -> str:
        if not rows:
            return (
                f"{label} 在 {plan.start_date} 至 {plan.end_date} 无匹配记录"
                f"（limit={plan.limit}）。"
            )
        return (
            f"返回 {len(rows)} 条{label}，区间 {plan.start_date} ~ {plan.end_date}。"
            f"{' 单号 ' + plan.bill_no if plan.bill_no else ''}"
        )

    def run(self, request: KingdeeQueryRequest) -> KingdeeQueryResponse:
        plan = self.model.structured(
            KingdeeQueryPlan,
            system_prompt=self.system_prompt,
            payload={"objective": request.question},
        )
        rows, label, form_id, columns = self.query_tool.execute(self._client(), plan)
        return KingdeeQueryResponse(
            question=request.question,
            plan=plan,
            document_label=label,
            form_id=form_id,
            columns=columns,
            rows=rows,
            summary=self._summary(plan, label, rows),
            total=len(rows),
        )
