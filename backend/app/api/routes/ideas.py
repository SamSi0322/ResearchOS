from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.schemas import (
    FunnelAdvanceIn,
    IdeaDecisionIn,
    IdeaGenerateIn,
    IdeaOut,
    ScorecardOut,
)
from app.db import get_session
from app.services import FunnelService, IdeaGenerationService

router = APIRouter()


@router.get("", response_model=list[IdeaOut])
def list_ideas(
    project_id: str, stage: str | None = Query(None), db: Session = Depends(get_session)
):
    return IdeaGenerationService(db).list(project_id, stage=stage)


@router.post("/generate", response_model=list[IdeaOut])
async def generate_ideas(
    project_id: str, payload: IdeaGenerateIn, db: Session = Depends(get_session)
):
    try:
        return await IdeaGenerationService(db).generate(project_id, payload)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e


@router.post("/score", response_model=list[ScorecardOut])
async def score_ideas(
    project_id: str, stage: str = Query("S0"), db: Session = Depends(get_session)
):
    return await FunnelService(db).score(project_id, stage)


@router.post("/advance", response_model=dict)
def advance_stage(
    project_id: str, payload: FunnelAdvanceIn, db: Session = Depends(get_session)
):
    try:
        return FunnelService(db).advance(project_id, payload)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e


@router.put("/{idea_id}/decision", response_model=IdeaOut)
def apply_decision(
    project_id: str,
    idea_id: str,
    payload: IdeaDecisionIn,
    db: Session = Depends(get_session),
):
    try:
        return FunnelService(db).apply_decision(idea_id, payload)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.get("/{idea_id}", response_model=IdeaOut)
def get_idea(project_id: str, idea_id: str, db: Session = Depends(get_session)):
    try:
        return IdeaGenerationService(db).get(idea_id)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
