from datetime import date

import pytest

from ops_agent.agent_integration import kingdee_integration_configured, merge_integration_update
from ops_agent.agent_registry import AgentUpdateRequest, create_agent_registry
from ops_agent.runtime.agent_tool_policy import active_data_query_tools
from ops_agent.config import Settings
from ops_agent.runtime.tools import ToolRegistry
from ops_agent.workflows.kingdee_cloud.domain import KingdeeQueryPlan
from ops_agent.workflows.kingdee_cloud.query_tool import build_filter_string, form_id_for


def test_kingdee_integration_configured_requires_all_fields():
    assert not kingdee_integration_configured(None)
    assert not kingdee_integration_configured({"server_url": "https://erp/K3Cloud"})
    assert kingdee_integration_configured(
        {
            "server_url": "https://erp/K3Cloud",
            "acct_id": "100001",
            "app_id": "app",
            "app_secret": "secret",
            "username": "demo",
        }
    )


def test_merge_integration_update_preserves_kingdee_secret():
    current = {
        "server_url": "https://erp/K3Cloud",
        "acct_id": "100001",
        "app_id": "app",
        "app_secret": "stored-secret",
        "username": "demo",
        "lcid": 2052,
    }
    merged = merge_integration_update(
        "kingdee-cloud",
        current,
        {"app_secret": "********", "username": "demo2"},
    )
    assert merged["app_secret"] == "stored-secret"
    assert merged["username"] == "demo2"


def test_build_filter_string_for_sale_order():
    plan = KingdeeQueryPlan(
        document_type="sale_order",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 1, 31),
        bill_no="SO-001",
    )
    text = build_filter_string(plan)
    assert "FDate >= '2026-01-01'" in text
    assert "FDate <= '2026-01-31'" in text
    assert "FBillNo = 'SO-001'" in text
    assert form_id_for("sale_order") == "SAL_SaleOrder"


def test_disabled_kingdee_agent_excludes_tool(tmp_path):
    registry = create_agent_registry(tmp_path / "agents.json")
    registry.update(
        "kingdee-cloud",
        AgentUpdateRequest(
            enabled=False,
            integration={
                "server_url": "https://erp/K3Cloud",
                "acct_id": "100001",
                "app_id": "app",
                "app_secret": "secret",
                "username": "demo",
            },
        ),
    )
    settings = Settings(_env_file=None)
    tools = active_data_query_tools(registry, settings)
    assert "kingdee_cloud_query" not in tools
