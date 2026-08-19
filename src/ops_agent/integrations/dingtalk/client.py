from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


DEFAULT_BASE_URL = "https://api.dingtalk.com"
ACCESS_TOKEN_PATH = "/v1.0/oauth2/accessToken"
DIRECT_MESSAGE_PATH = "/v1.0/robot/oToMessages/batchSend"
GROUP_MESSAGE_PATH = "/v1.0/robot/groupMessages/send"


class DingTalkClientError(RuntimeError):
    pass


@dataclass
class _TokenCache:
    access_token: str = ""
    expires_at: float = 0.0


class DingTalkClient:
    """Enterprise-internal DingTalk OpenAPI client with connection-local token cache."""

    def __init__(
        self,
        app_key: str,
        app_secret: str,
        robot_code: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = 20.0,
    ) -> None:
        self.app_key = app_key.strip()
        self.app_secret = app_secret.strip()
        self.robot_code = robot_code.strip()
        self.base_url = self._validated_base_url(base_url)
        self.timeout_seconds = timeout_seconds
        self._token = _TokenCache()
        self._lock = threading.Lock()

    @staticmethod
    def _validated_base_url(value: str) -> str:
        normalized = str(value or DEFAULT_BASE_URL).strip().rstrip("/")
        parsed = urllib.parse.urlparse(normalized)
        if parsed.scheme != "https" or parsed.hostname != "api.dingtalk.com":
            raise ValueError("钉钉 API Base URL 必须是 https://api.dingtalk.com")
        return normalized

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any],
        access_token: str | None = None,
        query: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        query_string = urllib.parse.urlencode(query or {})
        url = f"{self.base_url}{path}"
        if query_string:
            url = f"{url}?{query_string}"
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "Accept": "application/json",
        }
        if access_token:
            headers["x-acs-dingtalk-access-token"] = access_token
        request = urllib.request.Request(
            url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise DingTalkClientError(f"钉钉 API HTTP {exc.code}: {detail[:1000]}") from exc
        except urllib.error.URLError as exc:
            raise DingTalkClientError(f"钉钉 API 网络请求失败: {exc.reason}") from exc
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError as exc:
            raise DingTalkClientError("钉钉 API 返回非 JSON 响应") from exc
        if not isinstance(payload, dict):
            raise DingTalkClientError("钉钉 API 响应格式异常")
        return payload

    def _ensure_access_token(self) -> str:
        with self._lock:
            now = time.time()
            if self._token.access_token and now < self._token.expires_at - 120:
                return self._token.access_token
            payload = self._request_json(
                "POST",
                ACCESS_TOKEN_PATH,
                body={"appKey": self.app_key, "appSecret": self.app_secret},
            )
            access_token = str(payload.get("accessToken") or "").strip()
            if not access_token:
                message = payload.get("message") or payload.get("code") or "缺少 accessToken"
                raise DingTalkClientError(f"获取钉钉 access token 失败: {message}")
            expires_in = int(payload.get("expireIn") or 7200)
            self._token = _TokenCache(
                access_token=access_token,
                expires_at=now + max(expires_in, 300),
            )
            return access_token

    @staticmethod
    def _message(message_type: str, content: str, title: str | None) -> tuple[str, str]:
        if message_type == "markdown":
            return "sampleMarkdown", json.dumps(
                {"title": (title or "通知").strip(), "text": content},
                ensure_ascii=False,
            )
        return "sampleText", json.dumps({"content": content}, ensure_ascii=False)

    def send_direct_message(
        self,
        *,
        user_id: str,
        content: str,
        message_type: str = "text",
        title: str | None = None,
    ) -> dict[str, Any]:
        msg_key, msg_param = self._message(message_type, content, title)
        return self._request_json(
            "POST",
            DIRECT_MESSAGE_PATH,
            access_token=self._ensure_access_token(),
            body={
                "robotCode": self.robot_code,
                "userIds": [user_id],
                "msgKey": msg_key,
                "msgParam": msg_param,
            },
        )

    def send_group_message(
        self,
        *,
        open_conversation_id: str,
        content: str,
        message_type: str = "text",
        title: str | None = None,
    ) -> dict[str, Any]:
        msg_key, msg_param = self._message(message_type, content, title)
        return self._request_json(
            "POST",
            GROUP_MESSAGE_PATH,
            access_token=self._ensure_access_token(),
            body={
                "robotCode": self.robot_code,
                "openConversationId": open_conversation_id,
                "msgKey": msg_key,
                "msgParam": msg_param,
            },
        )

    def create_todo(
        self,
        *,
        owner_union_id: str,
        subject: str,
        executor_union_ids: list[str],
        description: str = "",
        due_time_ms: int | None = None,
        participant_union_ids: list[str] | None = None,
        source_id: str | None = None,
        detail_url: str | None = None,
        priority: int = 20,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "subject": subject,
            "creatorId": owner_union_id,
            "description": description,
            "executorIds": executor_union_ids,
            "participantIds": participant_union_ids or [],
            "isOnlyShowExecutor": True,
            "priority": priority,
            "notifyConfigs": {"dingNotify": "1"},
        }
        if due_time_ms is not None:
            body["dueTime"] = due_time_ms
        if source_id:
            body["sourceId"] = source_id
        if detail_url:
            body["detailUrl"] = {"appUrl": detail_url, "pcUrl": detail_url}
        return self._request_json(
            "POST",
            f"/v1.0/todo/users/{urllib.parse.quote(owner_union_id, safe='')}/tasks",
            query={"operatorId": owner_union_id},
            access_token=self._ensure_access_token(),
            body=body,
        )
