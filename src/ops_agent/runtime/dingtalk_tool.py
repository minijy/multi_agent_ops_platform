from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator

from .connectors import ConnectorRuntime
from .tools import ToolDefinition, ToolExecutionContext, ToolRegistry


MessageType = Literal["text", "markdown"]


class DingTalkDirectMessageArguments(BaseModel):
    user_id: str = Field(min_length=1, max_length=128)
    content: str = Field(min_length=1, max_length=5000)
    message_type: MessageType = "text"
    title: str | None = Field(default=None, max_length=200)


class DingTalkGroupMessageArguments(BaseModel):
    open_conversation_id: str = Field(min_length=1, max_length=256)
    content: str = Field(min_length=1, max_length=5000)
    message_type: MessageType = "text"
    title: str | None = Field(default=None, max_length=200)


class DingTalkTodoArguments(BaseModel):
    owner_union_id: str | None = Field(default=None, max_length=128)
    subject: str = Field(min_length=1, max_length=1024)
    description: str = Field(default="", max_length=4096)
    executor_union_ids: list[str] = Field(min_length=1, max_length=100)
    participant_union_ids: list[str] = Field(default_factory=list, max_length=100)
    due_at: datetime | None = None
    detail_url: str | None = Field(default=None, max_length=1000)
    source_id: str | None = Field(default=None, max_length=128)
    priority: int = Field(default=20, ge=0, le=100)

    @field_validator("executor_union_ids", "participant_union_ids")
    @classmethod
    def unique_non_empty_ids(
        cls, values: list[str], info: ValidationInfo
    ) -> list[str]:
        normalized = [str(value).strip() for value in values if str(value).strip()]
        if info.field_name == "executor_union_ids" and not normalized:
            raise ValueError("至少需要一个待办执行人 UnionId")
        if len(normalized) != len(set(normalized)):
            raise ValueError("UnionId 不能重复")
        return normalized

    @model_validator(mode="after")
    def validate_detail_url(self) -> "DingTalkTodoArguments":
        if self.detail_url and not self.detail_url.startswith("https://"):
            raise ValueError("detail_url 必须使用 HTTPS")
        return self


def _assert_targets_allowed(
    connectors: ConnectorRuntime,
    connection,
    tool_name: str,
    scope_name: str,
    requested: list[str],
    delegated_scope: dict[str, tuple[str, ...]],
) -> None:
    allowed = set(
        connectors.scoped_tool_resources(
            connection, tool_name, scope_name, delegated_scope
        )
    )
    if not allowed:
        raise PermissionError(f"当前 Tool 未配置可访问的钉钉目标范围: {scope_name}")
    requested_set = set(requested)
    if "*" not in allowed and not requested_set <= allowed:
        denied = sorted(requested_set - allowed)
        raise PermissionError(f"钉钉目标不在连接授权范围内: {', '.join(denied)}")


def _message_result(kind: str, target: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "submitted",
        "channel": kind,
        "target": target,
        "process_query_key": payload.get("processQueryKey"),
        "invalid_user_ids": payload.get("invalidStaffIdList") or [],
        "flow_controlled_user_ids": payload.get("flowControlledStaffIdList") or [],
    }


