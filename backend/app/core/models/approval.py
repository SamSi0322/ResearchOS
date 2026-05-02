from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import ApprovalDecision, ApprovalStatus, GateKey
from app.db.base import Base, TimestampMixin


class ApprovalRequest(Base, TimestampMixin):
    """A pipeline checkpoint awaiting an internal human decision.

    One row per gate-per-project. The pipeline checks the latest unresolved
    row for ``(project_id, stage_key)``; if none exists at a required gate,
    it creates one, sends the email / outbox notification, and pauses until
    the row resolves.

    Tokens are stored in cleartext (not secret) - they are short random
    identifiers used inside signed approval links, not bearer credentials.
    """

    __tablename__ = "approval_requests"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("student_projects.id", ondelete="CASCADE")
    )
    stage_key: Mapped[str] = mapped_column(String(64))

    status: Mapped[str] = mapped_column(
        String(32), default=ApprovalStatus.pending.value
    )
    decision: Mapped[str | None] = mapped_column(String(32), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    approver_email: Mapped[str] = mapped_column(String(255))
    cc_emails: Mapped[list] = mapped_column(JSON, default=list)

    requested_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    timeout_at: Mapped[datetime] = mapped_column(DateTime)
    reminder_count: Mapped[int] = mapped_column(Integer, default=0)
    # Historical field — kept for back-compat with the existing reminder_scan
    # logic. ``last_reminder_sent_at`` is the new explicit name and is kept
    # in lockstep with ``last_reminder_at`` by the scheduler.
    last_reminder_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_reminder_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )

    token: Mapped[str] = mapped_column(String(64))
    context_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    outbox_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
