from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from .workflows.amazon_finance.agent import SYSTEM_PROMPT as DEFAULT_AMAZON_SYSTEM_PROMPT
from .workflows.lingxing_profit.agent import SYSTEM_PROMPT as DEFAULT_LINGXING_SYSTEM_PROMPT
from .workflows.lingxing_profit.domain import LingXingIntegrationConfig
from .workflows.kingdee_cloud.agent import SYSTEM_PROMPT as DEFAULT_KINGDEE_SYSTEM_PROMPT
from .workflows.kingdee_cloud.domain import KingdeeIntegrationConfig
from .workflows.profit_report.agent import SYSTEM_PROMPT as DEFAULT_PROFIT_REPORT_SYSTEM_PROMPT


AgentKind = Literal["runtime", "hybrid", "workflow"]


class AgentDefinition(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=120)
    role: str = Field(min_length=1, max_length=500)
    kind: AgentKind
    description: str = Field(default="", max_length=2000)
    enabled: bool = True
    system_prompt: str = Field(default="", max_length=12000)
    allowed_tools: list[str] = Field(default_factory=list)
    workflow_id: str = Field(default="", max_length=64)
    builtin: bool = True
    integration: dict[str, Any] = Field(default_factory=dict)

    def effective_system_prompt(self, default_prompt: str) -> str:
        prompt = self.system_prompt.strip()
        return prompt or default_prompt


def _runtime_default_prompt() -> str:
    from .runtime.agent_loop import SYSTEM_PROMPT

    return SYSTEM_PROMPT


def default_agent_definitions() -> list[AgentDefinition]:
    runtime_prompt = _runtime_default_prompt()
    return [
        AgentDefinition(
            id="function-calling-runtime",
            name="Function Calling Agent",
            role="模型路由、工具循环与 Session 事件记录",
            kind="runtime",
            description=(
                "通用 Agent 对话 Runtime。支持 Skills、MCP、沙箱、Subagent、"
                "Amazon 结算、领星 API、金蝶云星空与本地利润报表查询工具。"
            ),
            enabled=True,
            system_prompt=runtime_prompt,
            allowed_tools=[],
            workflow_id="function-calling-runtime-v1",
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
            role="PostgreSQL 本地仓 · 只读 SQL 白名单",
            kind="hybrid",
            description=(
                "查询已导入 PostgreSQL 的领星利润报表（表 lingxing_profit_order_transactions）。"
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


class AgentUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    role: str | None = Field(default=None, min_length=1, max_length=500)
    description: str | None = Field(default=None, max_length=2000)
    enabled: bool | None = None
    system_prompt: str | None = Field(default=None, max_length=12000)
    allowed_tools: list[str] | None = None
    integration: dict[str, Any] | None = None


class AgentRegistry:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._agents: dict[str, AgentDefinition] = {}
        self.reload()

    def reload(self) -> None:
        defaults = {item.id: item for item in default_agent_definitions()}
        stored = self._read_file()
        merged: dict[str, AgentDefinition] = {}
        for agent_id, default in defaults.items():
            override = stored.get(agent_id, {})
            if override:
                merged[agent_id] = default.model_copy(
                    update={
                        key: value
                        for key, value in override.items()
                        if key in AgentDefinition.model_fields and key != "id"
                    }
                )
            else:
                merged[agent_id] = default
        self._agents = merged
        if not self.path.is_file():
            self.save()

    def save(self) -> None:
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
        if not self.path.is_file():
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

    def runtime_config(self) -> AgentDefinition:
        return self._agents["function-calling-runtime"]

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
                    "builtin": agent.builtin,
                    "system_prompt_configured": bool(agent.system_prompt.strip()),
                }
            )
        return items


def create_agent_registry(path: Path) -> AgentRegistry:
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
            "name": "通用 Function Calling Runtime",
            "status": next(
                (
                    item["status"]
                    for item in agents
                    if item["id"] == "function-calling-runtime"
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
            "name": "利润报表 · PostgreSQL 本地仓",
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
