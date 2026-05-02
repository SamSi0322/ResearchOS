from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Float, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import ProjectStatus
from app.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from .idea import Idea
    from .experiment import ExperimentRun
    from .session import MentorshipSession
    from .manuscript import Manuscript
    from .package import DeliveryPackage


class StudentProject(Base, TimestampMixin):
    __tablename__ = "student_projects"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), default=ProjectStatus.active.value)
    student_name: Mapped[str] = mapped_column(String(255))
    student_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    mentor_name: Mapped[str] = mapped_column(String(255))
    advisor_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    research_direction: Mapped[str] = mapped_column(Text)
    target_venues: Mapped[list] = mapped_column(JSON, default=list)
    constraints: Mapped[str | None] = mapped_column(Text, nullable=True)
    exploration_strategy: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider_profile: Mapped[str] = mapped_column(String(64), default="default")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- Human-in-the-loop ------------------------------------------------
    # When enabled, the batch orchestrator and package service pause at the
    # configured gates, create an ApprovalRequest, send an email (or file
    # outbox entry) to the approver, and only resume when the request is
    # resolved. Defaults make the feature explicit but non-intrusive.
    human_in_loop_enabled: Mapped[bool] = mapped_column(default=False, nullable=False)
    primary_approver_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    cc_emails: Mapped[list] = mapped_column(JSON, default=list)
    approval_timeout_hours: Mapped[int] = mapped_column(Integer, default=72)
    reminder_interval_hours: Mapped[int] = mapped_column(Integer, default=24)
    approval_gates: Mapped[list] = mapped_column(
        JSON,
        default=lambda: ["post_shortlist", "post_pilot_evidence", "pre_package_freeze"],
    )

    brief: Mapped["ResearchBrief | None"] = relationship(
        back_populates="project", uselist=False, cascade="all, delete-orphan"
    )
    ideas: Mapped[list["Idea"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    runs: Mapped[list["ExperimentRun"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    sessions: Mapped[list["MentorshipSession"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    manuscripts: Mapped[list["Manuscript"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    packages: Mapped[list["DeliveryPackage"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class ResearchBrief(Base, TimestampMixin):
    __tablename__ = "research_briefs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("student_projects.id", ondelete="CASCADE")
    )
    research_direction: Mapped[str] = mapped_column(Text)
    constraints: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_venues: Mapped[list] = mapped_column(JSON, default=list)
    budget_usd: Mapped[float] = mapped_column(Float, default=0.0)
    strategy: Mapped[str | None] = mapped_column(String(128), nullable=True)
    raw_context: Mapped[str | None] = mapped_column(Text, nullable=True)

    project: Mapped["StudentProject"] = relationship(back_populates="brief")


class SourceSnapshot(Base, TimestampMixin):
    """Frozen snapshot of external context the mentor team used at a point in time."""

    __tablename__ = "source_snapshots"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("student_projects.id", ondelete="CASCADE")
    )
    title: Mapped[str] = mapped_column(String(255))
    url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    kind: Mapped[str] = mapped_column(String(64), default="note")  # paper|dataset|note|other
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    captured_at: Mapped[datetime | None] = mapped_column(nullable=True)


class BudgetPolicy(Base, TimestampMixin):
    __tablename__ = "budget_policies"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("student_projects.id", ondelete="CASCADE")
    )
    ceiling_usd: Mapped[float] = mapped_column(Float, default=50.0)
    warn_ratio: Mapped[float] = mapped_column(Float, default=0.8)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class BudgetLedgerEntry(Base, TimestampMixin):
    __tablename__ = "budget_ledger"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("student_projects.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(String(64))  # provider_call | run | manual
    amount_usd: Mapped[float] = mapped_column(Float)
    reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
