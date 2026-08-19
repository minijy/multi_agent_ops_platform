from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


ProfitMetric = Literal[
    "overview",
    "daily",
    "store",
    "msku",
    "order",
    "event_source",
]


class ProfitReportQueryPlan(BaseModel):
    metric: ProfitMetric
    start_date: date | None = None
    end_date: date | None = None
    currency_code: str | None = Field(default=None, max_length=8)
    store_name: str | None = Field(default=None, max_length=128)
    limit: int = Field(default=20, ge=1, le=200)

    @model_validator(mode="after")
    def validate_dates(self) -> "ProfitReportQueryPlan":
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("start_date must not be after end_date")
        return self


class ProfitReportQueryRequest(BaseModel):
    question: str = Field(min_length=2, max_length=2000)
    currency_code: str | None = Field(default=None, max_length=8)
    plan: ProfitReportQueryPlan | None = None


class ProfitReportQueryResponse(BaseModel):
    question: str
    plan: ProfitReportQueryPlan
    columns: list[str]
    rows: list[dict[str, Any]]
    summary: str
    total_rows: int
    data_scope: str = "领星利润分析数据（分析仓）"
