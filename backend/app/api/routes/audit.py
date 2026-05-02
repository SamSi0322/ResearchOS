from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.schemas import AuditEventOut
from app.db import get_session
from app.services import AuditService

router = APIRouter()


@router.get("", response_model=list[AuditEventOut])
def list_audit(
    project_id: str | None = Query(None),
    limit: int = Query(200, ge=1, le=2000),
    db: Session = Depends(get_session),
):
    return AuditService(db).list(project_id=project_id, limit=limit)
