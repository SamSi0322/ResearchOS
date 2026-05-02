from __future__ import annotations

from sqlalchemy import ForeignKey, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class AuditEvent(Base, TimestampMixin):
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("student_projects.id", ondelete="SET NULL"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(64))
    actor: Mapped[str] = mapped_column(String(64), default="system")  # system|mentor|advisor|student
    subject_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    subject_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
