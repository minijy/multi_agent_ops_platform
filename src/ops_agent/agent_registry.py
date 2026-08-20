from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from .agent_roles import (
    ANALYST_AGENT_ID,
    ANALYST_SYSTEM_PROMPT,
    ANALYST_TOOLS,
    AMAZON_FINANCE_ANALYST_ID,
    AMAZON_FINANCE_ANALYST_PROMPT,
    COORDINATOR_AGENT_ID,
    COORDINATOR_SYSTEM_PROMPT,
    COORDINATOR_TOOLS,
    ERP_ANALYST_ID,
    ERP_ANALYST_PROMPT,
    PROFIT_ANALYST_ID,
    PROFIT_ANALYST_PROMPT,
)
from .workflows.amazon_finance.agent import SYSTEM_PROMPT as DEFAULT_AMAZON_SYSTEM_PROMPT
from .workflows.lingxing_profit.agent import SYSTEM_PROMPT as DEFAULT_LINGXING_SYSTEM_PROMPT
from .workflows.lingxing_profit.domain import LingXingIntegrationConfig
from .workflows.kingdee_cloud.agent import SYSTEM_PROMPT as DEFAULT_KINGDEE_SYSTEM_PROMPT
from .workflows.kingdee_cloud.domain import KingdeeIntegrationConfig
from .workflows.profit_report.agent import SYSTEM_PROMPT as DEFAULT_PROFIT_REPORT_SYSTEM_PROMPT
from .source_privacy import sanitize_public_text


AgentKind = Literal["runtime", "role", "hybrid", "workflow"]


class AgentDefinition(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=120)
    role: str = Field(min_length=1, max_length=500)
    kind: AgentKind
    description: str = Field(default="", max_length=2000)
    enabled: bool = True
    system_prompt: str = Field(default="", max_length=12000)
    allowed_tools: list[str] = Field(default_factory=list)
    strict_tool_allowlist: bool = False
    workflow_id: str = Field(default="", max_length=64)
    builtin: bool = True
    integration: dict[str, Any] = Field(default_factory=dict)

    def effective_system_prompt(self, default_prompt: str) -> str:
        prompt = self.system_prompt.strip()
        return prompt or default_prompt

    def accepts_delegation(self) -> bool:
        return self.kind == "role" and self.enabled


