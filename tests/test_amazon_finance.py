from datetime import date

from ops_agent.model_gateway import MockModelGateway
from ops_agent.workflows.amazon_finance.domain import AmazonFinanceQueryPlan
from ops_agent.workflows.amazon_finance.query_tool import AmazonFinanceQueryTool


def test_mock_model_builds_monthly_fee_plan():
    plan = MockModelGateway().structured(
        AmazonFinanceQueryPlan,
        system_prompt="test",
        payload={"objective": "分析 2026年7月 Top 10 费用"},
    )

    assert plan.metric == "fee"
    assert plan.start_date == date(2026, 7, 1)
    assert plan.end_date == date(2026, 7, 31)
    assert plan.limit == 10


def test_mock_model_defaults_to_overview_without_dates():
    plan = MockModelGateway().structured(
        AmazonFinanceQueryPlan,
        system_prompt="test",
        payload={"objective": "查看亚马逊结算概览"},
    )

    assert plan.metric == "overview"
    assert plan.start_date is None
    assert plan.end_date is None


def test_query_plan_injects_tenant_and_skips_seller_parameter():
    statement, parameters = AmazonFinanceQueryTool("postgresql://unused")._statement(
        AmazonFinanceQueryPlan(metric="overview"),
        "tenant-a",
    )
    sql_text = statement.as_string(None)

    assert "tenant_id = %s" in sql_text
    assert "amazon_finance_released_transactions" in sql_text
    assert "WHERE t.seller_id" not in sql_text
    assert parameters == ["tenant-a"]


def test_query_plan_injects_binding_marketplace_ids():
    statement, parameters = AmazonFinanceQueryTool("postgresql://unused")._statement(
        AmazonFinanceQueryPlan(metric="overview"),
        "tenant-a",
        ("ATVPDKIKX0DER",),
    )
    sql_text = statement.as_string(None)

    assert "marketplace_id = ANY(%s)" in sql_text
    assert parameters == ["tenant-a", ["ATVPDKIKX0DER"]]
