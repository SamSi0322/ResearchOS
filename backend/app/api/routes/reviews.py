from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.schemas import ReviewIssueIn, ReviewIssueOut, ReviewRunIn
from app.db import get_session
from app.services import ReviewService

router = APIRouter()


@router.get("", response_model=list[ReviewIssueOut])
def list_issues(project_id: str, db: Session = Depends(get_session)):
    return ReviewService(db).list(project_id)


@router.post("/run", response_model=list[ReviewIssueOut])
async def run_reviewers(
    project_id: str, payload: ReviewRunIn, db: Session = Depends(get_session)
):
    try:
        return await ReviewService(db).run_reviewers(project_id, payload)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e


@router.put("/{issue_id}", response_model=ReviewIssueOut)
def update_issue(
    project_id: str,
    issue_id: str,
    payload: ReviewIssueIn,
    db: Session = Depends(get_session),
):
    try:
        return ReviewService(db).update(issue_id, payload)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
