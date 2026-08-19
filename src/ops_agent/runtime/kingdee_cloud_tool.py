from __future__ import annotations

from typing import Any

from ..workflows.kingdee_cloud.domain import KingdeeQueryPlan
from ..workflows.kingdee_cloud.query_tool import KingdeeQueryTool
from .tools import ToolDefinition, ToolExecutionContext, ToolRegistry
from .connectors import ConnectorRuntime
from ..source_privacy import KINGDEE_SOURCE


def register_kingdee_cloud_tool(
    registry: ToolRegistry,
    connectors: ConnectorRuntime,
    *,
    timeout_seconds: float = 45.0,
) -> None:
    query_tool = KingdeeQueryTool()

    def execute(
        plan: KingdeeQueryPlan,
        context: ToolExecutionContext,
    ) -> dict[str, Any]:
        rows, label, form_id, columns = connectors.execute_tool(
            context.tenant_id,
            "kingdee_cloud_query",
            lambda client, _connection: query_tool.execute(client, plan),
        )
        summary = (
            f"{label} 在 {plan.start_date} 至 {plan.end_date} 无匹配记录"
            if not rows
            else f"返回 {len(rows)} 条{label}，区间 {plan.start_date} ~ {plan.end_date}。"
        )
        return {
            "plan": plan.model_dump(mode="json"),
            "document_label": label,
            "form_id": form_id,
            "columns": columns,
            "rows": rows,
            "summary": summary,
            "total": len(rows),
            "data_scope": KINGDEE_SOURCE,
            "data_source": KINGDEE_SOURCE,
            "calculation": {
                "engine": "kingdee-webapi",
                "operation": "authorized bill query",
                "grouped_by": [],
                "source_rows": len(rows),
            },
        }

    registry.register(
        ToolDefinition(
            name="kingdee_cloud_query",
            description=(
                "查询金蝶云星空私有云 WebAPI 单据（ExecuteBillQuery）。"
                "支持销售订单、销售出库单、普通应收单、费用应收单。"
                "必填 document_type、start_date、end_date；可选 bill_no、limit。"
            ),
            arguments_model=KingdeeQueryPlan,
            handler=execute,
            risk="low",
            requires_approval=False,
            timeout_seconds=max(1.0, timeout_seconds + 1),
            concurrency_safe=True,
            builtin=True,
        )
    )
