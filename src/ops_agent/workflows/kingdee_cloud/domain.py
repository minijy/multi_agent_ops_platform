from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


DocumentType = Literal[
    "sale_order",
    "sale_outstock",
    "ar_receivable",
    "ar_expense_receivable",
]


class KingdeeIntegrationConfig(BaseModel):
    server_url: str = ""
    acct_id: str = ""
    app_id: str = ""
    app_secret: str = ""
    username: str = ""
    lcid: int = Field(default=2052, ge=1, le=9999)


class KingdeeQueryRequest(BaseModel):
    question: str = Field(min_length=2, max_length=2000)


class KingdeeQueryPlan(BaseModel):
    document_type: DocumentType
    start_date: date
    end_date: date
    bill_no: str = ""
    limit: int = Field(default=50, ge=1, le=1000)
    start_row: int = Field(default=0, ge=0)
    extra_filter: str = ""

    @model_validator(mode="after")
    def validate_dates(self) -> "KingdeeQueryPlan":
        if self.start_date > self.end_date:
            raise ValueError("start_date must not be after end_date")
        return self


class KingdeeQueryResponse(BaseModel):
    question: str
    plan: KingdeeQueryPlan
    document_label: str
    form_id: str
    columns: list[str]
    rows: list[dict[str, Any]]
    summary: str
    total: int
    data_scope: str = "金蝶云星空 · ExecuteBillQuery"
