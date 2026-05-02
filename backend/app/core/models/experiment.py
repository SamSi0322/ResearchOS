from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Float, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import RunResultClass, RunStatus
from app.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from .idea import Idea
    from .project import StudentProject


class ExperimentSpec(Base, TimestampMixin):
    __tablename__ = "experiment_specs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("student_projects.id", ondelete="CASCADE")
    )
    idea_id: Mapped[str] = mapped_column(String(64), ForeignKey("ideas.id", ondelete="CASCADE"))
    version: Mapped[int] = mapped_column(Integer, default=1)

    hypothesis: Mapped[str] = mapped_column(Text)
    problem_framing: Mapped[str] = mapped_column(Text)
    target_metrics: Mapped[list] = mapped_column(JSON, default=list)
    dataset_assumptions: Mapped[str] = mapped_column(Text, default="")
    baseline: Mapped[str] = mapped_column(Text, default="")
    experiment_plan: Mapped[str] = mapped_column(Text, default="")
    constraints: Mapped[str] = mapped_column(Text, default="")
    success_criteria: Mapped[list] = mapped_column(JSON, default=list)
    stop_criteria: Mapped[list] = mapped_column(JSON, default=list)
    budget_estimate_usd: Mapped[float] = mapped_column(Float, default=0.0)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)

    runs: Mapped[list["ExperimentRun"]] = relationship(
        back_populates="spec", cascade="all, delete-orphan"
    )


class ExperimentRun(Base, TimestampMixin):
    __tablename__ = "experiment_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("student_projects.id", ondelete="CASCADE")
    )
    spec_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("experiment_specs.id", ondelete="CASCADE")
    )
    idea_id: Mapped[str] = mapped_column(String(64), ForeignKey("ideas.id", ondelete="CASCADE"))
    workspace_path: Mapped[str] = mapped_column(String(1024))
    status: Mapped[str] = mapped_column(String(32), default=RunStatus.queued.value)
    result_class: Mapped[str | None] = mapped_column(String(32), nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    seed: Mapped[int] = mapped_column(Integer, default=0)
    code_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider_routing: Mapped[dict] = mapped_column(JSON, default=dict)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    stdout_log: Mapped[str | None] = mapped_column(Text, nullable=True)
    stderr_log: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    mock: Mapped[bool] = mapped_column(default=False)
    # Lightweight per-run USD estimate. Sum of per-call estimates returned
    # by the provider adapters (input × input-rate + output × output-rate).
    # Never an invoice — heuristic only. 0.0 for mock runs.
    total_estimated_cost: Mapped[float] = mapped_column(Float, default=0.0)

    project: Mapped["StudentProject"] = relationship(back_populates="runs")
    spec: Mapped["ExperimentSpec"] = relationship(back_populates="runs")
    artifacts: Mapped[list["Artifact"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class Artifact(Base, TimestampMixin):
    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("experiment_runs.id", ondelete="CASCADE"), nullable=True
    )
    project_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("student_projects.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(String(64))  # log|metrics|artifact|spec|code|draft|figure
    name: Mapped[str] = mapped_column(String(512))
    path: Mapped[str] = mapped_column(String(1024))
    sha256: Mapped[str | None] = mapped_column(String(128), nullable=True)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    mock: Mapped[bool] = mapped_column(default=False)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)

    run: Mapped["ExperimentRun | None"] = relationship(back_populates="artifacts")
