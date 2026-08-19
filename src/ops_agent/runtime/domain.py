from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


ToolRisk = Literal["low", "medium", "high"]


class AttachmentReference(BaseModel):
    attachment_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    media_type: Literal["image/png", "image/jpeg", "image/webp", "image/gif"]
    bytes: int = Field(gt=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    name: str | None = Field(default=None, max_length=255)


class AttachmentUploadRequest(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    media_type: Literal["image/png", "image/jpeg", "image/webp", "image/gif"]
    data_base64: str = Field(min_length=4)


class ToolCall(BaseModel):
    call_id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ModelTurn(BaseModel):
    provider: str
    model: str
    content: str = ""
    reasoning_content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    call_id: str
    tool_name: str
    ok: bool
    output: Any = None
    error: str | None = None
    duration_ms: float = 0
    replayed: bool = False


class RuntimeAgentRequest(BaseModel):
    question: str = Field(min_length=2, max_length=4000)
    session_id: str | None = Field(default=None, max_length=128)
    model_id: str | None = Field(default=None, max_length=64)
    attachment_ids: list[str] = Field(default_factory=list, max_length=20)


class ResumeAgentRequest(BaseModel):
    session_id: str = Field(min_length=8, max_length=128)


class ContextWindowUpdate(BaseModel):
    enabled: bool | None = None
    keep_recent_user_turns: int | None = Field(default=None, ge=1, le=200)
    max_messages: int | None = Field(default=None, ge=8, le=500)
    max_chars: int | None = Field(default=None, ge=4_000, le=2_000_000)
    tool_max_rows: int | None = Field(default=None, ge=1, le=200)
    tool_max_chars: int | None = Field(default=None, ge=500, le=80_000)


class RuntimeAgentResponse(BaseModel):
    session_id: str
    answer: str
    provider: str
    model: str
    tool_results: list[ToolResult] = Field(default_factory=list)
    event_count: int
    status: Literal[
        "completed",
        "waiting_approval",
        "cancelled",
        "interrupted",
        "budget_exceeded",
        "timed_out",
        "failed",
    ] = "completed"
    pending_approval_ids: list[str] = Field(default_factory=list)
