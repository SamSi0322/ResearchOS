"""Upload + safely extract a ZIP of background material.

Rules:

* ZIP-only. We trust neither the filename extension alone nor the client's
  ``Content-Type`` - the first 4 bytes must be the PK local-file header.
* Size-capped. Upload is streamed in chunks and an incremental byte counter
  aborts if it exceeds ``settings.context_bundle_max_bytes`` (default
  512 MiB).
* Zip-slip safe. Every extracted path is resolved against the extraction
  root; anything that escapes is skipped.
* Symlinks inside the archive are ignored (we only extract regular files).
* Very large archives are still tolerated; we cap the number of extracted
  files at ``settings.context_bundle_max_extracted_files``.

Text snippets:
* A tiny subset of text-like files (``.txt .md .json .yaml .yml .csv``) is
  read up to ``settings.context_bundle_snippet_char_limit`` characters each
  and kept on the bundle row as ``selected_snippets`` so the idea-generation
  service can splice a compact summary into its prompt.
"""

from __future__ import annotations

import hashlib
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Iterator

from fastapi import UploadFile
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.enums import AuditKind, ContextBundleStatus
from app.core.models import ContextBundle, StudentProject
from app.services.audit_service import AuditService
from app.storage import get_artifact_store
from app.utils import get_logger, new_id

logger = get_logger(__name__)


_PK_HEADER = b"PK\x03\x04"
_TEXT_EXTENSIONS = {".txt", ".md", ".json", ".yaml", ".yml", ".csv", ".log"}


@dataclass
class ExtractResult:
    status: str
    manifest: list[dict]
    selected_snippets: list[dict]
    total_text_chars: int
    text_file_count: int
    extracted_path: str | None
    error: str | None = None


