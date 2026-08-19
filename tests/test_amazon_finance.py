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


def test_query_plan_covers_all_connection_data_without_seller_parameter():
    statement, parameters = AmazonFinanceQueryTool("postgresql://unused")._statement(
        AmazonFinanceQueryPlan(metric="overview")
    )
    sql_text = statement.as_string(None)

    assert "WHERE t.seller_id" not in sql_text
    assert "GROUP BY t.currency_code" in sql_text
    assert parameters == []
