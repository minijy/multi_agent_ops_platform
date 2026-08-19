from __future__ import annotations

from typing import TYPE_CHECKING

from ..agent_registry import AgentDefinition, AgentRegistry
from ..config import Settings
from ..agent_roles import (
    ANALYST_AGENT_ID,
    AMAZON_FINANCE_ANALYST_ID,
    COORDINATOR_AGENT_ID,
    DATA_QUERY_TOOL_NAMES,
    ERP_ANALYST_ID,
    PROFIT_ANALYST_ID,
    SPECIALIST_ANALYST_IDS,
    DINGTALK_TOOL_NAMES,
)
from .tools import ToolRegistry

if TYPE_CHECKING:
    from ..connections import ConnectionRegistry, ConnectorType


def _has_connection(
    connections: ConnectionRegistry | None,
    connector_type: ConnectorType,
    tenant_id: str | None,
) -> bool:
    if connections is None:
        return False
    if tenant_id is not None:
        return connections.get_default(tenant_id, connector_type) is not None
    return connections.configured(connector_type)

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
    registry: AgentRegistry,
    settings: Settings,
    connections: ConnectionRegistry | None = None,
    tenant_id: str | None = None,
) -> bool:
    configured = _has_connection(connections, "analytics", tenant_id)
    return configured and registry.amazon_finance_config().enabled


def lingxing_profit_tool_active(
    registry: AgentRegistry,
    connections: ConnectionRegistry | None = None,
    tenant_id: str | None = None,
) -> bool:
    agent = registry.lingxing_profit_config()
    configured = _has_connection(connections, "lingxing", tenant_id)
    return agent.enabled and configured


def profit_report_tool_active(
    registry: AgentRegistry,
    settings: Settings,
    connections: ConnectionRegistry | None = None,
    tenant_id: str | None = None,
) -> bool:
    configured = _has_connection(connections, "analytics", tenant_id)
    return configured and registry.profit_report_config().enabled


def kingdee_cloud_tool_active(
    registry: AgentRegistry,
    connections: ConnectionRegistry | None = None,
    tenant_id: str | None = None,
) -> bool:
    agent = registry.kingdee_cloud_config()
    configured = _has_connection(connections, "kingdee", tenant_id)
    return agent.enabled and configured


def active_data_query_tools(
    registry: AgentRegistry,
    settings: Settings,
    connections: ConnectionRegistry | None = None,
    tenant_id: str | None = None,
) -> frozenset[str]:
    active: set[str] = set()
    if amazon_finance_tool_active(registry, settings, connections, tenant_id):
        active.add("amazon_finance_query")
    if lingxing_profit_tool_active(registry, connections, tenant_id):
        active.add("lingxing_profit_query")
    if profit_report_tool_active(registry, settings, connections, tenant_id):
        active.add("profit_report_query")
    if kingdee_cloud_tool_active(registry, connections, tenant_id):
        active.add("kingdee_cloud_query")
    return frozenset(active)


def inactive_data_query_tools(
    registry: AgentRegistry,
    settings: Settings,
    connections: ConnectionRegistry | None = None,
    tenant_id: str | None = None,
) -> frozenset[str]:
    return frozenset(DATA_QUERY_TOOL_AGENTS) - active_data_query_tools(
        registry, settings, connections, tenant_id
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
    connections: ConnectionRegistry | None = None,
    tenant_id: str | None = None,
) -> set[str] | None:
    """Resolve Runtime tool visibility; drop data tools for disabled Agents."""
    all_names = set(tool_registry.tool_names())
    blocked = inactive_data_query_tools(registry, settings, connections, tenant_id)
    configured = tool_registry.resolve_allowed_tools(runtime_optional_tools)
    if configured is None:
        return all_names - blocked
    return configured - blocked


