from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.schemas import (
    ClaimOut,
    DraftGenerateIn,
    DraftOut,
    ManuscriptOut,
)
from app.core.models import Claim
from app.db import get_session
from app.services import (
    DraftService,
    ManuscriptQualityService,
    ReadinessService,
    ReviewSummaryService,
)

router = APIRouter()


@router.get("/quality/latest")
def latest_quality(project_id: str, db: Session = Depends(get_session)) -> dict:
    report = ManuscriptQualityService(db).latest_report(project_id)
    return report.as_dict() if report else {}


@router.get("/readiness")
def readiness(project_id: str, db: Session = Depends(get_session)) -> dict:
    return ReadinessService(db).summary(project_id)


@router.get("/review-summary")
def review_summary(project_id: str, db: Session = Depends(get_session)) -> dict:
    return ReviewSummaryService(db).summary(project_id)


@router.get("", response_model=list[ManuscriptOut])
def list_manuscripts(project_id: str, db: Session = Depends(get_session)):
    return DraftService(db).list(project_id)


@router.post("/generate", response_model=DraftOut)
async def generate_draft(
    project_id: str, payload: DraftGenerateIn, db: Session = Depends(get_session)
):
    try:
        return await DraftService(db).generate(project_id, payload)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e


@router.get("/{draft_id}", response_model=DraftOut)
def get_draft(project_id: str, draft_id: str, db: Session = Depends(get_session)):
    try:
        return DraftService(db).get_draft(draft_id)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e


@router.get("/claims/all", response_model=list[ClaimOut])
def list_claims(project_id: str, db: Session = Depends(get_session)):
    return (
        db.query(Claim)
        .filter(Claim.project_id == project_id)
        .order_by(Claim.created_at.desc())
        .all()
    )
