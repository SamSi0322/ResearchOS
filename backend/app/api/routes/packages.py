from __future__ import annotations

import json
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from sqlalchemy.orm import Session

from app.core.models import Artifact
from app.core.schemas import PackageCreateIn, PackageOut
from app.db import get_session
from app.services import PackageService

router = APIRouter()


@router.get("", response_model=list[PackageOut])
def list_packages(project_id: str, db: Session = Depends(get_session)):
    return PackageService(db).list(project_id)


@router.post("/build", response_model=PackageOut)
async def build_package(
    project_id: str, payload: PackageCreateIn, db: Session = Depends(get_session)
):
    from app.services.package_service import PackageBlockedError

    try:
        return await PackageService(db).build(project_id, payload)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    except PackageBlockedError as e:
        raise HTTPException(
            409,
            detail={
                "error": "blocked_by_approval_gate",
                "stage_key": e.stage_key,
                "approval_id": e.approval_id,
                "status": e.status,
                "reason": e.reason,
            },
        ) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.get("/{package_id}", response_model=PackageOut)
def get_package(project_id: str, package_id: str, db: Session = Depends(get_session)):
    try:
        return PackageService(db).get(package_id)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e


@router.get("/{package_id}/download")
def download_package(
    project_id: str, package_id: str, db: Session = Depends(get_session)
):
    try:
        pkg = PackageService(db).get(package_id)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    if not pkg.zip_path:
        raise HTTPException(409, "package has no zip path")
    zp = Path(pkg.zip_path)
    if not zp.exists():
        raise HTTPException(410, "package zip no longer on disk")
    return FileResponse(
        str(zp),
        media_type="application/zip",
        filename=f"{project_id}_package_v{pkg.version}.zip",
    )


@router.get("/{package_id}/manifest")
def get_manifest(project_id: str, package_id: str, db: Session = Depends(get_session)) -> dict:
    """Return the manifest.json contents without making the caller download the ZIP."""
    try:
        pkg = PackageService(db).get(package_id)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    if not pkg.zip_path or not Path(pkg.zip_path).exists():
        raise HTTPException(410, "package zip no longer on disk")
    with zipfile.ZipFile(pkg.zip_path) as zf:
        try:
            return json.loads(zf.read("manifest.json"))
        except KeyError:
            raise HTTPException(500, "manifest.json missing from package zip")


@router.get("/{package_id}/manuscript.pdf")
def download_manuscript_pdf(
    project_id: str, package_id: str, db: Session = Depends(get_session)
):
    """Stream the packaged manuscript PDF directly (no full ZIP download)."""
    try:
        pkg = PackageService(db).get(package_id)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    if not pkg.zip_path or not Path(pkg.zip_path).exists():
        raise HTTPException(410, "package zip no longer on disk")
    with zipfile.ZipFile(pkg.zip_path) as zf:
        pdfs = [n for n in zf.namelist() if n.startswith("manuscript/") and n.endswith(".pdf")]
        if not pdfs:
            raise HTTPException(404, "no PDF in this package")
        # Pick the highest-versioned one (names end like draft_vN.pdf).
        pdfs.sort()
        target = pdfs[-1]
        pdf_bytes = zf.read(target)
    # Write to a temp file-backed response so FastAPI streams cleanly.
    import tempfile

    tmp = tempfile.NamedTemporaryFile(prefix="researchos-pdf-", suffix=".pdf", delete=False)
    tmp.write(pdf_bytes)
    tmp.flush()
    tmp.close()
    return FileResponse(
        tmp.name,
        media_type="application/pdf",
        filename=f"{project_id}_{Path(target).name}",
    )


@router.get("/{package_id}/manuscript.md", response_class=PlainTextResponse)
def preview_manuscript_markdown(
    project_id: str, package_id: str, db: Session = Depends(get_session)
) -> str:
    try:
        pkg = PackageService(db).get(package_id)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    if not pkg.zip_path or not Path(pkg.zip_path).exists():
        raise HTTPException(410, "package zip no longer on disk")
    with zipfile.ZipFile(pkg.zip_path) as zf:
        md = [n for n in zf.namelist() if n.startswith("manuscript/") and n.endswith(".md")]
        if not md:
            raise HTTPException(404, "no markdown manuscript in this package")
        md.sort()
        return zf.read(md[-1]).decode("utf-8", errors="replace")


@router.get("/manuscript/latest-pdf")
def latest_manuscript_pdf(project_id: str, db: Session = Depends(get_session)):
    """Return the newest manuscript PDF stored as an artifact, regardless of package."""
    art = (
        db.query(Artifact)
        .filter(
            Artifact.project_id == project_id,
            Artifact.kind == "manuscript_pdf",
        )
        .order_by(Artifact.created_at.desc())
        .first()
    )
    if art is None:
        raise HTTPException(404, "no manuscript PDF has been generated yet")
    p = Path(art.path)
    if not p.exists():
        raise HTTPException(410, "manuscript PDF no longer on disk")
    return FileResponse(str(p), media_type="application/pdf", filename=art.name)
