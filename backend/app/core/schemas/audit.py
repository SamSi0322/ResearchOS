from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class AuditEventOut(BaseModel):
    id: str
    project_id: str | None = None
    kind: str
    actor: str
    subject_kind: str | None = None
    subject_id: str | None = None
    message: str | None = None
    payload: dict = Field(default_factory=dict)
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
