from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Float, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import FunnelStage, IdeaDecision, IdeaStage
from app.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from .project import StudentProject


class Idea(Base, TimestampMixin):
    __tablename__ = "ideas"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("student_projects.id", ondelete="CASCADE")
    )
    title: Mapped[str] = mapped_column(String(255))
    summary: Mapped[str] = mapped_column(Text)
    hypothesis: Mapped[str | None] = mapped_column(Text, nullable=True)
    novelty_claim: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_metric: Mapped[str | None] = mapped_column(String(255), nullable=True)
    cluster_tag: Mapped[str | None] = mapped_column(String(128), nullable=True)
    stage: Mapped[str] = mapped_column(String(16), default=IdeaStage.S0.value)
    decision: Mapped[str] = mapped_column(String(16), default=IdeaDecision.pending.value)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)

    project: Mapped["StudentProject"] = relationship(back_populates="ideas")
    decisions: Mapped[list["FunnelDecision"]] = relationship(
        back_populates="idea", cascade="all, delete-orphan"
    )
    scorecards: Mapped[list["Scorecard"]] = relationship(
        back_populates="idea", cascade="all, delete-orphan"
    )


class FunnelDecision(Base, TimestampMixin):
    __tablename__ = "funnel_decisions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    idea_id: Mapped[str] = mapped_column(String(64), ForeignKey("ideas.id", ondelete="CASCADE"))
    from_stage: Mapped[str] = mapped_column(String(16))
    to_stage: Mapped[str] = mapped_column(String(16))
    decision: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_by: Mapped[str] = mapped_column(String(128), default="system")

    idea: Mapped["Idea"] = relationship(back_populates="decisions")


class Scorecard(Base, TimestampMixin):
    __tablename__ = "scorecards"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    idea_id: Mapped[str] = mapped_column(String(64), ForeignKey("ideas.id", ondelete="CASCADE"))
    stage: Mapped[str] = mapped_column(String(16))
    novelty: Mapped[float] = mapped_column(Float, default=0.0)
    feasibility: Mapped[float] = mapped_column(Float, default=0.0)
    rigor: Mapped[float] = mapped_column(Float, default=0.0)
    impact: Mapped[float] = mapped_column(Float, default=0.0)
    overall: Mapped[float] = mapped_column(Float, default=0.0)
    rubric: Mapped[dict] = mapped_column(JSON, default=dict)

    idea: Mapped["Idea"] = relationship(back_populates="scorecards")
