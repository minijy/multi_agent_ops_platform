from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from .sign import generate_sign


DEFAULT_BASE_URL = "https://openapi.lingxing.com"
PROFIT_ORDER_TRANSACTION_PATH = (
    "/basicOpen/finance/profitReport/order/transcation/list"
)


class LingXingClientError(RuntimeError):
    pass


@dataclass
class _TokenCache:
    access_token: str = ""
    refresh_token: str = ""
    expires_at: float = 0.0


class LingXingClient:
    def __init__(
        self,
        app_id: str,
        app_secret: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.app_id = app_id.strip()
        self.app_secret = app_secret.strip()
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._token = _TokenCache()
        self._lock = threading.Lock()

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        query = urllib.parse.urlencode(params or {}, doseq=True)
        full_url = f"{url}?{query}" if query else url
        data = None
        req_headers = dict(headers or {})
        if body is not None:
            data = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
            req_headers.setdefault("Content-Type", "application/json;charset=UTF-8")
        request = urllib.request.Request(full_url, data=data, headers=req_headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise LingXingClientError(f"HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise LingXingClientError(f"网络请求失败: {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise LingXingClientError("领星 API 返回非 JSON 响应") from exc
        if not isinstance(payload, dict):
            raise LingXingClientError("领星 API 响应格式异常")
        return payload

    def _ensure_access_token(self) -> str:
        with self._lock:
            if self._token.access_token and time.time() < self._token.expires_at - 60:
                return self._token.access_token
            url = f"{self.base_url}/api/auth-server/oauth/access-token"
            payload = self._request_json(
                "POST",
                url,
                params={"appId": self.app_id, "appSecret": self.app_secret},
            )
            code = payload.get("code")
            if code not in {0, 200}:
                message = payload.get("message") or payload.get("msg") or "unknown"
                raise LingXingClientError(f"获取 access_token 失败: {message}")
            data = payload.get("data")
            if not isinstance(data, dict) or not data.get("access_token"):
                raise LingXingClientError("获取 access_token 失败: 响应缺少 data.access_token")
            self._token.access_token = str(data["access_token"])
            self._token.refresh_token = str(data.get("refresh_token") or "")
            expires_in = int(data.get("expires_in") or 7200)
            self._token.expires_at = time.time() + max(expires_in, 60)
            return self._token.access_token

    def request(
        self,
        path: str,
        *,
        method: str = "POST",
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        access_token = self._ensure_access_token()
        sign_params: dict[str, Any] = {}
        if body:
            sign_params.update(body)
        sign_params.update(
            {
                "app_key": self.app_id,
                "access_token": access_token,
                "timestamp": str(int(time.time())),
            }
        )
        sign = generate_sign(self.app_id, sign_params)
        query = {
            "app_key": self.app_id,
            "access_token": access_token,
            "timestamp": sign_params["timestamp"],
            "sign": sign,
        }
        url = f"{self.base_url}{path}"
        payload = self._request_json(method, url, params=query, body=body)
        if payload.get("code") != 0:
            message = payload.get("message") or payload.get("msg") or "unknown"
            raise LingXingClientError(f"领星 API 错误: {message}")
        return payload

    def profit_report_order_transactions(self, body: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
        payload = self.request(PROFIT_ORDER_TRANSACTION_PATH, body=body)
        data = payload.get("data")
        if not isinstance(data, dict):
            return [], 0
        records = data.get("records") or []
        total = int(data.get("total") or payload.get("total") or len(records))
        if not isinstance(records, list):
            return [], total
        normalized = [item for item in records if isinstance(item, dict)]
        return normalized, total
