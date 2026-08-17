from datetime import date

from ops_agent.workflows.profit_report.domain import ProfitReportQueryPlan
from ops_agent.workflows.profit_report.query_tool import ProfitReportQueryTool


def test_profit_report_plan_validation():
    plan = ProfitReportQueryPlan(
        metric="overview",
        start_date=date(2024, 9, 1),
        end_date=date(2024, 9, 30),
        currency_code="USD",
    )
    assert plan.metric == "overview"


def test_profit_report_query_tool_builds_filters(monkeypatch):
    captured: dict = {}

    class FakeCursor:
        def __init__(self, mode: str):
            self.mode = mode

        def execute(self, statement, parameters=None):
            if self.mode == "query":
                captured["statement"] = str(statement)
                captured["parameters"] = parameters

        def fetchall(self):
            if self.mode == "query":
                return [
                    {
                        "row_count": 2,
                        "gross_profit_total": "100",
                        "settlement_total": "80",
                        "product_sales_total": "200",
                        "first_posted_at": None,
                        "last_posted_at": None,
                        "currency_code": "USD",
                    }
                ]
            return []

        def fetchone(self):
            if self.mode == "table_stats":
                return {
                    "total": 10,
                    "first_posted_at": date(2026, 1, 1),
                    "last_posted_at": date(2026, 7, 31),
                }
            return {"total": 2}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class FakeConnection:
        def __init__(self):
            self.calls = 0

        def cursor(self):
            self.calls += 1
            if self.calls == 1:
                return FakeCursor("settings")
            if self.calls == 2:
                return FakeCursor("table_stats")
            if self.calls == 3:
                return FakeCursor("query")
            return FakeCursor("count")

        def transaction(self):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(
        "ops_agent.workflows.profit_report.query_tool.connect",
        lambda *_args, **_kwargs: FakeConnection(),
    )
    tool = ProfitReportQueryTool("postgresql://example")
    plan = ProfitReportQueryPlan(
        metric="overview",
        start_date=date(2024, 9, 1),
        end_date=date(2024, 9, 3),
        currency_code="USD",
    )
    rows, total = tool.execute(plan)
    assert total == 2
    assert rows[0]["row_count"] == 2
    assert "currency_code = %s" in captured["statement"]


def test_profit_report_query_tool_reports_date_range_when_empty(monkeypatch):
    from ops_agent.workflows.profit_report.query_tool import ProfitReportQueryError

    class FakeCursor:
        def __init__(self, mode: str):
            self.mode = mode

        def execute(self, statement, parameters=None):
            return None

        def fetchall(self):
            return []

        def fetchone(self):
            if self.mode == "table_stats":
                return {
                    "total": 10,
                    "first_posted_at": date(2026, 1, 1),
                    "last_posted_at": date(2026, 7, 31),
                }
            return {"total": 0}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class FakeConnection:
        def __init__(self):
            self.calls = 0

        def cursor(self):
            self.calls += 1
            if self.calls == 1:
                return FakeCursor("settings")
            if self.calls == 2:
                return FakeCursor("table_stats")
            if self.calls == 3:
                return FakeCursor("query")
            return FakeCursor("count")

        def transaction(self):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(
        "ops_agent.workflows.profit_report.query_tool.connect",
        lambda *_args, **_kwargs: FakeConnection(),
    )
    tool = ProfitReportQueryTool("postgresql://example")
    plan = ProfitReportQueryPlan(
        metric="msku",
        start_date=date(2022, 1, 1),
        end_date=date(2022, 12, 31),
    )
    try:
        tool.execute(plan)
    except ProfitReportQueryError as exc:
        message = str(exc)
        assert "指定日期" in message
        assert "2026-01-01" in message
    else:
        raise AssertionError("expected ProfitReportQueryError")
