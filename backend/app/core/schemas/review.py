from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ReviewIssueOut(BaseModel):
    id: str
    project_id: str
    draft_id: str | None = None
    subject_kind: str
    subject_id: str
    reviewer_class: str
    severity: str
    state: str
    description: str
    evidence: str | None = None
    suggested_remediation: str | None = None
    resolution_note: str | None = None
    meta: dict = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ReviewIssueIn(BaseModel):
    state: str | None = None
    resolution_note: str | None = None
    severity: str | None = None


class ReviewRunIn(BaseModel):
    draft_id: str | None = None
    reviewer_classes: list[str] | None = None
    extra_instructions: str | None = None
