from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.enums import AuditKind
from app.core.models import MentorshipSession
from app.core.schemas import MentorshipSessionIn
from app.services.audit_service import AuditService
from app.utils import new_id


class MentorshipSessionService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.audit = AuditService(db)

    def create(self, project_id: str, payload: MentorshipSessionIn) -> MentorshipSession:
        sess = MentorshipSession(
            id=new_id("sess"),
            project_id=project_id,
            scheduled_at=payload.scheduled_at,
            status=payload.status,
            mentor_name=payload.mentor_name,
            notes=payload.notes,
            student_participation_notes=payload.student_participation_notes,
            next_actions=payload.next_actions,
            unresolved_blockers=payload.unresolved_blockers,
            student_must_understand=payload.student_must_understand,
        )
        self.db.add(sess)
        self.audit.log(
            project_id=project_id,
            kind=AuditKind.session_logged,
            subject_kind="session",
            subject_id=sess.id,
            message=f"Logged session with {sess.mentor_name} @ {sess.scheduled_at:%Y-%m-%d}",
        )
        self.db.commit()
        return sess

    def update(
        self, session_id: str, payload: MentorshipSessionIn
    ) -> MentorshipSession:
        sess = (
            self.db.query(MentorshipSession)
            .filter(MentorshipSession.id == session_id)
            .first()
        )
        if sess is None:
            raise LookupError(f"session not found: {session_id}")
        for k, v in payload.model_dump().items():
            setattr(sess, k, v)
        self.audit.log(
            project_id=sess.project_id,
            kind=AuditKind.session_logged,
            subject_kind="session",
            subject_id=sess.id,
            message="session updated",
        )
        self.db.commit()
        return sess

    def delete(self, session_id: str) -> None:
        sess = (
            self.db.query(MentorshipSession)
            .filter(MentorshipSession.id == session_id)
            .first()
        )
        if sess is None:
            return
        self.db.delete(sess)
        self.db.commit()

    def list(self, project_id: str) -> list[MentorshipSession]:
        return (
            self.db.query(MentorshipSession)
            .filter(MentorshipSession.project_id == project_id)
            .order_by(MentorshipSession.scheduled_at.desc())
            .all()
        )
