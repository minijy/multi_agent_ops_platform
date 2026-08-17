from __future__ import annotations

from typing import Any

from ..agent_registry import AgentRegistry
from ..integrations.kingdee.client import KingdeeClient, KingdeeCredentials
from ..workflows.kingdee_cloud.domain import KingdeeIntegrationConfig, KingdeeQueryPlan
from ..workflows.kingdee_cloud.query_tool import KingdeeQueryTool
from .tools import ToolDefinition, ToolExecutionContext, ToolRegistry


def _integration_from_registry(registry: AgentRegistry) -> KingdeeIntegrationConfig:
    agent = registry.kingdee_cloud_config()
    raw = agent.integration if isinstance(agent.integration, dict) else {}
    return KingdeeIntegrationConfig.model_validate(raw)


def register_kingdee_cloud_tool(
    registry: ToolRegistry,
    agent_registry: AgentRegistry,
    *,
    timeout_seconds: float = 45.0,
) -> None:
    query_tool = KingdeeQueryTool()

    def execute(
        plan: KingdeeQueryPlan,
        context: ToolExecutionContext,
    ) -> dict[str, Any]:
        integration = _integration_from_registry(agent_registry)
        if not (
            integration.server_url.strip()
            and integration.acct_id.strip()
            and integration.app_id.strip()
            and integration.app_secret.strip()
            and integration.username.strip()
        ):
            raise ValueError(
                "金蝶云星空 WebAPI 凭证未配置，请在 Agents 页编辑「金蝶云星空 Agent」"
                "填写服务地址、账套 ID、应用 ID、应用密钥与集成用户名"
            )
        client = KingdeeClient(
            KingdeeCredentials(
                server_url=integration.server_url.strip(),
                acct_id=integration.acct_id.strip(),
                app_id=integration.app_id.strip(),
                app_secret=integration.app_secret.strip(),
                username=integration.username.strip(),
                lcid=integration.lcid,
            ),
            timeout_seconds=timeout_seconds,
        )
        rows, label, form_id, columns = query_tool.execute(client, plan)
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
            "data_scope": "金蝶云星空 · ExecuteBillQuery",
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
