from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.core.enums import AuditKind
from app.core.models import AuditEvent
from app.utils import new_id


class AuditService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def log(
        self,
        *,
        project_id: str | None,
        kind: AuditKind | str,
        message: str | None = None,
        actor: str = "system",
        subject_kind: str | None = None,
        subject_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> AuditEvent:
        evt = AuditEvent(
            id=new_id("aud"),
            project_id=project_id,
            kind=kind.value if isinstance(kind, AuditKind) else kind,
            actor=actor,
            subject_kind=subject_kind,
            subject_id=subject_id,
            message=message,
            payload=payload or {},
        )
        self.db.add(evt)
        self.db.flush()
        return evt

    def list(self, *, project_id: str | None = None, limit: int = 200) -> list[AuditEvent]:
        q = self.db.query(AuditEvent).order_by(AuditEvent.created_at.desc())
        if project_id is not None:
            q = q.filter(AuditEvent.project_id == project_id)
        return q.limit(limit).all()
