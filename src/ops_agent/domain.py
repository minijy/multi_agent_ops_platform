from __future__ import annotations

from pydantic import BaseModel, Field


class ApprovalRequest(BaseModel):
    approved: bool
    comment: str = Field(default="", max_length=1000)
