from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.schemas import (
    ProjectCreateIn,
    ProjectOut,
    ProjectUpdateIn,
    ResearchBriefIn,
    ResearchBriefOut,
    OkOut,
)
from app.db import get_session
from app.services import FunnelService, ResearchBriefService
from app.services.budget_service import BudgetService

router = APIRouter()


@router.get("", response_model=list[ProjectOut])
def list_projects(db: Session = Depends(get_session)):
    svc = ResearchBriefService(db)
    return svc.list_projects()


@router.post("", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
def create_project(payload: ProjectCreateIn, db: Session = Depends(get_session)):
    svc = ResearchBriefService(db)
    return svc.create_project(payload)


@router.get("/{project_id}", response_model=ProjectOut)
def get_project(project_id: str, db: Session = Depends(get_session)):
    svc = ResearchBriefService(db)
    try:
        return svc.get_project(project_id)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e


@router.put("/{project_id}", response_model=ProjectOut)
def update_project(
    project_id: str, payload: ProjectUpdateIn, db: Session = Depends(get_session)
):
    svc = ResearchBriefService(db)
    try:
        return svc.update_project(project_id, payload)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e


@router.put("/{project_id}/brief", response_model=ResearchBriefOut)
def replace_brief(
    project_id: str, payload: ResearchBriefIn, db: Session = Depends(get_session)
):
    svc = ResearchBriefService(db)
    try:
        return svc.replace_brief(project_id, payload)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e


@router.get("/{project_id}/funnel/summary")
def funnel_summary(project_id: str, db: Session = Depends(get_session)) -> dict:
    svc = FunnelService(db)
    return svc.stage_summary(project_id)


@router.get("/{project_id}/budget")
def budget_summary(project_id: str, db: Session = Depends(get_session)) -> dict:
    """Ledger-backed budget summary for a project.

    Returns ``{ceiling_usd, spent_usd, remaining_usd, warn, entries}``. The
    ``spent_usd`` field is the sum of ``BudgetLedgerEntry.amount_usd`` rows
    written by the runtime after each completed run.
    """
    return BudgetService(db).summary(project_id)
