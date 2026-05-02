from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ScorecardOut(BaseModel):
    id: str
    stage: str
    novelty: float
    feasibility: float
    rigor: float
    impact: float
    overall: float
    rubric: dict = Field(default_factory=dict)
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class IdeaOut(BaseModel):
    id: str
    project_id: str
    title: str
    summary: str
    hypothesis: str | None = None
    novelty_claim: str | None = None
    target_metric: str | None = None
    cluster_tag: str | None = None
    stage: str
    decision: str
    score: float | None = None
    rationale: str | None = None
    created_at: datetime
    updated_at: datetime
    scorecards: list[ScorecardOut] = Field(default_factory=list)
    meta: dict = Field(default_factory=dict)

    model_config = ConfigDict(from_attributes=True)


class IdeaGenerateIn(BaseModel):
    count: int = 50
    extra_context: str | None = None


class IdeaDecisionIn(BaseModel):
    decision: str  # keep|reject|promote|waived
    rationale: str | None = None
    promote_to_stage: str | None = None


class FunnelAdvanceIn(BaseModel):
    from_stage: str
    to_stage: str
    keep_count: int | None = None
    auto_reject: bool = True
    rationale: str | None = None
