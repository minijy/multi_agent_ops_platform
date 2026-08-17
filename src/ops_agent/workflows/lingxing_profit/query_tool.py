from __future__ import annotations

from typing import Any

from ...integrations.lingxing.client import LingXingClient, LingXingClientError
from .domain import LingXingProfitQueryPlan


class LingXingProfitQueryError(RuntimeError):
    pass


DISPLAY_COLUMNS = [
    "storeName",
    "country",
    "postedDatetimeLocale",
    "orderId",
    "msku",
    "asin",
    "description",
    "currencyCode",
    "productSales",
    "sellingFees",
    "fbaFees",
    "other",
    "settlementTotal",
    "settlementGrossProfit",
    "settlementGrossProfitRate",
    "eventSource",
    "settlementStatus",
    "fundTransferStatus",
]


class LingXingProfitQueryTool:
    def execute(
        self,
        client: LingXingClient,
        plan: LingXingProfitQueryPlan,
    ) -> tuple[list[dict[str, Any]], int]:
        body: dict[str, Any] = {
            "offset": plan.offset,
            "length": plan.length,
            "startDate": plan.start_date.isoformat(),
            "endDate": plan.end_date.isoformat(),
            "searchDateField": plan.search_date_field,
            "orderStatus": plan.order_status,
        }
        if plan.currency_code:
            body["currencyCode"] = plan.currency_code
        if plan.sids:
            body["sids"] = plan.sids
        try:
            records, total = client.profit_report_order_transactions(body)
        except LingXingClientError as exc:
            raise LingXingProfitQueryError(str(exc)) from exc
        rows = [{column: record.get(column) for column in DISPLAY_COLUMNS} for record in records]
        return rows, total
