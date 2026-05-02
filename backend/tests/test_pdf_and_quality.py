"""Tests for PDF rendering + manuscript quality + package enrichment."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest


@pytest.mark.asyncio
async def test_package_includes_pdf_quality_and_readiness(fresh_db):
    from app.core.schemas import (
        DraftGenerateIn,
        IdeaGenerateIn,
        PackageCreateIn,
        ProjectCreateIn,
        ProviderCredentialIn,
        RunStartIn,
        SpecGenerateIn,
        ReviewRunIn,
    )
    from app.db.session import SessionLocal
    from app.services import (
        DraftService,
        ExperimentRunnerService,
        IdeaGenerationService,
        PackageService,
        ProviderSecretService,
        ResearchBriefService,
        ResultAnalysisService,
        ReviewService,
        SpecService,
    )

    with SessionLocal() as db:
        project = ResearchBriefService(db).create_project(
            ProjectCreateIn(
                title="PDF test project",
                student_name="tester",
                mentor_name="tester",
                research_direction="Validate the end-to-end PDF + quality pipeline.",
                target_venues=["Internal"],
            )
        )
        ProviderSecretService(db).add(
            ProviderCredentialIn(
                provider="mock", label="pdf-test", api_key="mock-pdf", is_default=True
            )
        )

    with SessionLocal() as db:
        ideas = await IdeaGenerationService(db).generate(
            project.id, IdeaGenerateIn(count=1)
        )
    idea = ideas[0]

    with SessionLocal() as db:
        spec = await SpecService(db).generate(
            project.id, SpecGenerateIn(idea_id=idea.id)
        )

    with SessionLocal() as db:
        run = await ExperimentRunnerService(db).start_and_run(
            project.id,
            RunStartIn(spec_id=spec.id, worker="claude_code", seed=1),
        )
    assert run.status == "succeeded"

    with SessionLocal() as db:
        await ResultAnalysisService(db).analyze(run.id)

    with SessionLocal() as db:
        from app.core.models import DraftSection

        # Skip the polish pass in tests to keep this focused and deterministic.
        draft = await DraftService(db).generate(
            project.id,
            DraftGenerateIn(
                manuscript_title="PDF test draft",
                target_venue="Internal",
                extra_instructions="__skip_polish__",
            ),
        )
        section_count = (
            db.query(DraftSection).filter(DraftSection.draft_id == draft.id).count()
        )
    assert section_count > 0, "expected draft sections"

    with SessionLocal() as db:
        await ReviewService(db).run_reviewers(project.id, ReviewRunIn())

    with SessionLocal() as db:
        pkg = await PackageService(db).build(
            project.id,
            PackageCreateIn(allow_with_waived_p2=True, include_mock=True),
        )
    assert pkg.zip_path and Path(pkg.zip_path).exists()

    with zipfile.ZipFile(pkg.zip_path) as zf:
        names = zf.namelist()
        pdfs = [n for n in names if n.startswith("manuscript/") and n.endswith(".pdf")]
        assert pdfs, f"no PDF in zip (entries: {names[:20]})"
        # PDF is non-trivial in size (warning + title + sections).
        pdf_bytes = zf.read(pdfs[0])
        assert len(pdf_bytes) > 1500

        # Quality / review / readiness must all be embedded.
        assert "manuscript/quality.json" in names
        assert "manuscript/review_summary.json" in names
        assert "manuscript/readiness.json" in names

        manifest = json.loads(zf.read("manifest.json"))
        ms = manifest.get("manuscript", {})
        assert ms.get("has_pdf") is True
        assert ms.get("pdf_path") == pdfs[0]
        assert ms.get("quality_report_path") == "manuscript/quality.json"
        assert ms.get("review_summary_path") == "manuscript/review_summary.json"
        assert ms.get("readiness_summary_path") == "manuscript/readiness.json"
        # Readiness decision is exposed at the manifest top level too.
        assert manifest.get("readiness_tier")
        assert manifest.get("package_decision")

    # PDF bytes open with the standard %PDF- header.
    assert pdf_bytes.startswith(b"%PDF-")
    # Every piece of the package must be free of raw key fragments.
    for n in names:
        data = zf.read(n) if False else None  # placeholder; zf closed
    # Reopen for a leakage scan
    with zipfile.ZipFile(pkg.zip_path) as zf:
        haystacks = [zf.read(n) for n in zf.namelist()]
    for blob in haystacks:
        lowered = blob.decode("utf-8", "replace").lower()
        assert "sk-proj-" not in lowered
        assert "sk-ant-" not in lowered


@pytest.mark.asyncio
async def test_quality_service_counts_placeholders(fresh_db):
    """If the draft service produces placeholders, the quality service sees them."""
    from app.core.schemas import (
        DraftGenerateIn,
        ProjectCreateIn,
        ProviderCredentialIn,
    )
    from app.db.session import SessionLocal
    from app.services import (
        DraftService,
        ManuscriptQualityService,
        ProviderSecretService,
        ResearchBriefService,
    )

    with SessionLocal() as db:
        project = ResearchBriefService(db).create_project(
            ProjectCreateIn(
                title="Placeholder counting",
                student_name="tester",
                mentor_name="tester",
                research_direction="We generate a draft with zero claims to force placeholders.",
            )
        )
        ProviderSecretService(db).add(
            ProviderCredentialIn(
                provider="mock", label="q", api_key="mock", is_default=True
            )
        )
    with SessionLocal() as db:
        # No runs + no claims yet -> draft falls back heavily.
        await DraftService(db).generate(
            project.id,
            DraftGenerateIn(extra_instructions="__skip_polish__"),
        )
    with SessionLocal() as db:
        report = ManuscriptQualityService(db).latest_report(project.id)
    assert report is not None
    # Without claims the evidence coverage is 0 and at least one section
    # reports [PLACEHOLDER]; completeness should stay bounded.
    assert report.placeholder_count >= 1
    assert report.evidence_coverage_ratio == 0.0
    assert 0.0 <= report.draft_completeness_score <= 0.9


def test_unsupported_numbers_heuristic_blocks_fabrication():
    """The polish-pass guard revokes text with novel numeric tokens."""
    from types import SimpleNamespace

    from app.services.draft_service import _has_unsupported_numbers

    class FakeClaim:
        def __init__(self, text="", value=""):
            self.text = text
            self.value = value

    # Novel big numbers (NOT in any claim) should be blocked.
    assert _has_unsupported_numbers(
        "We observe a 87.4% improvement over baseline.",
        [FakeClaim(text="no numbers here", value="")],
    )

    # Numbers that DO appear in claim text are allowed.
    assert not _has_unsupported_numbers(
        "We report 0.0342 delta.",
        [FakeClaim(text="Variant delta is 0.0342", value="+0.0342")],
    )

    # Small enumerator integers (1, 2) are whitelisted.
    assert not _has_unsupported_numbers("Step 1. Do X.  Step 2. Do Y.", [])
