from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class MentorshipSessionIn(BaseModel):
    scheduled_at: datetime
    mentor_name: str
    status: str = "scheduled"
    notes: str | None = None
    student_participation_notes: str | None = None
    next_actions: list[str] = Field(default_factory=list)
    unresolved_blockers: list[str] = Field(default_factory=list)
    student_must_understand: list[str] = Field(default_factory=list)


class MentorshipSessionOut(MentorshipSessionIn):
    id: str
    project_id: str
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
