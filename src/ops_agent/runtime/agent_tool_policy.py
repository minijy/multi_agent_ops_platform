from __future__ import annotations

from ..agent_integration import kingdee_integration_configured, lingxing_integration_configured
from ..agent_registry import AgentRegistry
from ..config import Settings
from .tools import ToolRegistry

DATA_QUERY_TOOL_AGENTS: dict[str, str] = {
    "amazon_finance_query": "amazon-finance-query",
    "lingxing_profit_query": "lingxing-profit-report",
    "profit_report_query": "profit-report-query",
    "kingdee_cloud_query": "kingdee-cloud",
}

TOOL_SKILL_NAMES: dict[str, str] = {
    "amazon_finance_query": "amazon-settlement-analysis",
    "lingxing_profit_query": "lingxing-profit-analysis",
    "profit_report_query": "profit-report-analysis",
    "kingdee_cloud_query": "kingdee-cloud-analysis",
}


def amazon_finance_tool_active(
    registry: AgentRegistry, settings: Settings
) -> bool:
    return bool(settings.analytics_dsn) and registry.amazon_finance_config().enabled


def lingxing_profit_tool_active(registry: AgentRegistry) -> bool:
    agent = registry.lingxing_profit_config()
    return agent.enabled and lingxing_integration_configured(agent.integration)


def profit_report_tool_active(
    registry: AgentRegistry, settings: Settings
) -> bool:
    return bool(settings.analytics_dsn) and registry.profit_report_config().enabled


def kingdee_cloud_tool_active(registry: AgentRegistry) -> bool:
    agent = registry.kingdee_cloud_config()
    return agent.enabled and kingdee_integration_configured(agent.integration)


def active_data_query_tools(
    registry: AgentRegistry, settings: Settings
) -> frozenset[str]:
    active: set[str] = set()
    if amazon_finance_tool_active(registry, settings):
        active.add("amazon_finance_query")
    if lingxing_profit_tool_active(registry):
        active.add("lingxing_profit_query")
    if profit_report_tool_active(registry, settings):
        active.add("profit_report_query")
    if kingdee_cloud_tool_active(registry):
        active.add("kingdee_cloud_query")
    return frozenset(active)


def inactive_data_query_tools(
    registry: AgentRegistry, settings: Settings
) -> frozenset[str]:
    return frozenset(DATA_QUERY_TOOL_AGENTS) - active_data_query_tools(
        registry, settings
    )


def skill_names_for_tools(tool_names: set[str] | None) -> set[str] | None:
    if tool_names is None:
        return None
    return {
        skill
        for tool, skill in TOOL_SKILL_NAMES.items()
        if tool in tool_names
    }


def runtime_tool_allowlist(
    registry: AgentRegistry,
    settings: Settings,
    tool_registry: ToolRegistry,
    runtime_optional_tools: list[str] | None,
) -> set[str] | None:
    """Resolve Runtime tool visibility; drop data tools for disabled Agents."""
    all_names = set(tool_registry.tool_names())
    blocked = inactive_data_query_tools(registry, settings)
    configured = tool_registry.resolve_allowed_tools(runtime_optional_tools)
    if configured is None:
        return all_names - blocked
    return configured - blocked


def data_tool_usage_prompt(active_tools: frozenset[str]) -> str:
    lines = ["\n当前可用的数据查询工具（未列出的工具不可调用）："]
    if not active_tools:
        lines.append("- 无（相关 Agent 未启用或未配置）")
        return "\n".join(lines)
    if "amazon_finance_query" in active_tools:
        lines.append(
            "- Amazon 结算、费用、SKU、结算批次：调用 amazon_finance_query；"
            "不要编造数字，不要手写 SQL。"
        )
    if "lingxing_profit_query" in active_tools:
        lines.append(
            "- 领星开放平台实时利润报表：调用 lingxing_profit_query；"
            "必填 start_date、end_date。"
        )
    if "profit_report_query" in active_tools:
        lines.append(
            "- 本地 PostgreSQL 利润报表（lingxing_profit_order_transactions）："
            "调用 profit_report_query；必填 start_date、end_date，日期必须在库内已有结算时间范围内。"
        )
    if "kingdee_cloud_query" in active_tools:
        lines.append(
            "- 金蝶云星空销售/应收单据：调用 kingdee_cloud_query；"
            "必填 document_type、start_date、end_date。"
        )
    return "\n".join(lines)
