from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


CurrencyCode = Literal[
    "",
    "CNY",
    "USD",
    "EUR",
    "JPY",
    "AUD",
    "CAD",
    "MXN",
    "GBP",
    "INR",
    "AED",
    "SGD",
    "SAR",
    "BRL",
    "SEK",
    "PLN",
    "TRY",
    "HKD",
]

SearchDateField = Literal[
    "posted_date_locale",
    "fund_transfer_datetime_locale",
    "shipment_datetime_locale",
    "order_datetime_locale",
    "accounting_time",
]


class LingXingIntegrationConfig(BaseModel):
    app_id: str = ""
    app_secret: str = ""
    base_url: str = "https://openapi.lingxing.com"


class LingXingProfitQueryRequest(BaseModel):
    question: str = Field(min_length=2, max_length=2000)


class LingXingProfitQueryPlan(BaseModel):
    start_date: date
    end_date: date
    currency_code: CurrencyCode = ""
    offset: int = Field(default=0, ge=0)
    length: int = Field(default=20, ge=1, le=1000)
    sids: list[int] = Field(default_factory=list)
    search_date_field: SearchDateField = "posted_date_locale"
    order_status: str = "Disbursed"

    @model_validator(mode="after")
    def validate_dates(self) -> "LingXingProfitQueryPlan":
        if self.start_date > self.end_date:
            raise ValueError("start_date must not be after end_date")
        return self


class LingXingProfitQueryResponse(BaseModel):
    question: str
    plan: LingXingProfitQueryPlan
    columns: list[str]
    rows: list[dict[str, Any]]
    summary: str
    total: int
    data_scope: str = "领星利润报表 · 订单维度 transaction 视图"