def resolve_agent_tool_allowlist(
    agent: AgentDefinition,
    registry: AgentRegistry,
    settings: Settings,
    tool_registry: ToolRegistry,
    connections: ConnectionRegistry | None = None,
    tenant_id: str | None = None,
) -> set[str]:
    """Resolve the complete tool set for a decision-core Agent."""
    blocked = inactive_data_query_tools(registry, settings, connections, tenant_id)
    visible = set(tool_registry.tool_names()) - blocked
    if agent.strict_tool_allowlist:
        requested = set(agent.allowed_tools)
        if agent.id == COORDINATOR_AGENT_ID:
            requested |= DINGTALK_TOOL_NAMES
            requested |= {
                "remember_fact",
                "search_memory",
                "forget_memory",
                "search_knowledge",
                "web_search",
            }
            requested -= DATA_QUERY_TOOL_NAMES
            if settings.analyst_mode == "general":
                requested.add("delegate_subagent")
                requested.discard("delegate_specialists")
            else:
                # In specialist mode every delegation goes through the batch tool.
                # This makes concurrency a Runtime guarantee instead of relying on
                # the model to choose between two overlapping delegation tools.
                requested.add("delegate_specialists")
                requested.discard("delegate_subagent")
        if agent.id == ANALYST_AGENT_ID or agent.id in SPECIALIST_ANALYST_IDS:
            requested.discard("delegate_subagent")
            requested -= {
                "remember_fact",
                "search_memory",
                "forget_memory",
                "search_knowledge",
                "web_search",
            }
        return requested & visible
    allowlist = runtime_tool_allowlist(
        registry,
        settings,
        tool_registry,
        agent.allowed_tools,
        connections,
        tenant_id,
    )
    return visible if allowlist is None else allowlist


def coordinator_delegation_prompt(
    registry: AgentRegistry | None,
    analyst_mode: str = "general",
    allowed_data_tools: set[str] | frozenset[str] | None = None,
) -> str:
    lines = ["\n可委派的子 Agent："]
    if analyst_mode == "specialized_parallel":
        specialists = (
            (AMAZON_FINANCE_ANALYST_ID, "Amazon 结算、费用、SKU 和结算批次"),
            (PROFIT_ANALYST_ID, "订单利润、收入、成本和毛利"),
            (ERP_ANALYST_ID, "金蝶销售、出库、应收和回款"),
        )
        available = [
            (agent_id, description)
            for agent_id, description in specialists
            if registry is not None
            and (agent := registry.get(agent_id)) is not None
            and agent.enabled
            and (
                allowed_data_tools is None
                or bool(
                    (set(agent.allowed_tools) & DATA_QUERY_TOOL_NAMES)
                    & set(allowed_data_tools)
                )
            )
        ]
        if not available:
            lines.append("- 当前没有已启用的专业分析 Agent。")
            return "\n".join(lines)
        for agent_id, description in available:
            lines.append(f"- {agent_id}：{description}。")
        lines.append(
            "- 所有数据分析任务只调用 delegate_specialists，一次性提交完整计划中的 "
            "1–3 个任务，并行完成后统一返回；不要调用 delegate_subagent。"
        )
        lines.append(
            "- delegate_specialists 会阻塞等待全部子任务结束。工具返回后必须立即根据 "
            "tasks[].answer 汇总最终答案；禁止回复‘正在收集’、‘请稍等’或‘稍后反馈’，"
            "也不能在没有实际结果时宣称任务已完成。"
        )
        lines.append(
            "- 不要按月份、产品或分页把同一领域拆成多个任务；应合并为一个专业任务，"
            "让查询工具通过时间范围和 group_by 完成统计。只有职责可独立时才拆分。"
        )
        return "\n".join(lines)
    analyst = registry.get(ANALYST_AGENT_ID) if registry is not None else None
    if analyst is not None and analyst.enabled:
        lines.append(
            "- analyst：Amazon 结算、费用、SKU、利润报表、领星或金蝶查询；"
            "agent_id 填 analyst，objective 写清用户目标。"
        )
    else:
        lines.append("- 当前没有已启用的分析子 Agent。")
    return "\n".join(lines)


def data_tool_usage_prompt(active_tools: frozenset[str]) -> str:
    lines = [
        "\n当前可用的数据查询工具（未列出的工具不可调用）：",
        "- 本列表已按当前用户权限过滤。同类工具中直接选择可用者，"
        "不得让用户选择技术数据源。",
        "- 输出不得包含物理表名、Schema、DSN 或 SQL；"
        "回答末尾必须标明工具返回的业务数据来源。",
    ]
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
            "- 领星利润分析数据（分析仓）："
            "调用 profit_report_query；必填 start_date、end_date，日期必须在库内已有结算时间范围内。"
        )
    if {"lingxing_profit_query", "profit_report_query"}.issubset(active_tools):
        lines.append(
            "- 上述两个利润工具均可用：用户未指定时优先实时数据；"
            "首选调用失败时自动改用分析仓，不要向用户追问。"
        )
    if "kingdee_cloud_query" in active_tools:
        lines.append(
            "- 金蝶云星空销售/应收单据：调用 kingdee_cloud_query；"
            "必填 document_type、start_date、end_date。"
        )
    return "\n".join(lines)
