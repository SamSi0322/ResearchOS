from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import SessionStatus
from app.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from .project import StudentProject


class MentorshipSession(Base, TimestampMixin):
    __tablename__ = "mentorship_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("student_projects.id", ondelete="CASCADE")
    )
    scheduled_at: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(16), default=SessionStatus.scheduled.value)
    mentor_name: Mapped[str] = mapped_column(String(255))
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    student_participation_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_actions: Mapped[list] = mapped_column(JSON, default=list)
    unresolved_blockers: Mapped[list] = mapped_column(JSON, default=list)
    student_must_understand: Mapped[list] = mapped_column(JSON, default=list)

    project: Mapped["StudentProject"] = relationship(back_populates="sessions")
