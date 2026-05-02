"""Human-in-the-loop approval endpoints.

These are split into two prefixes:

* ``/api/approvals``               — operator-wide + token-action paths
* ``/api/projects/{id}/approvals`` — project-scoped list / create / act

Decisions are recorded via the service which audits + sends a confirmation
email (real SMTP if configured, file outbox fallback otherwise).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.schemas import (
    ApprovalCreateIn,
    ApprovalDecisionIn,
    ApprovalRequestOut,
)
from app.core.schemas.common import OkOut
from app.db import get_session
from app.services.approval_service import ApprovalService

global_router = APIRouter()
project_router = APIRouter()


# ---- project-scoped ----------------------------------------------------


@project_router.get("", response_model=list[ApprovalRequestOut])
def list_project_approvals(project_id: str, db: Session = Depends(get_session)):
    return ApprovalService(db).list_for_project(project_id)


@project_router.post("", response_model=ApprovalRequestOut, status_code=201)
async def create_project_approval(
    project_id: str,
    payload: ApprovalCreateIn,
    db: Session = Depends(get_session),
):
    svc = ApprovalService(db)
    try:
        gate = await svc.ensure_gate(
            project_id=project_id,
            stage_key=payload.stage_key,
            context_snapshot=payload.context_snapshot,
        )
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    if gate.approval is None:
        raise HTTPException(
            409,
            f"project not configured for gate {payload.stage_key} "
            f"(reason={gate.reason})",
        )
    return gate.approval


# ---- global ------------------------------------------------------------


@global_router.get("", response_model=list[ApprovalRequestOut])
def list_pending(db: Session = Depends(get_session)):
    return ApprovalService(db).list_pending()


@global_router.get("/{approval_id}", response_model=ApprovalRequestOut)
def get_approval(approval_id: str, db: Session = Depends(get_session)):
    try:
        return ApprovalService(db).get(approval_id)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e


@global_router.post("/{approval_id}/decision", response_model=ApprovalRequestOut)
async def decide(
    approval_id: str,
    payload: ApprovalDecisionIn,
    db: Session = Depends(get_session),
):
    svc = ApprovalService(db)
    try:
        return await svc.decide(
            approval_id,
            decision=payload.decision,
            note=payload.note,
            actor=payload.actor,
        )
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@global_router.post("/token/{token}", response_model=ApprovalRequestOut)
async def decide_by_token(
    token: str,
    payload: ApprovalDecisionIn,
    db: Session = Depends(get_session),
):
    svc = ApprovalService(db)
    try:
        approval = svc.get_by_token(token)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    try:
        return await svc.decide(
            approval.id,
            decision=payload.decision,
            note=payload.note,
            actor=payload.actor,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@global_router.get("/token/{token}")
def get_by_token(token: str, db: Session = Depends(get_session)):
    """Return the approval row for a given token. Used by the click-through
    UI and by tests."""
    try:
        return ApprovalService(db).get_by_token(token)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e


@global_router.post("/scan/reminders")
async def scan_reminders(db: Session = Depends(get_session)):
    reminded = await ApprovalService(db).reminder_scan()
    return {"reminded": [a.id for a in reminded], "count": len(reminded)}


@global_router.post("/scan/expire")
def scan_expire(db: Session = Depends(get_session)):
    expired = ApprovalService(db).expiration_scan()
    return {"expired": [a.id for a in expired], "count": len(expired)}
