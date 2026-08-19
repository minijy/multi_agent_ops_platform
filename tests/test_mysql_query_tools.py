from __future__ import annotations

from datetime import date

from ops_agent.workflows.amazon_finance.domain import AmazonFinanceQueryPlan
from ops_agent.workflows.amazon_finance.query_tool import AmazonFinanceQueryTool
from ops_agent.workflows.profit_report.domain import ProfitReportQueryPlan
from ops_agent.workflows.profit_report.query_tool import ProfitReportQueryTool


def test_amazon_finance_mysql_daily_statement_uses_mysql_date_function():
    tool = AmazonFinanceQueryTool("mysql://reader@db/analytics", engine="mysql")
    statement, parameters = tool._mysql_statement(
        AmazonFinanceQueryPlan(
            metric="daily",
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 31),
            limit=25,
        )
    )

    assert "DATE(t.posted_at)" in statement
    assert "AT TIME ZONE" not in statement
    assert "::date" not in statement
    assert parameters == [date(2026, 1, 1), date(2026, 2, 1), 25]


def test_profit_report_mysql_statement_keeps_filters_parameterized():
    tool = ProfitReportQueryTool("mysql://reader@db/analytics", engine="mysql")
    statement, parameters = tool._mysql_statement(
        ProfitReportQueryPlan(
            metric="store",
            start_date=date(2026, 2, 1),
            end_date=date(2026, 2, 28),
            store_name="Canada",
            currency_code="cad",
            limit=10,
        )
    )

    assert "FROM lingxing_profit_order_transactions" in statement
    assert "currency_code = %s" in statement
    assert "store_name = %s" in statement
    assert parameters == [
        date(2026, 2, 1),
        date(2026, 3, 1),
        "CAD",
        "Canada",
        10,
    ]