def register_dingtalk_tools(
    registry: ToolRegistry,
    connectors: ConnectorRuntime,
    *,
    timeout_seconds: float = 20.0,
) -> None:
    def send_direct(
        arguments: DingTalkDirectMessageArguments,
        context: ToolExecutionContext,
    ) -> dict[str, Any]:
        def operation(client, connection):
            _assert_targets_allowed(
                connectors,
                connection,
                "dingtalk_send_direct_message",
                "dingtalk_user_ids",
                [arguments.user_id],
                context.resource_scope,
            )
            payload = client.send_direct_message(**arguments.model_dump())
            return _message_result("direct", arguments.user_id, payload)

        return connectors.execute_tool(
            context.tenant_id,
            "dingtalk_send_direct_message",
            operation,
            retry_transient=False,
        )

    def send_group(
        arguments: DingTalkGroupMessageArguments,
        context: ToolExecutionContext,
    ) -> dict[str, Any]:
        def operation(client, connection):
            _assert_targets_allowed(
                connectors,
                connection,
                "dingtalk_send_group_message",
                "dingtalk_conversation_ids",
                [arguments.open_conversation_id],
                context.resource_scope,
            )
            payload = client.send_group_message(**arguments.model_dump())
            return _message_result("group", arguments.open_conversation_id, payload)

        return connectors.execute_tool(
            context.tenant_id,
            "dingtalk_send_group_message",
            operation,
            retry_transient=False,
        )

    def create_todo(
        arguments: DingTalkTodoArguments,
        context: ToolExecutionContext,
    ) -> dict[str, Any]:
        def operation(client, connection):
            owner = arguments.owner_union_id or str(
                connection.config.get("default_todo_owner_union_id") or ""
            ).strip()
            if not owner:
                raise ValueError("请指定 owner_union_id，或在钉钉连接中配置默认待办创建者")
            targets = [
                owner,
                *arguments.executor_union_ids,
                *arguments.participant_union_ids,
            ]
            _assert_targets_allowed(
                connectors,
                connection,
                "dingtalk_create_todo",
                "dingtalk_union_ids",
                targets,
                context.resource_scope,
            )
            due_at = arguments.due_at
            if due_at and due_at.tzinfo is None:
                due_at = due_at.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
            payload = client.create_todo(
                owner_union_id=owner,
                subject=arguments.subject,
                description=arguments.description,
                executor_union_ids=arguments.executor_union_ids,
                participant_union_ids=arguments.participant_union_ids,
                due_time_ms=int(due_at.timestamp() * 1000) if due_at else None,
                detail_url=arguments.detail_url,
                source_id=arguments.source_id or f"arkflow-{uuid.uuid4().hex}",
                priority=arguments.priority,
            )
            return {
                "status": "created",
                "task_id": payload.get("id") or payload.get("taskId"),
                "subject": arguments.subject,
                "owner_union_id": owner,
                "executor_union_ids": arguments.executor_union_ids,
                "due_at": due_at.isoformat() if due_at else None,
            }

        return connectors.execute_tool(
            context.tenant_id,
            "dingtalk_create_todo",
            operation,
            retry_transient=False,
        )

    definitions = (
        ToolDefinition(
            name="dingtalk_send_direct_message",
            description=(
                "向已授权的钉钉用户发送机器人单聊文本或 Markdown 消息。"
                "这是外部写操作，发送前必须获得人工审批。"
            ),
            arguments_model=DingTalkDirectMessageArguments,
            handler=send_direct,
            risk="medium",
            requires_approval=True,
            timeout_seconds=timeout_seconds + 1,
            concurrency_safe=True,
            source="dingtalk",
            builtin=False,
        ),
        ToolDefinition(
            name="dingtalk_send_group_message",
            description=(
                "向已授权的钉钉群 openConversationId 发送机器人文本或 Markdown 消息。"
                "这是外部写操作，发送前必须获得人工审批。"
            ),
            arguments_model=DingTalkGroupMessageArguments,
            handler=send_group,
            risk="medium",
            requires_approval=True,
            timeout_seconds=timeout_seconds + 1,
            concurrency_safe=True,
            source="dingtalk",
            builtin=False,
        ),
        ToolDefinition(
            name="dingtalk_create_todo",
            description=(
                "为已授权的 UnionId 创建钉钉待办，可设置标题、执行人、参与人和截止时间。"
                "这是外部写操作，创建前必须获得人工审批。"
            ),
            arguments_model=DingTalkTodoArguments,
            handler=create_todo,
            risk="medium",
            requires_approval=True,
            timeout_seconds=timeout_seconds + 1,
            concurrency_safe=True,
            source="dingtalk",
            builtin=False,
        ),
    )
    for definition in definitions:
        registry.register(definition)