class ContextBundleService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.settings = get_settings()
        self.audit = AuditService(db)

    # --- upload --------------------------------------------------------

    async def upload_zip(
        self,
        *,
        project_id: str,
        upload: UploadFile,
    ) -> ContextBundle:
        project = (
            self.db.query(StudentProject)
            .filter(StudentProject.id == project_id)
            .first()
        )
        if project is None:
            raise LookupError(f"project not found: {project_id}")

        filename = (upload.filename or "bundle.zip").strip()
        if not filename.lower().endswith(".zip"):
            raise ValueError("Only .zip files are accepted as context bundles.")

        settings = self.settings
        max_bytes = int(settings.context_bundle_max_bytes)

        # Stream into a project-scoped location under the artifact store.
        store = get_artifact_store()
        root = store.project_root(project_id) / "context"
        root.mkdir(parents=True, exist_ok=True)
        tmp_path = root / f"upload_{new_id('bnd')}.tmp"

        sha = hashlib.sha256()
        size = 0
        header_ok: bool | None = None
        try:
            with tmp_path.open("wb") as out:
                while True:
                    chunk = await upload.read(1 << 20)  # 1 MiB chunks
                    if not chunk:
                        break
                    if header_ok is None:
                        header_ok = chunk[:4].startswith(_PK_HEADER)
                        if not header_ok:
                            raise ValueError(
                                "Uploaded file does not begin with a ZIP header."
                            )
                    size += len(chunk)
                    if size > max_bytes:
                        raise ValueError(
                            f"Uploaded file exceeds the {max_bytes}-byte limit."
                        )
                    sha.update(chunk)
                    out.write(chunk)
        except Exception:
            # Clean up partial file on any validation failure.
            tmp_path.unlink(missing_ok=True)
            raise

        if size == 0:
            tmp_path.unlink(missing_ok=True)
            raise ValueError("Uploaded file was empty.")
        if header_ok is not True:
            tmp_path.unlink(missing_ok=True)
            raise ValueError("Uploaded file does not begin with a ZIP header.")

        content_hash = sha.hexdigest()
        final_zip = root / f"{content_hash[:16]}.zip"
        if final_zip.exists():
            # dedupe: same bytes uploaded twice
            tmp_path.unlink(missing_ok=True)
        else:
            tmp_path.replace(final_zip)

        bundle = ContextBundle(
            id=new_id("bnd"),
            project_id=project_id,
            filename=filename,
            content_hash=content_hash,
            size_bytes=size,
            storage_path=str(final_zip),
            extraction_status=ContextBundleStatus.pending.value,
        )
        self.db.add(bundle)
        self.db.flush()

        self.audit.log(
            project_id=project_id,
            kind=AuditKind.context_bundle_uploaded,
            message=f"context bundle uploaded: {filename} ({size} bytes)",
            subject_kind="context_bundle",
            subject_id=bundle.id,
            payload={"size_bytes": size, "content_hash": content_hash},
        )
        self.db.commit()

        # Extract eagerly; failures leave the zip and mark status.
        try:
            result = self._extract_and_index(bundle)
            bundle.extraction_status = result.status
            bundle.manifest = result.manifest
            bundle.selected_snippets = result.selected_snippets
            bundle.total_text_chars = result.total_text_chars
            bundle.text_file_count = result.text_file_count
            bundle.extracted_path = result.extracted_path
            if result.error:
                bundle.extraction_error = result.error
                self.audit.log(
                    project_id=project_id,
                    kind=AuditKind.context_bundle_failed,
                    message=f"context bundle extraction failed: {result.error}",
                    subject_kind="context_bundle",
                    subject_id=bundle.id,
                    payload={"error": result.error},
                )
            else:
                self.audit.log(
                    project_id=project_id,
                    kind=AuditKind.context_bundle_extracted,
                    message=(
                        f"context bundle extracted: {len(result.manifest)} files, "
                        f"{result.text_file_count} text"
                    ),
                    subject_kind="context_bundle",
                    subject_id=bundle.id,
                    payload={
                        "file_count": len(result.manifest),
                        "text_file_count": result.text_file_count,
                        "total_text_chars": result.total_text_chars,
                    },
                )
        except Exception as e:  # noqa: BLE001
            bundle.extraction_status = ContextBundleStatus.failed.value
            bundle.extraction_error = str(e)
            self.audit.log(
                project_id=project_id,
                kind=AuditKind.context_bundle_failed,
                message=f"context bundle extraction crashed: {e}",
                subject_kind="context_bundle",
                subject_id=bundle.id,
            )
        self.db.commit()
        return bundle

    def list_for_project(self, project_id: str) -> list[ContextBundle]:
        return (
            self.db.query(ContextBundle)
            .filter(ContextBundle.project_id == project_id)
            .order_by(ContextBundle.created_at.desc())
            .all()
        )

    def get(self, bundle_id: str) -> ContextBundle:
        row = (
            self.db.query(ContextBundle)
            .filter(ContextBundle.id == bundle_id)
            .first()
        )
        if row is None:
            raise LookupError(f"context bundle not found: {bundle_id}")
        return row

    def delete(self, bundle_id: str) -> None:
        row = self.get(bundle_id)
        # Remove files first; if unlink fails we still drop the DB row so
        # the operator can retry the upload.
        try:
            if row.extracted_path:
                shutil.rmtree(row.extracted_path, ignore_errors=True)
            Path(row.storage_path).unlink(missing_ok=True)
        except Exception as e:  # noqa: BLE001
            logger.warning("bundle file cleanup failed: %s", e)
        self.db.delete(row)
        self.db.commit()

    # --- extraction ----------------------------------------------------

    def _extract_and_index(self, bundle: ContextBundle) -> ExtractResult:
        zip_path = Path(bundle.storage_path)
        target = zip_path.parent / f"{bundle.content_hash[:16]}_extracted"
        target.mkdir(parents=True, exist_ok=True)

        max_files = int(self.settings.context_bundle_max_extracted_files)
        snippet_limit = int(self.settings.context_bundle_snippet_char_limit)

        manifest: list[dict] = []
        snippets: list[dict] = []
        total_text_chars = 0
        text_file_count = 0

        try:
            with zipfile.ZipFile(zip_path) as zf:
                members = zf.infolist()
                if len(members) > max_files:
                    return ExtractResult(
                        status=ContextBundleStatus.deferred.value,
                        manifest=[],
                        selected_snippets=[],
                        total_text_chars=0,
                        text_file_count=0,
                        extracted_path=None,
                        error=(
                            f"archive has {len(members)} entries, exceeds "
                            f"max_extracted_files={max_files}"
                        ),
                    )
                for m in members:
                    if m.is_dir():
                        continue
                    safe_rel = _safe_member_name(m.filename)
                    if safe_rel is None:
                        logger.info(
                            "skipping unsafe zip entry",
                            extra={"bundle_id": bundle.id, "entry_name": m.filename},
                        )
                        continue
                    dst = (target / safe_rel).resolve()
                    try:
                        dst.relative_to(target.resolve())
                    except ValueError:
                        logger.info(
                            "skipping zip-slip entry",
                            extra={"bundle_id": bundle.id, "entry_name": m.filename},
                        )
                        continue
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(m) as src, dst.open("wb") as out:
                        shutil.copyfileobj(src, out)

                    rel_posix = safe_rel.as_posix()
                    size_bytes = dst.stat().st_size
                    entry = {
                        "path": rel_posix,
                        "size_bytes": size_bytes,
                        "sha256": _file_sha256(dst),
                    }
                    if dst.suffix.lower() in _TEXT_EXTENSIONS and size_bytes <= 2 * 1024 * 1024:
                        try:
                            text = dst.read_text(encoding="utf-8", errors="replace")
                        except OSError:
                            text = ""
                        if text:
                            snippet = text[:snippet_limit]
                            snippets.append(
                                {
                                    "path": rel_posix,
                                    "text": snippet,
                                    "chars": len(snippet),
                                }
                            )
                            total_text_chars += len(snippet)
                            text_file_count += 1
                    manifest.append(entry)
        except zipfile.BadZipFile:
            return ExtractResult(
                status=ContextBundleStatus.failed.value,
                manifest=[],
                selected_snippets=[],
                total_text_chars=0,
                text_file_count=0,
                extracted_path=None,
                error="invalid zip archive",
            )

        return ExtractResult(
            status=ContextBundleStatus.ok.value,
            manifest=manifest,
            selected_snippets=snippets,
            total_text_chars=total_text_chars,
            text_file_count=text_file_count,
            extracted_path=str(target),
        )


