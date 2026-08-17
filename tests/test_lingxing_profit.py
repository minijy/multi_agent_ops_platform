from datetime import date

from ops_agent.integrations.lingxing.client import LingXingClient
from ops_agent.integrations.lingxing.sign import generate_sign, _format_params, _md5_upper
from ops_agent.workflows.lingxing_profit.domain import LingXingProfitQueryPlan
from ops_agent.workflows.lingxing_profit.query_tool import LingXingProfitQueryTool


def test_sign_format_skips_empty_and_sorts_keys():
    canonical = _format_params(
        {
            "z": "1",
            "a": "",
            "m": {"b": 2, "a": 1},
            "access_token": "token",
        }
    )
    assert canonical.startswith("access_token=token")
    assert 'm={"a":1,"b":2}' in canonical
    assert "a=" not in canonical


def test_sign_is_deterministic():
    params = {
        "app_key": "demo-app",
        "access_token": "abc",
        "timestamp": "1700000000",
        "startDate": "2024-09-01",
    }
    assert generate_sign("demo-app", params) == generate_sign("demo-app", params)
    assert _md5_upper(_format_params(params)) == _md5_upper(_format_params(params))


def test_query_tool_maps_response_columns():
    class FakeClient:
        def profit_report_order_transactions(self, body):
            assert body["startDate"] == "2024-09-01"
            assert body["currencyCode"] == "USD"
            return (
                [
                    {
                        "storeName": "测试店",
                        "country": "美国",
                        "postedDatetimeLocale": "2024-09-02 10:00:00",
                        "orderId": "123",
                        "msku": "MSKU-1",
                        "asin": "B001",
                        "description": "Order",
                        "currencyCode": "USD",
                        "productSales": 100.0,
                        "sellingFees": -15.0,
                        "fbaFees": -5.0,
                        "other": 0.0,
                        "settlementTotal": 80.0,
                        "settlementGrossProfit": 50.0,
                        "settlementGrossProfitRate": 0.5,
                        "eventSource": "Order",
                        "settlementStatus": "Closed",
                        "fundTransferStatus": "Succeeded",
                    }
                ],
                1,
            )

    plan = LingXingProfitQueryPlan(
        start_date=date(2024, 9, 1),
        end_date=date(2024, 9, 3),
        currency_code="USD",
    )
    rows, total = LingXingProfitQueryTool().execute(FakeClient(), plan)
    assert total == 1
    assert rows[0]["storeName"] == "测试店"
    assert rows[0]["settlementGrossProfit"] == 50.0


def test_client_builds_profit_request(monkeypatch):
    captured: dict = {}

    def fake_request_json(self, method, url, *, params=None, body=None, headers=None):
        captured["method"] = method
        captured["url"] = url
        captured["params"] = params
        captured["body"] = body
        if url.endswith("/access-token"):
            return {
                "code": 200,
                "data": {
                    "access_token": "token-1",
                    "refresh_token": "refresh-1",
                    "expires_in": 7200,
                },
            }
        return {
            "code": 0,
            "message": "success",
            "data": {"records": [], "total": 0},
        }

    monkeypatch.setattr(LingXingClient, "_request_json", fake_request_json)
    client = LingXingClient("app-id", "app-secret")
    records, total = client.profit_report_order_transactions(
        {"startDate": "2024-09-01", "endDate": "2024-09-03"}
    )
    assert total == 0
    assert records == []
    assert captured["params"]["app_key"] == "app-id"
    assert captured["params"]["access_token"] == "token-1"
    assert "sign" in captured["params"]
    assert captured["url"].endswith("/basicOpen/finance/profitReport/order/transcation/list")
