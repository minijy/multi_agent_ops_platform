from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


FinanceMetric = Literal[
    "overview",
    "daily",
    "transaction_type",
    "fee",
    "sku",
    "settlement",
]


class AmazonFinanceQueryRequest(BaseModel):
    question: str = Field(min_length=2, max_length=2000)
    seller_id: str | None = Field(default=None, max_length=128)


class AmazonFinanceQueryPlan(BaseModel):
    metric: FinanceMetric
    start_date: date | None = None
    end_date: date | None = None
    limit: int = Field(default=20, ge=1, le=100)

    @model_validator(mode="after")
    def validate_dates(self) -> "AmazonFinanceQueryPlan":
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("start_date must not be after end_date")
        return self


class AmazonFinanceQueryResponse(BaseModel):
    question: str
    seller_id: str
    plan: AmazonFinanceQueryPlan
    columns: list[str]
    rows: list[dict[str, Any]]
    summary: str
    data_scope: str = "RELEASED only"
