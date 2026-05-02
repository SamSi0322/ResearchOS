"""Context bundle upload + safe extraction tests."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest


def _make_zip_bytes(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


async def _upload(db, project_id: str, data: bytes, filename: str = "bundle.zip"):
    from app.services.context_bundle_service import ContextBundleService

    class _InMemoryUpload:
        def __init__(self, payload: bytes, name: str) -> None:
            self.filename = name
            self._buf = io.BytesIO(payload)

        async def read(self, size: int = -1) -> bytes:
            return self._buf.read(size)

    upload = _InMemoryUpload(data, filename)
    svc = ContextBundleService(db)
    return await svc.upload_zip(project_id=project_id, upload=upload)


async def _seed_project(db) -> str:
    from app.core.schemas import ProjectCreateIn
    from app.services import ResearchBriefService

    project = ResearchBriefService(db).create_project(
        ProjectCreateIn(
            title="Bundle test",
            student_name="tester",
            mentor_name="tester",
            research_direction="test bundles",
        )
    )
    return project.id


@pytest.mark.asyncio
async def test_upload_valid_zip_is_indexed(fresh_db):
    from app.db.session import SessionLocal

    with SessionLocal() as db:
        pid = await _seed_project(db)

    data = _make_zip_bytes(
        {
            "notes/alpha.md": b"# Alpha\nSome background notes about our topic.",
            "notes/beta.txt": b"Secondary file describing prior work.",
            "image.png": b"\x89PNG\r\n\x1a\n" + b"\x00" * 64,
        }
    )
    with SessionLocal() as db:
        bundle = await _upload(db, pid, data)

    assert bundle.extraction_status == "ok"
    assert bundle.text_file_count == 2
    assert bundle.total_text_chars > 0
    # Manifest has all 3 files + sha256 entries.
    assert len(bundle.manifest) == 3
    assert all("sha256" in e for e in bundle.manifest)


@pytest.mark.asyncio
async def test_rejects_non_zip_header(fresh_db):
    from app.db.session import SessionLocal

    with SessionLocal() as db:
        pid = await _seed_project(db)
    with SessionLocal() as db:
        with pytest.raises(ValueError, match="ZIP header"):
            await _upload(db, pid, b"not actually a zip at all", filename="evil.zip")


@pytest.mark.asyncio
async def test_rejects_wrong_extension(fresh_db):
    from app.db.session import SessionLocal

    with SessionLocal() as db:
        pid = await _seed_project(db)
    with SessionLocal() as db:
        with pytest.raises(ValueError, match=".zip"):
            await _upload(db, pid, _make_zip_bytes({"x": b"y"}), filename="bundle.txt")


@pytest.mark.asyncio
async def test_rejects_oversize(fresh_db, monkeypatch):
    import os
    import secrets

    from app.config import reset_settings_cache
    from app.db.session import SessionLocal

    monkeypatch.setenv("RESEARCHOS_CONTEXT_BUNDLE_MAX_BYTES", "4096")
    reset_settings_cache()
    try:
        with SessionLocal() as db:
            pid = await _seed_project(db)

        # Random (incompressible) payload so the zip ends up well above the
        # 4 KiB cap even with DEFLATE.
        big = _make_zip_bytes({"big.bin": secrets.token_bytes(16 * 1024)})
        assert len(big) > 4096, "test payload must exceed the cap"
        with SessionLocal() as db:
            with pytest.raises(ValueError, match="exceeds"):
                await _upload(db, pid, big)
    finally:
        monkeypatch.delenv("RESEARCHOS_CONTEXT_BUNDLE_MAX_BYTES", raising=False)
        reset_settings_cache()


@pytest.mark.asyncio
async def test_zip_slip_entries_are_skipped(fresh_db):
    """Paths that try to escape the extraction root must be dropped, not
    extracted, and should not appear in the manifest."""
    from app.db.session import SessionLocal

    # Craft a zip whose entries attempt path traversal + absolute paths.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("safe.txt", b"ok")
        # Traversal attempt.
        zf.writestr("../escape.txt", b"evil")
        zf.writestr("sub/../../evil.txt", b"evil")
        # Absolute path (POSIX style).
        zf.writestr("/etc/passwd", b"evil")
    data = buf.getvalue()

    with SessionLocal() as db:
        pid = await _seed_project(db)
    with SessionLocal() as db:
        bundle = await _upload(db, pid, data)
    # Only the safe entry survives.
    paths = [e["path"] for e in bundle.manifest]
    assert bundle.extraction_status == "ok", (
        f"expected ok, got {bundle.extraction_status}: {bundle.extraction_error}"
    )
    assert paths == ["safe.txt"], f"manifest={bundle.manifest}"

    # Verify nothing was written outside the extracted root.
    extracted_root = Path(bundle.extracted_path)
    for entry in bundle.manifest:
        assert (extracted_root / entry["path"]).resolve().is_relative_to(
            extracted_root.resolve()
        )


@pytest.mark.asyncio
async def test_bundle_snippets_feed_idea_generation(fresh_db):
    """The ``load_bundle_context`` helper returns indexed snippets the
    IdeaGenerationService can paste into a prompt."""
    from app.db.session import SessionLocal
    from app.services.context_bundle_service import load_bundle_context

    with SessionLocal() as db:
        pid = await _seed_project(db)

    data = _make_zip_bytes(
        {"notes/brief.md": b"We care about tokenizer drift under register shifts."}
    )
    with SessionLocal() as db:
        await _upload(db, pid, data)

    with SessionLocal() as db:
        ctx = load_bundle_context(db, project_id=pid, char_budget=2000)
    snippets = ctx.get("snippets") or []
    assert snippets, "expected at least one snippet"
    assert any("tokenizer" in (s.get("text") or "").lower() for s in snippets)
