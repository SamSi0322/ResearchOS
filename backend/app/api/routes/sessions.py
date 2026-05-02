from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.schemas import MentorshipSessionIn, MentorshipSessionOut, OkOut
from app.db import get_session
from app.services import MentorshipSessionService

router = APIRouter()


@router.get("", response_model=list[MentorshipSessionOut])
def list_sessions(project_id: str, db: Session = Depends(get_session)):
    return MentorshipSessionService(db).list(project_id)


@router.post("", response_model=MentorshipSessionOut)
def create_session(
    project_id: str, payload: MentorshipSessionIn, db: Session = Depends(get_session)
):
    return MentorshipSessionService(db).create(project_id, payload)


@router.put("/{session_id}", response_model=MentorshipSessionOut)
def update_session(
    project_id: str,
    session_id: str,
    payload: MentorshipSessionIn,
    db: Session = Depends(get_session),
):
    try:
        return MentorshipSessionService(db).update(session_id, payload)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e


@router.delete("/{session_id}", response_model=OkOut)
def delete_session(project_id: str, session_id: str, db: Session = Depends(get_session)):
    MentorshipSessionService(db).delete(session_id)
    return OkOut()
