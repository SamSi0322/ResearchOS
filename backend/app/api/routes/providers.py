from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.models import ProviderValidationLog
from app.core.schemas import (
    MessageOut,
    ProviderCredentialIn,
    ProviderCredentialOut,
    ProviderCredentialUpdateIn,
    ProviderTestIn,
    ProviderValidationLogOut,
    ProviderValidationResult,
)
from app.db import get_session
from app.services import ProviderSecretService

router = APIRouter()


@router.get("", response_model=list[ProviderCredentialOut])
def list_credentials(db: Session = Depends(get_session)):
    svc = ProviderSecretService(db)
    return svc.list()


@router.post("", response_model=ProviderCredentialOut, status_code=status.HTTP_201_CREATED)
def add_credential(payload: ProviderCredentialIn, db: Session = Depends(get_session)):
    svc = ProviderSecretService(db)
    cred = svc.add(payload)
    # Deliberately do NOT echo api_key back.
    return cred


@router.put("/{credential_id}", response_model=ProviderCredentialOut)
def update_credential(
    credential_id: str,
    payload: ProviderCredentialUpdateIn,
    db: Session = Depends(get_session),
):
    svc = ProviderSecretService(db)
    try:
        return svc.update(credential_id, payload)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e


@router.delete("/{credential_id}", response_model=MessageOut)
def delete_credential(credential_id: str, db: Session = Depends(get_session)):
    svc = ProviderSecretService(db)
    try:
        svc.delete(credential_id)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    return MessageOut(message="deleted")


@router.get(
    "/validation/latest",
    response_model=list[ProviderValidationLogOut],
)
def latest_validation_per_provider(db: Session = Depends(get_session)):
    """Return the most recent validation row for each distinct provider.

    Useful for the UI header strip that wants one "last known state" per
    provider (OpenAI / Anthropic / mock) without pulling the full history.
    Empty list when no validations have been recorded yet.
    """
    rows = (
        db.query(ProviderValidationLog)
        .order_by(ProviderValidationLog.created_at.desc())
        .all()
    )
    latest_by_provider: dict[str, ProviderValidationLog] = {}
    for row in rows:
        latest_by_provider.setdefault(row.provider, row)
    # Return stable provider order so the UI doesn't flicker.
    return [latest_by_provider[k] for k in sorted(latest_by_provider.keys())]


@router.get(
    "/validation/history",
    response_model=list[ProviderValidationLogOut],
)
def list_validation_history(
    credential_id: str | None = Query(default=None),
    provider: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    db: Session = Depends(get_session),
):
    """Persistent history of past ``/providers/test`` + ``/smoke/ping`` calls.

    The Settings page uses this to replace the in-memory "Last validation"
    cell with one that survives a refresh. Filter by ``credential_id`` to
    narrow to a single row's history or by ``provider`` for the smoke ping
    flow (which has no credential_id).
    """
    q = db.query(ProviderValidationLog)
    if credential_id:
        q = q.filter(ProviderValidationLog.credential_id == credential_id)
    if provider:
        q = q.filter(ProviderValidationLog.provider == provider)
    return (
        q.order_by(ProviderValidationLog.created_at.desc()).limit(limit).all()
    )


@router.post("/test", response_model=ProviderValidationResult)
async def test_credential(
    payload: ProviderTestIn, db: Session = Depends(get_session)
) -> ProviderValidationResult:
    """Validate a stored credential against a dedicated low-cost test model.

    Always returns HTTP 200 with a structured ``ProviderValidationResult``.
    ``category`` tells the caller whether the credential is valid, the chosen
    model is unavailable, the provider is unreachable, or a config problem
    prevented the call. This is the "does this key work?" endpoint — for
    "does the current runtime route work?" see ``/api/smoke/ping``.
    """
    svc = ProviderSecretService(db)
    try:
        return await svc.test_connection(payload)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