def _safe_member_name(name: str) -> PurePosixPath | None:
    """Return a relative PurePosixPath or None if the entry is unsafe."""
    if not name or name.endswith("/") or name.endswith("\\"):
        # pure directory entries handled by is_dir check upstream
        return None
    # Normalise backslashes + collapse ..
    candidate = name.replace("\\", "/")
    if candidate.startswith("/") or (len(candidate) > 2 and candidate[1] == ":"):
        return None
    parts = [p for p in PurePosixPath(candidate).parts if p not in ("", ".")]
    if not parts:
        return None
    if any(p == ".." for p in parts):
        return None
    return PurePosixPath(*parts)


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --- prompt helpers used by IdeaGenerationService ---------------------


def load_bundle_context(
    db: Session, *, project_id: str, char_budget: int = 4000
) -> dict:
    """Collect a compact digest of all OK context bundles for a project.

    The digest is intentionally small; it is designed to be inlined into an
    idea-generation prompt without blowing the smoke-mode prompt budget.
    Returns ``{"files": [{path, size}], "snippets": [{path, text}], "char_budget"}``.
    """
    bundles = (
        db.query(ContextBundle)
        .filter(
            ContextBundle.project_id == project_id,
            ContextBundle.extraction_status == ContextBundleStatus.ok.value,
        )
        .order_by(ContextBundle.created_at.asc())
        .all()
    )
    files: list[dict] = []
    snippets: list[dict] = []
    budget = max(500, int(char_budget))
    used = 0
    for b in bundles:
        for entry in b.manifest or []:
            files.append(entry)
        for snip in b.selected_snippets or []:
            remaining = budget - used
            if remaining <= 0:
                break
            text = str(snip.get("text", ""))
            if not text:
                continue
            truncated = text[:remaining]
            used += len(truncated)
            snippets.append({"path": snip.get("path"), "text": truncated})
    return {"files": files, "snippets": snippets, "char_budget": budget}
