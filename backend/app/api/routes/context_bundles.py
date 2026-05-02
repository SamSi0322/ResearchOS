"""Context bundle upload + inspect routes.

* ``POST /api/projects/{id}/context-bundles`` uploads a ZIP (<=512 MiB)
* ``GET``  lists them
* ``GET /{bundle_id}`` returns a single bundle
* ``DELETE /{bundle_id}`` removes the bundle and its extracted tree
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.core.schemas import ContextBundleOut, ContextBundleSummary
from app.core.schemas.common import OkOut
from app.db import get_session
from app.services.context_bundle_service import ContextBundleService

router = APIRouter()


@router.get("", response_model=list[ContextBundleSummary])
def list_bundles(project_id: str, db: Session = Depends(get_session)):
    return ContextBundleService(db).list_for_project(project_id)


@router.post("", response_model=ContextBundleOut, status_code=201)
async def upload_bundle(
    project_id: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_session),
):
    svc = ContextBundleService(db)
    try:
        return await svc.upload_zip(project_id=project_id, upload=file)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.get("/{bundle_id}", response_model=ContextBundleOut)
def get_bundle(project_id: str, bundle_id: str, db: Session = Depends(get_session)):
    svc = ContextBundleService(db)
    try:
        row = svc.get(bundle_id)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    if row.project_id != project_id:
        raise HTTPException(404, "bundle does not belong to this project")
    return row


@router.delete("/{bundle_id}", response_model=OkOut)
def delete_bundle(project_id: str, bundle_id: str, db: Session = Depends(get_session)):
    svc = ContextBundleService(db)
    try:
        row = svc.get(bundle_id)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    if row.project_id != project_id:
        raise HTTPException(404, "bundle does not belong to this project")
    svc.delete(bundle_id)
    return OkOut()
