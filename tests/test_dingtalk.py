from __future__ import annotations

import json
from datetime import datetime, timezone

from ops_agent.connections import create_connection_registry
from ops_agent.integrations.dingtalk.client import DingTalkClient
from ops_agent.runtime.connectors import ConnectorRuntime, create_tool_bindings
from ops_agent.runtime.dingtalk_tool import register_dingtalk_tools
from ops_agent.runtime.domain import ToolCall
from ops_agent.runtime.tools import (
    ApprovalGuard,
    ConnectorAccessGuard,
    ToolExecutionContext,
    ToolExecutor,
    ToolRegistry,
)


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def test_dingtalk_client_caches_token_and_builds_message_requests(monkeypatch):
    requests = []

    def fake_urlopen(request, timeout):
        requests.append(
            {
                "url": request.full_url,
                "headers": dict(request.header_items()),
                "body": json.loads(request.data.decode("utf-8")),
                "timeout": timeout,
            }
        )
        if request.full_url.endswith("/v1.0/oauth2/accessToken"):
            return _Response({"accessToken": "token-1", "expireIn": 7200})
        return _Response({"processQueryKey": f"message-{len(requests)}"})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = DingTalkClient("app-key", "app-secret", "robot-code")
    direct = client.send_direct_message(user_id="user-1", content="hello")
    group = client.send_group_message(
        open_conversation_id="cid-1",
        content="**release**",
        message_type="markdown",
        title="Release",
    )

    assert direct["processQueryKey"]
    assert group["processQueryKey"]
    assert len([item for item in requests if item["url"].endswith("accessToken")]) == 1
    assert requests[1]["body"] == {
        "robotCode": "robot-code",
        "userIds": ["user-1"],
        "msgKey": "sampleText",
        "msgParam": '{"content": "hello"}',
    }
    assert requests[2]["body"]["openConversationId"] == "cid-1"
    assert requests[2]["body"]["msgKey"] == "sampleMarkdown"
    assert json.loads(requests[2]["body"]["msgParam"])["title"] == "Release"
    assert requests[1]["headers"]["X-acs-dingtalk-access-token"] == "token-1"


def test_dingtalk_client_builds_todo_request(monkeypatch):
    captured = {}

    def fake_request(self, method, path, *, body, access_token=None, query=None):
        captured.update(method=method, path=path, body=body, query=query)
        return {"id": "task-1"}

    client = DingTalkClient("app-key", "app-secret", "robot-code")
    monkeypatch.setattr(client, "_ensure_access_token", lambda: "token")
    monkeypatch.setattr(client, "_request_json", fake_request.__get__(client))
    result = client.create_todo(
        owner_union_id="owner/1",
        subject="Review",
        executor_union_ids=["executor-1"],
        due_time_ms=1_800_000_000_000,
        source_id="source-1",
        detail_url="https://example.com/todo/1",
    )

    assert result["id"] == "task-1"
    assert captured["path"] == "/v1.0/todo/users/owner%2F1/tasks"
    assert captured["query"] == {"operatorId": "owner/1"}
    assert captured["body"]["executorIds"] == ["executor-1"]
    assert captured["body"]["detailUrl"]["pcUrl"].startswith("https://")


class _FakeDingTalkClient:
    def __init__(self):
        self.calls = []

    def send_direct_message(self, **kwargs):
        self.calls.append(("direct", kwargs))
        return {"processQueryKey": "message-1"}

    def send_group_message(self, **kwargs):
        self.calls.append(("group", kwargs))
        return {"processQueryKey": "message-2"}

    def create_todo(self, **kwargs):
        self.calls.append(("todo", kwargs))
        return {"id": "task-1"}


class _FakeDingTalkProvider:
    connector_type = "dingtalk"
    min_interval_seconds = 0.0

    def __init__(self, client):
        self.client = client

    def create_client(self, _values):
        return self.client


def _dingtalk_executor(tmp_path):
    connections = create_connection_registry(
        tmp_path / "connections.json", tmp_path / "secrets.json"
    )
    connection = connections.create(
        tenant_id="tenant-a",
        connector_type="dingtalk",
        name="DingTalk",
        values={
            "app_key": "app-key",
            "app_secret": "app-secret",
            "robot_code": "robot-code",
            "default_todo_owner_union_id": "owner-1",
        },
        resource_scopes={
            "dingtalk_user_ids": ["user-1"],
            "dingtalk_conversation_ids": ["cid-1"],
            "dingtalk_union_ids": ["owner-1", "executor-1"],
        },
    )
    bindings = create_tool_bindings(tmp_path / "bindings.json")
    for tool_name in (
        "dingtalk_send_direct_message",
        "dingtalk_send_group_message",
        "dingtalk_create_todo",
    ):
        bindings.select("tenant-a", tool_name, connection.id, connections)
    fake = _FakeDingTalkClient()
    connectors = ConnectorRuntime(
        connections,
        [_FakeDingTalkProvider(fake)],
        bindings=bindings,
        max_retries=1,
    )
    tools = ToolRegistry()
    register_dingtalk_tools(tools, connectors)
    executor = ToolExecutor(
        tools,
        guards=[ApprovalGuard(), ConnectorAccessGuard(bindings, connections)],
    )
    return executor, fake


def test_dingtalk_tools_require_approval_and_enforce_recipient_scope(tmp_path):
    executor, fake = _dingtalk_executor(tmp_path)
    call = ToolCall(
        call_id="call-1",
        name="dingtalk_send_direct_message",
        arguments={"user_id": "user-1", "content": "hello"},
    )
    denied = executor.execute(
        call,
        ToolExecutionContext(
            session_id="session", tenant_id="tenant-a", user_id="admin"
        ),
    )
    assert denied.ok is False
    assert "requires approval" in denied.error
    assert fake.calls == []

    approved = executor.execute(
        call,
        ToolExecutionContext(
            session_id="session",
            tenant_id="tenant-a",
            user_id="admin",
            approved_call_ids=frozenset({"call-1"}),
        ),
    )
    assert approved.ok is True
    assert approved.output["process_query_key"] == "message-1"

    outside = executor.execute(
        ToolCall(
            call_id="call-2",
            name="dingtalk_send_direct_message",
            arguments={"user_id": "user-2", "content": "hello"},
        ),
        ToolExecutionContext(
            session_id="session",
            tenant_id="tenant-a",
            user_id="admin",
            approved_call_ids=frozenset({"call-2"}),
        ),
    )
    assert outside.ok is False
    assert "不在连接授权范围" in outside.error


def test_dingtalk_todo_uses_default_owner_and_millisecond_due_time(tmp_path):
    executor, fake = _dingtalk_executor(tmp_path)
    result = executor.execute(
        ToolCall(
            call_id="todo-1",
            name="dingtalk_create_todo",
            arguments={
                "subject": "Review",
                "executor_union_ids": ["executor-1"],
                "due_at": "2027-01-01T08:00:00+08:00",
            },
        ),
        ToolExecutionContext(
            session_id="session",
            tenant_id="tenant-a",
            user_id="admin",
            approved_call_ids=frozenset({"todo-1"}),
        ),
    )

    assert result.ok is True
    kind, payload = fake.calls[-1]
    assert kind == "todo"
    assert payload["owner_union_id"] == "owner-1"
    expected = int(datetime(2027, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    assert payload["due_time_ms"] == expected
