from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class ApprovalRequestOut(BaseModel):
    id: str
    project_id: str
    stage_key: str
    status: str
    decision: str | None = None
    decision_note: str | None = None
    approver_email: str
    cc_emails: list[str] = Field(default_factory=list)
    requested_at: datetime
    resolved_at: datetime | None = None
    timeout_at: datetime
    reminder_count: int = 0
    last_reminder_at: datetime | None = None
    token: str
    context_snapshot: dict[str, Any] = Field(default_factory=dict)
    outbox_path: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ApprovalDecisionIn(BaseModel):
    decision: str  # approve | reject | request_changes
    note: str | None = None
    actor: str = "operator"


class ApprovalCreateIn(BaseModel):
    stage_key: str
    context_snapshot: dict[str, Any] = Field(default_factory=dict)
    approver_email: str | None = None  # override project default
    cc_emails: list[str] | None = None
