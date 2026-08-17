from __future__ import annotations

import calendar
import json
import re
from datetime import date
from typing import Any, TypeVar

from pydantic import BaseModel

from .config import Settings
from .workflows.amazon_finance.domain import AmazonFinanceQueryPlan
from .workflows.lingxing_profit.domain import LingXingProfitQueryPlan
from .workflows.profit_report.domain import ProfitReportQueryPlan


T = TypeVar("T", bound=BaseModel)


class ModelGateway:
    def structured(
        self, schema: type[T], *, system_prompt: str, payload: dict[str, Any]
    ) -> T:
        raise NotImplementedError


class MockModelGateway(ModelGateway):
    """Deterministic local model substitute used for development and CI."""

    def structured(
        self, schema: type[T], *, system_prompt: str, payload: dict[str, Any]
    ) -> T:
        objective = str(payload.get("objective", ""))
        lowered = objective.lower()
        if schema is AmazonFinanceQueryPlan:
            if any(word in lowered for word in ("settlement", "结算批次", "结算单")):
                metric = "settlement"
            elif any(word in lowered for word in ("sku", "asin", "商品")):
                metric = "sku"
            elif any(word in lowered for word in ("费用", "费率", "佣金", "fee")):
                metric = "fee"
            elif any(word in lowered for word in ("交易类型", "transaction type", "类型")):
                metric = "transaction_type"
            elif any(word in lowered for word in ("每天", "每日", "按日", "daily", "趋势")):
                metric = "daily"
            else:
                metric = "overview"

            start_date = end_date = None
            iso_dates = re.findall(r"\b(\d{4}-\d{2}-\d{2})\b", objective)
            if iso_dates:
                start_date = iso_dates[0]
                end_date = iso_dates[-1]
            else:
                month_match = re.search(r"(?:(\d{4})年)?(\d{1,2})月", objective)
                if month_match:
                    year = int(month_match.group(1) or date.today().year)
                    month = int(month_match.group(2))
                    start_date = f"{year:04d}-{month:02d}-01"
                    end_date = (
                        f"{year:04d}-{month:02d}-{calendar.monthrange(year, month)[1]:02d}"
                    )
            limit_match = re.search(r"(?:top|前)\s*(\d+)", lowered)
            limit = min(int(limit_match.group(1)), 100) if limit_match else 20
            return schema.model_validate(
                {
                    "metric": metric,
                    "start_date": start_date,
                    "end_date": end_date,
                    "limit": limit,
                }
            )
        if schema is LingXingProfitQueryPlan:
            iso_dates = re.findall(r"\b(\d{4}-\d{2}-\d{2})\b", objective)
            if len(iso_dates) >= 2:
                start_date, end_date = iso_dates[0], iso_dates[-1]
            else:
                month_match = re.search(r"(?:(\d{4})年)?(\d{1,2})月", objective)
                if month_match:
                    year = int(month_match.group(1) or date.today().year)
                    month = int(month_match.group(2))
                    start_date = f"{year:04d}-{month:02d}-01"
                    end_date = (
                        f"{year:04d}-{month:02d}-{calendar.monthrange(year, month)[1]:02d}"
                    )
                else:
                    today = date.today()
                    start_date = f"{today.year:04d}-{today.month:02d}-01"
                    end_date = today.isoformat()
            currency = ""
            for code in ("USD", "CNY", "EUR", "GBP", "JPY"):
                if code.lower() in lowered or code in objective.upper():
                    currency = code
                    break
            length_match = re.search(r"(?:top|前|返回)\s*(\d+)", lowered)
            length = min(int(length_match.group(1)), 1000) if length_match else 20
            return schema.model_validate(
                {
                    "start_date": start_date,
                    "end_date": end_date,
                    "currency_code": currency,
                    "length": length,
                }
            )
        if schema is ProfitReportQueryPlan:
            if any(word in lowered for word in ("店铺", "store")):
                metric = "store"
            elif any(word in lowered for word in ("msku", "sku", "asin")):
                metric = "msku"
            elif any(word in lowered for word in ("订单", "order")):
                metric = "order"
            elif any(word in lowered for word in ("费用类型", "event", "fee")):
                metric = "event_source"
            elif any(word in lowered for word in ("每天", "每日", "按日", "daily", "趋势")):
                metric = "daily"
            else:
                metric = "overview"
            iso_dates = re.findall(r"\b(\d{4}-\d{2}-\d{2})\b", objective)
            if len(iso_dates) >= 2:
                start_date, end_date = iso_dates[0], iso_dates[-1]
            else:
                month_match = re.search(r"(?:(\d{4})年)?(\d{1,2})月", objective)
                if month_match:
                    year = int(month_match.group(1) or date.today().year)
                    month = int(month_match.group(2))
                    start_date = f"{year:04d}-{month:02d}-01"
                    end_date = (
                        f"{year:04d}-{month:02d}-{calendar.monthrange(year, month)[1]:02d}"
                    )
                else:
                    start_date = end_date = None
            currency = ""
            for code in ("USD", "CNY", "EUR", "GBP", "JPY"):
                if code.lower() in lowered or code in objective.upper():
                    currency = code
                    break
            limit_match = re.search(r"(?:top|前|返回)\s*(\d+)", lowered)
            limit = min(int(limit_match.group(1)), 200) if limit_match else 20
            return schema.model_validate(
                {
                    "metric": metric,
                    "start_date": start_date,
                    "end_date": end_date,
                    "currency_code": currency or None,
                    "limit": limit,
                }
            )
        raise TypeError(f"MockModelGateway does not support {schema.__name__}")


