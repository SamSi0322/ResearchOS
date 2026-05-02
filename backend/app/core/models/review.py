from __future__ import annotations

from sqlalchemy import ForeignKey, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import ReviewerClass, ReviewSeverity, ReviewState
from app.db.base import Base, TimestampMixin


class ReviewIssue(Base, TimestampMixin):
    __tablename__ = "review_issues"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("student_projects.id", ondelete="CASCADE")
    )
    draft_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("drafts.id", ondelete="SET NULL"), nullable=True
    )
    subject_kind: Mapped[str] = mapped_column(String(64))  # draft|run|claim|spec|package
    subject_id: Mapped[str] = mapped_column(String(64))
    reviewer_class: Mapped[str] = mapped_column(String(32), default=ReviewerClass.methodology.value)
    severity: Mapped[str] = mapped_column(String(8), default=ReviewSeverity.P2.value)
    state: Mapped[str] = mapped_column(String(16), default=ReviewState.open.value)
    description: Mapped[str] = mapped_column(Text)
    evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    suggested_remediation: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
