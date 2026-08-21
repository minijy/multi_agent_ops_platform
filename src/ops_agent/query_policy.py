from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Literal

from pydantic import BaseModel


QueryRole = Literal["viewer", "operator", "approver", "admin"]

ROLE_RANK = {
    "viewer": 0,
    "operator": 1,
    "approver": 1,
    "admin": 2,
}

MAX_SPAN_DAYS = 366
DEFAULT_WINDOW_DAYS = 90
DETAIL_METRICS = frozenset({"sku", "settlement", "msku", "order"})


class QueryPolicyError(PermissionError):
    """Raised when a query plan violates the metric catalog."""


@dataclass(frozen=True)
class MetricSpec:
    min_role: QueryRole
    max_limit: int
    dates_required: bool = False
    max_span_days: int = MAX_SPAN_DAYS
    limit_field: str = "limit"


METRIC_CATALOG: dict[str, dict[str, MetricSpec]] = {
    "amazon_finance_query": {
        "overview": MetricSpec("viewer", max_limit=20),
        "daily": MetricSpec("operator", max_limit=100, dates_required=True),
        "transaction_type": MetricSpec("operator", max_limit=50, dates_required=True),
        "fee": MetricSpec("operator", max_limit=50, dates_required=True),
        "sku": MetricSpec("admin", max_limit=50, dates_required=True),
        "settlement": MetricSpec("admin", max_limit=50, dates_required=True),
    },
    "profit_report_query": {
        "overview": MetricSpec("viewer", max_limit=20),
        "daily": MetricSpec("operator", max_limit=100, dates_required=True),
        "store": MetricSpec("operator", max_limit=50, dates_required=True),
        "event_source": MetricSpec("operator", max_limit=50, dates_required=True),
        "msku": MetricSpec("admin", max_limit=50, dates_required=True),
        "order": MetricSpec("admin", max_limit=50, dates_required=True),
    },
    "lingxing_profit_query": {
        "orders": MetricSpec(
            "operator",
            max_limit=100,
            dates_required=True,
            limit_field="length",
        ),
    },
    "kingdee_cloud_query": {
        "sale_order": MetricSpec("operator", max_limit=100, dates_required=True),
        "sale_outstock": MetricSpec("operator", max_limit=100, dates_required=True),
        "ar_receivable": MetricSpec("operator", max_limit=100, dates_required=True),
        "ar_expense_receivable": MetricSpec("operator", max_limit=100, dates_required=True),
    },
}


def _metric_key(tool_name: str, plan: BaseModel) -> str:
    if tool_name == "lingxing_profit_query":
        return "orders"
    if tool_name == "kingdee_cloud_query":
        return str(getattr(plan, "document_type"))
    return str(getattr(plan, "metric"))


def _limit_value(plan: BaseModel, field: str) -> int:
    return int(getattr(plan, field))


def enforce_query_plan(tool_name: str, plan: BaseModel, role: str) -> Any:
    """Clamp a model-produced plan to the closed metric catalog.

    Dates that are missing on detail metrics are filled from today; oversized
    windows and grain the role cannot use are rejected rather than silently
    truncated, so the caller sees the true authorized contract.
    """
    catalog = METRIC_CATALOG.get(tool_name)
    if catalog is None:
        return plan
    metric = _metric_key(tool_name, plan)
    spec = catalog.get(metric)
    if spec is None:
        raise QueryPolicyError(f"unknown metric for {tool_name}: {metric}")
    if ROLE_RANK.get(role, 0) < ROLE_RANK[spec.min_role]:
        raise QueryPolicyError(
            f"role {role} cannot query {tool_name} metric {metric}"
        )

    updates: dict[str, Any] = {}
    start = getattr(plan, "start_date", None)
    end = getattr(plan, "end_date", None)
    today = date.today()
    if spec.dates_required and (start is None or end is None):
        updates["end_date"] = end or today
        updates["start_date"] = start or (updates["end_date"] - timedelta(days=DEFAULT_WINDOW_DAYS))
        start = updates["start_date"]
        end = updates["end_date"]
    if start and end and (end - start).days > spec.max_span_days:
        raise QueryPolicyError(
            f"query window exceeds {spec.max_span_days} days for {metric}"
        )
    current_limit = _limit_value(plan, spec.limit_field)
    if current_limit > spec.max_limit:
        raise QueryPolicyError(
            f"{spec.limit_field} exceeds {spec.max_limit} for {tool_name} metric {metric}"
        )
    if not updates:
        return plan
    return plan.model_copy(update=updates)