class OpenAIModelGateway(ModelGateway):
    def __init__(self, settings: Settings) -> None:
        from langchain_openai import ChatOpenAI

        model_options: dict[str, Any] = {
            "model": settings.model_name,
            "api_key": settings.openai_api_key,
        }
        if settings.model_temperature is not None:
            model_options["temperature"] = settings.model_temperature
        self.model = ChatOpenAI(
            **model_options,
        )

    def structured(
        self, schema: type[T], *, system_prompt: str, payload: dict[str, Any]
    ) -> T:
        structured_model = self.model.with_structured_output(schema)
        return structured_model.invoke(
            [
                ("system", system_prompt),
                ("user", json.dumps(payload, ensure_ascii=False, default=str)),
            ]
        )


class ZhipuModelGateway(ModelGateway):
    """Structured-output bridge for Amazon Finance planning via zai-sdk."""

    def __init__(self, settings: Settings) -> None:
        from zai import ZhipuAiClient

        self.model_name = settings.zhipu_model_name
        self.temperature = settings.model_temperature
        self.client = ZhipuAiClient(
            api_key=settings.zai_api_key,
            base_url=settings.zhipu_base_url,
        )

    def structured(
        self, schema: type[T], *, system_prompt: str, payload: dict[str, Any]
    ) -> T:
        tool_name = "submit_structured_result"
        options: dict[str, Any] = {
            "model": self.model_name,
            "messages": [
                (
                    {
                        "role": "system",
                        "content": (
                            f"{system_prompt}\n"
                            f"必须调用 {tool_name} 返回结果，不要直接输出自然语言。"
                        ),
                    }
                ),
                {
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False, default=str),
                },
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "description": "提交符合指定 JSON Schema 的结构化结果",
                        "parameters": schema.model_json_schema(),
                    },
                }
            ],
            "tool_choice": "auto",
        }
        if self.temperature is not None:
            options["temperature"] = self.temperature
        response = self.client.chat.completions.create(**options)
        message = response.choices[0].message
        if message.tool_calls:
            arguments = message.tool_calls[0].function.arguments
            data = json.loads(arguments) if isinstance(arguments, str) else arguments
            return schema.model_validate(data)
        content = (message.content or "").strip()
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content)
        return schema.model_validate_json(content)


def create_model_gateway(settings: Settings) -> ModelGateway:
    if settings.model_provider == "openai":
        return OpenAIModelGateway(settings)
    if settings.model_provider == "zhipu":
        return ZhipuModelGateway(settings)
    return MockModelGateway()
