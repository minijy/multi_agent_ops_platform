from __future__ import annotations

from typing import Any

from ...integrations.kingdee.client import KingdeeClient, KingdeeClientError
from .domain import DocumentType, KingdeeQueryPlan


class KingdeeQueryError(RuntimeError):
    pass


DOCUMENT_META: dict[DocumentType, dict[str, str]] = {
    "sale_order": {
        "form_id": "SAL_SaleOrder",
        "label": "销售订单",
        "field_keys": (
            "FBillNo,FDate,FCustId.FName,FSaleOrgId.FName,"
            "FDocumentStatus,FBillAllAmount,FNote"
        ),
        "date_field": "FDate",
    },
    "sale_outstock": {
        "form_id": "SAL_OUTSTOCK",
        "label": "销售出库单",
        "field_keys": (
            "FBillNo,FDate,FCustId.FName,FStockOrgId.FName,"
            "FDocumentStatus,FRealQty,FBillAllAmount"
        ),
        "date_field": "FDate",
    },
    "ar_receivable": {
        "form_id": "AR_receivable",
        "label": "应收单",
        "field_keys": (
            "FBillNo,FDate,FCUSTOMERID.FName,FDocumentStatus,"
            "FALLAMOUNTFOR,FCURRENCYID.FName,FRemark"
        ),
        "date_field": "FDate",
    },
    "ar_expense_receivable": {
        "form_id": "AR_OtherRecAble",
        "label": "费用应收单",
        "field_keys": (
            "FBillNo,FDate,FCUSTOMERID.FName,FDocumentStatus,"
            "FALLAMOUNTFOR,FCURRENCYID.FName,FRemark"
        ),
        "date_field": "FDate",
    },
}


def document_label(document_type: DocumentType) -> str:
    return DOCUMENT_META[document_type]["label"]


def form_id_for(document_type: DocumentType) -> str:
    return DOCUMENT_META[document_type]["form_id"]


def _escape_filter(value: str) -> str:
    return value.replace("'", "''")


def build_filter_string(plan: KingdeeQueryPlan) -> str:
    meta = DOCUMENT_META[plan.document_type]
    date_field = meta["date_field"]
    parts = [
        f"{date_field} >= '{plan.start_date.isoformat()}'",
        f"{date_field} <= '{plan.end_date.isoformat()}'",
    ]
    bill_no = plan.bill_no.strip()
    if bill_no:
        parts.append(f"FBillNo = '{_escape_filter(bill_no)}'")
    extra = plan.extra_filter.strip()
    if extra:
        parts.append(extra)
    return " and ".join(parts)


def _rows_to_dicts(field_keys: str, raw_rows: list[list[Any]]) -> list[dict[str, Any]]:
    columns = [item.strip() for item in field_keys.split(",") if item.strip()]
    rows: list[dict[str, Any]] = []
    for raw in raw_rows:
        row: dict[str, Any] = {}
        for index, column in enumerate(columns):
            row[column] = raw[index] if index < len(raw) else None
        rows.append(row)
    return rows


class KingdeeQueryTool:
    def execute(
        self,
        client: KingdeeClient,
        plan: KingdeeQueryPlan,
    ) -> tuple[list[dict[str, Any]], str, str, list[str]]:
        meta = DOCUMENT_META[plan.document_type]
        field_keys = meta["field_keys"]
        filter_string = build_filter_string(plan)
        try:
            raw_rows = client.execute_bill_query(
                form_id=meta["form_id"],
                field_keys=field_keys,
                filter_string=filter_string,
                start_row=plan.start_row,
                limit=plan.limit,
            )
        except KingdeeClientError as exc:
            raise KingdeeQueryError(str(exc)) from exc
        rows = _rows_to_dicts(field_keys, raw_rows)
        columns = [item.strip() for item in field_keys.split(",") if item.strip()]
        return rows, meta["label"], meta["form_id"], columns
