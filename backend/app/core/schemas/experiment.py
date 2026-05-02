from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class SpecGenerateIn(BaseModel):
    idea_id: str
    extra_instructions: str | None = None


class SpecOut(BaseModel):
    id: str
    project_id: str
    idea_id: str
    version: int
    hypothesis: str
    problem_framing: str
    target_metrics: list[str] = Field(default_factory=list)
    dataset_assumptions: str
    baseline: str
    experiment_plan: str
    constraints: str
    success_criteria: list[str] = Field(default_factory=list)
    stop_criteria: list[str] = Field(default_factory=list)
    budget_estimate_usd: float = 0.0
    meta: dict = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class RunStartIn(BaseModel):
    spec_id: str
    # "two_step" = builder (claude_code) then reviewer (codex) merged - the
    # intended default collaboration model. Operators only need to override
    # when they explicitly want a single-pass builder or reviewer-only run.
    worker: str = "two_step"  # two_step | claude_code | codex
    seed: int | None = None
    extra_instructions: str | None = None


class ArtifactOut(BaseModel):
    id: str
    kind: str
    name: str
    path: str
    size_bytes: int
    sha256: str | None = None
    mock: bool = False
    meta: dict = Field(default_factory=dict)
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class RunOut(BaseModel):
    id: str
    project_id: str
    spec_id: str
    idea_id: str
    status: str
    result_class: str | None = None
    exit_code: int | None = None
    seed: int
    code_hash: str | None = None
    workspace_path: str
    provider_routing: dict = Field(default_factory=dict)
    metrics: dict = Field(default_factory=dict)
    config: dict = Field(default_factory=dict)
    summary: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    mock: bool = False
    total_estimated_cost: float = 0.0
    artifacts: list[ArtifactOut] = Field(default_factory=list)
    stdout_log: str | None = None
    stderr_log: str | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class RunAnalysisOut(BaseModel):
    run_id: str
    result_class: str
    verdict: str
    metrics: dict = Field(default_factory=dict)
    baseline_delta: dict = Field(default_factory=dict)
    promoted_idea: bool = False
    claim_ids: list[str] = Field(default_factory=list)
