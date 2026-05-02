from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.schemas import SpecGenerateIn, SpecOut
from app.db import get_session
from app.services import SpecService

router = APIRouter()


@router.get("", response_model=list[SpecOut])
def list_specs(
    project_id: str,
    idea_id: str | None = Query(None),
    db: Session = Depends(get_session),
):
    return SpecService(db).list(project_id, idea_id=idea_id)


@router.post("/generate", response_model=SpecOut)
async def generate_spec(
    project_id: str, payload: SpecGenerateIn, db: Session = Depends(get_session)
):
    try:
        return await SpecService(db).generate(project_id, payload)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.get("/{spec_id}", response_model=SpecOut)
def get_spec(project_id: str, spec_id: str, db: Session = Depends(get_session)):
    try:
        return SpecService(db).get(spec_id)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
