from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ResearchBriefIn(BaseModel):
    research_direction: str
    constraints: str | None = None
    target_venues: list[str] = Field(default_factory=list)
    budget_usd: float = 50.0
    strategy: str | None = None
    raw_context: str | None = None


class ResearchBriefOut(ResearchBriefIn):
    id: str
    project_id: str
    created_at: datetime
    updated_at: datetime
    model_config = ConfigDict(from_attributes=True)


_DEFAULT_GATES: list[str] = [
    "post_shortlist",
    "post_pilot_evidence",
    "pre_package_freeze",
]


class ProjectCreateIn(BaseModel):
    title: str
    student_name: str
    student_ref: str | None = None
    mentor_name: str
    advisor_name: str | None = None
    research_direction: str
    target_venues: list[str] = Field(default_factory=list)
    constraints: str | None = None
    exploration_strategy: str | None = None
    provider_profile: str = "default"
    budget_usd: float = 50.0
    notes: str | None = None

    # Human-in-the-loop configuration. Defaults are OFF so existing callers
    # (and existing tests) do not need to change.
    human_in_loop_enabled: bool = False
    primary_approver_email: str | None = None
    cc_emails: list[str] = Field(default_factory=list)
    approval_timeout_hours: int = 72
    reminder_interval_hours: int = 24
    approval_gates: list[str] = Field(default_factory=lambda: list(_DEFAULT_GATES))


class ProjectUpdateIn(BaseModel):
    title: str | None = None
    status: str | None = None
    student_name: str | None = None
    student_ref: str | None = None
    mentor_name: str | None = None
    advisor_name: str | None = None
    research_direction: str | None = None
    target_venues: list[str] | None = None
    constraints: str | None = None
    exploration_strategy: str | None = None
    provider_profile: str | None = None
    notes: str | None = None

    # HITL update paths, all optional.
    human_in_loop_enabled: bool | None = None
    primary_approver_email: str | None = None
    cc_emails: list[str] | None = None
    approval_timeout_hours: int | None = None
    reminder_interval_hours: int | None = None
    approval_gates: list[str] | None = None


class ProjectOut(BaseModel):
    id: str
    title: str
    status: str
    student_name: str
    student_ref: str | None = None
    mentor_name: str
    advisor_name: str | None = None
    research_direction: str
    target_venues: list[str] = Field(default_factory=list)
    constraints: str | None = None
    exploration_strategy: str | None = None
    provider_profile: str = "default"
    notes: str | None = None
    created_at: datetime
    updated_at: datetime
    brief: ResearchBriefOut | None = None

    human_in_loop_enabled: bool = False
    primary_approver_email: str | None = None
    cc_emails: list[str] = Field(default_factory=list)
    approval_timeout_hours: int = 72
    reminder_interval_hours: int = 24
    approval_gates: list[str] = Field(default_factory=lambda: list(_DEFAULT_GATES))

    model_config = ConfigDict(from_attributes=True)
