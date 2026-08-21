from datetime import date, timedelta

import pytest

from ops_agent.query_policy import QueryPolicyError, enforce_query_plan
from ops_agent.workflows.amazon_finance.domain import AmazonFinanceQueryPlan
from ops_agent.workflows.kingdee_cloud.domain import KingdeeQueryPlan
from ops_agent.workflows.lingxing_profit.domain import LingXingProfitQueryPlan
from ops_agent.workflows.profit_report.domain import ProfitReportQueryPlan


def test_viewer_cannot_query_sku_grain():
    plan = AmazonFinanceQueryPlan(
        metric="sku",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 1, 31),
    )
    with pytest.raises(QueryPolicyError, match="cannot query"):
        enforce_query_plan("amazon_finance_query", plan, "viewer")


def test_operator_cannot_query_order_grain():
    plan = ProfitReportQueryPlan(
        metric="order",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 1, 31),
    )
    with pytest.raises(QueryPolicyError, match="cannot query"):
        enforce_query_plan("profit_report_query", plan, "operator")


def test_detail_metric_fills_missing_dates():
    plan = AmazonFinanceQueryPlan(metric="daily")
    resolved = enforce_query_plan("amazon_finance_query", plan, "admin")
    assert resolved.start_date is not None
    assert resolved.end_date == date.today()
    assert (resolved.end_date - resolved.start_date).days == 90


def test_window_longer_than_366_days_is_rejected():
    plan = AmazonFinanceQueryPlan(
        metric="fee",
        start_date=date(2024, 1, 1),
        end_date=date(2026, 1, 10),
        limit=10,
    )
    with pytest.raises(QueryPolicyError, match="exceeds 366"):
        enforce_query_plan("amazon_finance_query", plan, "admin")


def test_lingxing_length_cap():
    plan = LingXingProfitQueryPlan(
        start_date=date.today() - timedelta(days=10),
        end_date=date.today(),
        length=100,
    )
    assert enforce_query_plan("lingxing_profit_query", plan, "operator") is plan


def test_kingdee_limit_cap_rejects_over_schema():
    with pytest.raises(Exception):
        KingdeeQueryPlan(
            document_type="sale_order",
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 31),
            limit=500,
        )