def default_agent_definitions() -> list[AgentDefinition]:
    return [
        AgentDefinition(
            id=COORDINATOR_AGENT_ID,
            name="Coordinator",
            role="拆任务、委派 Analyst、汇总回答",
            kind="runtime",
            description=(
                "面向用户的决策核。普通问题直接回答；查数、结算、利润报表委派给 Analyst。"
            ),
            enabled=True,
            system_prompt=COORDINATOR_SYSTEM_PROMPT,
            allowed_tools=list(COORDINATOR_TOOLS),
            strict_tool_allowlist=True,
            workflow_id="function-calling-runtime-v1",
            builtin=True,
        ),
        AgentDefinition(
            id=ANALYST_AGENT_ID,
            name="Analyst",
            role="受委派的数据查询决策核",
            kind="role",
            description=(
                "独立工具循环：按目标调用 Amazon / 利润报表 / 领星 / 金蝶只读查询工具并整理结论。"
                "由 Coordinator 通过 delegate_subagent 委派，不直接对用户说话。"
            ),
            enabled=True,
            system_prompt=ANALYST_SYSTEM_PROMPT,
            allowed_tools=list(ANALYST_TOOLS),
            strict_tool_allowlist=True,
            workflow_id="analyst-v1",
            builtin=True,
        ),
        AgentDefinition(
            id=AMAZON_FINANCE_ANALYST_ID,
            name="Amazon Finance Analyst",
            role="Amazon 结算与费用分析",
            kind="role",
            description="专门处理 Amazon 结算、费用、交易类型、SKU 和结算批次查询。",
            enabled=True,
            system_prompt=AMAZON_FINANCE_ANALYST_PROMPT,
            allowed_tools=["load_skill", "amazon_finance_query"],
            strict_tool_allowlist=True,
            workflow_id="amazon-finance-analyst-v1",
            builtin=True,
        ),
        AgentDefinition(
            id=PROFIT_ANALYST_ID,
            name="Profit Analyst",
            role="订单利润与毛利分析",
            kind="role",
            description="专门处理领星实时利润和数据库利润仓分析。",
            enabled=True,
            system_prompt=PROFIT_ANALYST_PROMPT,
            allowed_tools=["load_skill", "lingxing_profit_query", "profit_report_query"],
            strict_tool_allowlist=True,
            workflow_id="profit-analyst-v1",
            builtin=True,
        ),
        AgentDefinition(
            id=ERP_ANALYST_ID,
            name="ERP Analyst",
            role="金蝶销售与应收分析",
            kind="role",
            description="专门处理金蝶销售订单、出库、应收、客户和回款查询。",
            enabled=True,
            system_prompt=ERP_ANALYST_PROMPT,
            allowed_tools=["load_skill", "kingdee_cloud_query"],
            strict_tool_allowlist=True,
            workflow_id="erp-analyst-v1",
            builtin=True,
        ),
        AgentDefinition(
            id="amazon-finance-query",
            name="Amazon Finance Query Agent",
            role="结算指标规划与只读查询",
            kind="hybrid",
            description=(
                "将自然语言转换为受约束的 Amazon 结算查询计划，并通过只读 SQL 白名单执行。"
            ),
            enabled=True,
            system_prompt=DEFAULT_AMAZON_SYSTEM_PROMPT,
            allowed_tools=["amazon_finance_query"],
            workflow_id="amazon-finance-query-v1",
            builtin=True,
        ),
        AgentDefinition(
            id="lingxing-profit-report",
            name="领星开放平台 Agent",
            role="开放平台实时接口 · 利润报表订单 transaction",
            kind="hybrid",
            description=(
                "直连领星开放平台 API，按时间与币种实时拉取「利润报表 - 订单维度 transaction 视图」。"
                "App ID / App Secret 由管理员在本 Agent 配置中维护。"
            ),
            enabled=True,
            system_prompt=DEFAULT_LINGXING_SYSTEM_PROMPT,
            allowed_tools=["lingxing_profit_query"],
            workflow_id="lingxing-profit-report-v1",
            integration=LingXingIntegrationConfig().model_dump(),
            builtin=True,
        ),
        AgentDefinition(
            id="profit-report-query",
            name="利润报表数据库 Agent",
            role="分析数据库 · 只读 SQL 白名单",
            kind="hybrid",
            description=(
                "查询已导入分析仓的领星利润报表。"
                "适合离线分析、大批量汇总；数据需先由 XLSX 导入脚本写入本地库。"
            ),
            enabled=True,
            system_prompt=DEFAULT_PROFIT_REPORT_SYSTEM_PROMPT,
            allowed_tools=["profit_report_query"],
            workflow_id="profit-report-query-v1",
            builtin=True,
        ),
        AgentDefinition(
            id="kingdee-cloud",
            name="金蝶云星空 Agent",
            role="私有云 WebAPI · 销售/应收单据查询",
            kind="hybrid",
            description=(
                "通过金蝶云星空 DynamicFormService.ExecuteBillQuery 查询销售订单、"
                "销售出库单、普通应收单、费用应收单。WebAPI 凭证由管理员在本 Agent 配置。"
            ),
            enabled=False,
            system_prompt=DEFAULT_KINGDEE_SYSTEM_PROMPT,
            allowed_tools=["kingdee_cloud_query"],
            workflow_id="kingdee-cloud-v1",
            integration=KingdeeIntegrationConfig().model_dump(),
            builtin=True,
        ),
    ]


def _migrate_coordinator_override(
    default: AgentDefinition, override: dict[str, Any]
) -> dict[str, Any]:
    migrated = dict(override)
    migrated.setdefault("strict_tool_allowlist", True)
    tools = list(migrated.get("allowed_tools") or default.allowed_tools)
    if "search_knowledge" not in tools:
        tools.append("search_knowledge")
    if "web_search" not in tools:
        tools.append("web_search")
    migrated["allowed_tools"] = tools
    prompt = str(migrated.get("system_prompt") or "")
    if (
        not prompt.strip()
        or "你是企业数据与运维助手" in prompt
        or "agent_id 必须是 analyst" in prompt
        or (
            "制度、手册、故障码、SOP、内部文档：调用 search_knowledge" in prompt
            and "系统不会预先检索知识库" not in prompt
        )
        or (
            "系统不会预先检索知识库" in prompt
            and "即使问「是什么意思」" not in prompt
        )
        or (
            "即使问「是什么意思」" in prompt
            and "web_search" not in prompt
        )
        or (
            "调用 web_search" in prompt
            and "当前工具列表里有 web_search" not in prompt
        )
    ):
        migrated["system_prompt"] = default.system_prompt
    if migrated.get("name") in {None, "", "Function Calling Agent"}:
        migrated["name"] = default.name
    role = str(migrated.get("role") or "")
    if not role or "模型路由、工具循环" in role:
        migrated["role"] = default.role
    description = str(migrated.get("description") or "")
    if "通用 Agent 对话 Runtime" in description:
        migrated["description"] = default.description
    return migrated


class AgentUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    role: str | None = Field(default=None, min_length=1, max_length=500)
    description: str | None = Field(default=None, max_length=2000)
    enabled: bool | None = None
    system_prompt: str | None = Field(default=None, max_length=12000)
    allowed_tools: list[str] | None = None
    integration: dict[str, Any] | None = None


class AgentRegistry:
    def __init__(
        self,
        path: Path | None = None,
        *,
        store: Any | None = None,
    ) -> None:
        self.path = path
        self.store = store
        self._agents: dict[str, AgentDefinition] = {}
        self.reload()

    def reload(self) -> None:
        defaults = {item.id: item for item in default_agent_definitions()}
        if self.store is not None:
            stored_items = {
                item["id"]: item for item in self.store.list_agents() if item.get("id")
            }
            merged: dict[str, AgentDefinition] = {}
            for agent_id, default in defaults.items():
                override = stored_items.get(agent_id, {})
                if override:
                    if agent_id == COORDINATOR_AGENT_ID:
                        override = _migrate_coordinator_override(default, override)
                    merged[agent_id] = default.model_copy(
                        update={
                            key: (
                                sanitize_public_text(value)
                                if key in {"role", "description", "system_prompt"}
                                and isinstance(value, str)
                                else value
                            )
                            for key, value in override.items()
                            if key in AgentDefinition.model_fields and key != "id"
                        }
                    )
                else:
                    merged[agent_id] = default
            # Keep any custom (non-default) agents stored in DB.
            for agent_id, payload in stored_items.items():
                if agent_id in merged:
                    continue
                try:
                    merged[agent_id] = AgentDefinition.model_validate(payload)
                except Exception:
                    continue
            self._agents = merged
            return

        stored = self._read_file()
        merged = {}
        for agent_id, default in defaults.items():
            override = stored.get(agent_id, {})
            if override:
                if agent_id == COORDINATOR_AGENT_ID:
                    override = _migrate_coordinator_override(default, override)
                merged[agent_id] = default.model_copy(
                    update={
                        key: (
                            sanitize_public_text(value)
                            if key in {"role", "description", "system_prompt"}
                            and isinstance(value, str)
                            else value
                        )
                        for key, value in override.items()
                        if key in AgentDefinition.model_fields and key != "id"
                    }
                )
            else:
                merged[agent_id] = default
        self._agents = merged
        if self.path is not None and not self.path.is_file():
            self.save()

    def save(self) -> None:
        if self.store is not None:
            for agent in self._agents.values():
                self.store.upsert_agent(agent.model_dump(mode="json"))
            return
        if self.path is None:
            raise RuntimeError("agent registry has no persistence backend")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            agent_id: agent.model_dump(mode="json")
            for agent_id, agent in self._agents.items()
        }
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _read_file(self) -> dict[str, dict[str, Any]]:
        if self.path is None or not self.path.is_file():
            return {}
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return {}
        if not isinstance(loaded, dict):
            return {}
        return {
            key: value
            for key, value in loaded.items()
            if isinstance(key, str) and isinstance(value, dict)
        }

    def list(self) -> list[AgentDefinition]:
        return list(self._agents.values())

    def get(self, agent_id: str) -> AgentDefinition | None:
        return self._agents.get(agent_id)

    def update(self, agent_id: str, updates: AgentUpdateRequest) -> AgentDefinition:
        current = self._agents.get(agent_id)
        if current is None:
            raise KeyError(agent_id)
        patch = updates.model_dump(exclude_unset=True)
        if "integration" in patch:
            from .agent_integration import merge_integration_update

            patch["integration"] = merge_integration_update(
                agent_id,
                current.integration,
                patch.pop("integration"),
            )
        updated = current.model_copy(update=patch)
        self._agents[agent_id] = updated
        self.save()
        return updated

    def replace_integration(
        self, agent_id: str, integration: dict[str, Any]
    ) -> AgentDefinition:
        current = self._agents.get(agent_id)
        if current is None:
            raise KeyError(agent_id)
        updated = current.model_copy(update={"integration": dict(integration)})
        self._agents[agent_id] = updated
        self.save()
        return updated

    def runtime_config(self) -> AgentDefinition:
        return self._agents[COORDINATOR_AGENT_ID]

    def analyst_config(self) -> AgentDefinition:
        return self._agents[ANALYST_AGENT_ID]

    def amazon_finance_config(self) -> AgentDefinition:
        return self._agents["amazon-finance-query"]

    def lingxing_profit_config(self) -> AgentDefinition:
        return self._agents["lingxing-profit-report"]

    def profit_report_config(self) -> AgentDefinition:
        return self._agents["profit-report-query"]

    def kingdee_cloud_config(self) -> AgentDefinition:
        return self._agents["kingdee-cloud"]

    def catalog_items(
        self,
        *,
        amazon_active: bool,
        lingxing_active: bool = False,
        profit_report_active: bool = False,
        kingdee_active: bool = False,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for agent in self.list():
            status = "active"
            if not agent.enabled:
                status = "disabled"
            elif agent.id == "amazon-finance-query" and not amazon_active:
                status = "disabled"
            elif agent.id == "lingxing-profit-report" and not lingxing_active:
                status = "disabled"
            elif agent.id == "profit-report-query" and not profit_report_active:
                status = "disabled"
            elif agent.id == "kingdee-cloud" and not kingdee_active:
                status = "disabled"
            items.append(
                {
                    "id": agent.id,
                    "name": agent.name,
                    "role": agent.role,
                    "kind": agent.kind,
                    "description": agent.description,
                    "enabled": agent.enabled,
                    "status": status,
                    "workflow_id": agent.workflow_id,
                    "allowed_tools": agent.allowed_tools,
                    "strict_tool_allowlist": agent.strict_tool_allowlist,
                    "builtin": agent.builtin,
                    "system_prompt_configured": bool(agent.system_prompt.strip()),
                }
            )
        return items


def create_agent_registry(path: Path | None = None, *, store: Any | None = None) -> AgentRegistry:
    if store is not None:
        return AgentRegistry(store=store)
    if path is None:
        raise ValueError("create_agent_registry requires path or store")
    return AgentRegistry(path.expanduser().resolve())


def snapshot_agents(
    registry: AgentRegistry,
    *,
    amazon_active: bool,
    lingxing_active: bool = False,
    profit_report_active: bool = False,
    kingdee_active: bool = False,
) -> dict[str, Any]:
    agents = registry.catalog_items(
        amazon_active=amazon_active,
        lingxing_active=lingxing_active,
        profit_report_active=profit_report_active,
        kingdee_active=kingdee_active,
    )
    workflows = [
        {
            "id": "function-calling-runtime-v1",
            "name": "Coordinator",
            "status": next(
                (
                    item["status"]
                    for item in agents
                    if item["id"] == COORDINATOR_AGENT_ID
                ),
                "active",
            ),
        },
        {
            "id": "analyst-v1",
            "name": "Analyst",
            "status": next(
                (
                    item["status"]
                    for item in agents
                    if item["id"] == ANALYST_AGENT_ID
                ),
                "active",
            ),
        },
        {
            "id": "amazon-finance-analyst-v1",
            "name": "Amazon Finance Analyst",
            "status": next(
                (
                    item["status"]
                    for item in agents
                    if item["id"] == AMAZON_FINANCE_ANALYST_ID
                ),
                "active",
            ),
        },
        {
            "id": "profit-analyst-v1",
            "name": "Profit Analyst",
            "status": next(
                (
                    item["status"]
                    for item in agents
                    if item["id"] == PROFIT_ANALYST_ID
                ),
                "active",
            ),
        },
        {
            "id": "erp-analyst-v1",
            "name": "ERP Analyst",
            "status": next(
                (
                    item["status"]
                    for item in agents
                    if item["id"] == ERP_ANALYST_ID
                ),
                "active",
            ),
        },
        {
            "id": "amazon-finance-query-v1",
            "name": "Amazon 结算数据查询",
            "status": next(
                (
                    item["status"]
                    for item in agents
                    if item["id"] == "amazon-finance-query"
                ),
                "disabled",
            ),
        },
        {
            "id": "lingxing-profit-report-v1",
            "name": "领星开放平台 · 利润报表 API",
            "status": next(
                (
                    item["status"]
                    for item in agents
                    if item["id"] == "lingxing-profit-report"
                ),
                "disabled",
            ),
        },
        {
            "id": "profit-report-query-v1",
            "name": "利润报表 · 分析数据库",
            "status": next(
                (
                    item["status"]
                    for item in agents
                    if item["id"] == "profit-report-query"
                ),
                "disabled",
            ),
        },
        {
            "id": "kingdee-cloud-v1",
            "name": "金蝶云星空 · WebAPI",
            "status": next(
                (
                    item["status"]
                    for item in agents
                    if item["id"] == "kingdee-cloud"
                ),
                "disabled",
            ),
        },
    ]
    return {"workflows": workflows, "agents": agents}
