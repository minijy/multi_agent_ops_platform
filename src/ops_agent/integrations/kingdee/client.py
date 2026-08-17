from __future__ import annotations

import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from http.cookiejar import CookieJar
from typing import Any


VALIDATE_USER = (
    "Kingdee.BOS.WebApi.ServicesStub.AuthService.ValidateUser.common.kdsvc"
)
EXECUTE_BILL_QUERY = (
    "Kingdee.BOS.WebApi.ServicesStub.DynamicFormService."
    "ExecuteBillQuery.common.kdsvc"
)


class KingdeeClientError(RuntimeError):
    pass


@dataclass
class KingdeeCredentials:
    server_url: str
    acct_id: str
    app_id: str
    app_secret: str
    username: str
    lcid: int = 2052


@dataclass
class _SessionState:
    logged_in: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)


class KingdeeClient:
    """Minimal private-cloud K3Cloud WebAPI client (DynamicFormService)."""

    def __init__(
        self,
        credentials: KingdeeCredentials,
        *,
        timeout_seconds: float = 45.0,
    ) -> None:
        self.credentials = credentials
        self.timeout_seconds = timeout_seconds
        self._session = _SessionState()
        self._cookie_jar = CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._cookie_jar)
        )

    def _base_url(self) -> str:
        raw = self.credentials.server_url.strip().rstrip("/")
        if not raw:
            raise KingdeeClientError("金蝶服务地址未配置")
        lowered = raw.lower()
        if lowered.endswith("/k3cloud"):
            return raw
        return f"{raw}/K3Cloud"

    def _service_url(self, service: str) -> str:
        return f"{self._base_url()}/{service}"

    def _post(
        self,
        service: str,
        payload: dict[str, Any] | list[Any],
        *,
        as_json: bool = True,
    ) -> Any:
        url = self._service_url(service)
        headers: dict[str, str] = {}
        body: bytes | None
        if as_json:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json;charset=UTF-8"
        else:
            body = urllib.parse.urlencode(payload).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with self._opener.open(request, timeout=self.timeout_seconds) as response:
                text = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise KingdeeClientError(f"HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise KingdeeClientError(f"网络请求失败: {exc.reason}") from exc
        if not text.strip():
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise KingdeeClientError(f"金蝶 API 返回非 JSON: {text[:300]}") from exc

    def login(self) -> None:
        cred = self.credentials
        if not all(
            [
                cred.acct_id.strip(),
                cred.username.strip(),
                cred.app_id.strip(),
                cred.app_secret.strip(),
            ]
        ):
            raise KingdeeClientError("金蝶 WebAPI 凭证不完整")
        payload = {
            "acctid": cred.acct_id.strip(),
            "username": cred.username.strip(),
            "appid": cred.app_id.strip(),
            "appsecret": cred.app_secret.strip(),
            "lcid": cred.lcid,
        }
        result = self._post(VALIDATE_USER, payload, as_json=False)
        if isinstance(result, dict):
            login_result_type = result.get("LoginResultType")
            if login_result_type not in {1, True, "1"}:
                message = (
                    result.get("Message")
                    or result.get("message")
                    or json.dumps(result, ensure_ascii=False)
                )
                raise KingdeeClientError(f"金蝶登录失败: {message}")
        self._session.logged_in = True

    def _ensure_login(self) -> None:
        with self._session.lock:
            if not self._session.logged_in:
                self.login()

    @staticmethod
    def _unwrap_query_result(result: Any) -> list[list[Any]]:
        if isinstance(result, list):
            return [row for row in result if isinstance(row, list)]
        if not isinstance(result, dict):
            raise KingdeeClientError("金蝶查询响应格式异常")
        if "Result" in result:
            inner = result["Result"]
            if isinstance(inner, list):
                return [row for row in inner if isinstance(row, list)]
            if isinstance(inner, dict):
                status = inner.get("ResponseStatus") or {}
                if status.get("IsSuccess") is False:
                    errors = status.get("Errors") or []
                    message = errors[0].get("Message") if errors else str(inner)
                    raise KingdeeClientError(f"金蝶查询失败: {message}")
                rows = inner.get("Result") or inner.get("Data") or inner.get("Rows")
                if isinstance(rows, list):
                    return [row for row in rows if isinstance(row, list)]
        status = result.get("ResponseStatus") or {}
        if status.get("IsSuccess") is False:
            errors = status.get("Errors") or []
            message = errors[0].get("Message") if errors else str(result)
            raise KingdeeClientError(f"金蝶查询失败: {message}")
        raise KingdeeClientError("金蝶查询响应格式异常")

    def execute_bill_query(
        self,
        *,
        form_id: str,
        field_keys: str,
        filter_string: str = "",
        order_string: str = "",
        start_row: int = 0,
        limit: int = 100,
    ) -> list[list[Any]]:
        self._ensure_login()
        query_payload = {
            "FormId": form_id,
            "FieldKeys": field_keys,
            "FilterString": filter_string,
            "OrderString": order_string,
            "TopRowCount": 0,
            "StartRow": start_row,
            "Limit": limit,
            "SubSystemId": "",
        }
        result = self._post(EXECUTE_BILL_QUERY, query_payload, as_json=True)
        return self._unwrap_query_result(result)
