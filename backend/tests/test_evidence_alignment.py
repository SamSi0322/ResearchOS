"""Evidence alignment: ensure claim_refs are populated after draft assembly."""

from __future__ import annotations

from dataclasses import dataclass

import pytest


@dataclass
class _FakeClaim:
    id: str
    text: str
    value: str = ""
    run_id: str | None = None


@dataclass
class _FakeSection:
    id: str
    draft_id: str
    key: str
    title: str
    content: str
    order_index: int
    claim_refs: list
    evidence_refs: list = None  # type: ignore[assignment]


def test_value_match_adds_claim_ref():
    from app.services.evidence_alignment import align_sections_with_claims

    sec = _FakeSection(
        id="s1",
        draft_id="d1",
        key="results",
        title="Results",
        content="We observe a delta of +0.0342 vs baseline.",
        order_index=0,
        claim_refs=[],
    )
    claim = _FakeClaim(id="c1", text="variant beats baseline", value="+0.0342")
    report = align_sections_with_claims([sec], [claim], structural_fallback=False)
    assert report.added_refs == 1
    assert "c1" in sec.claim_refs


def test_keyword_match_requires_two_overlaps():
    from app.services.evidence_alignment import align_sections_with_claims

    sec = _FakeSection(
        id="s1",
        draft_id="d1",
        key="discussion",
        title="Discussion",
        content=(
            "Tokenizer drift interacts with register shifts on social corpora."
        ),
        order_index=0,
        claim_refs=[],
    )
    # matches two strong tokens (tokenizer, register)
    c_hit = _FakeClaim(id="hit", text="Tokenizer register drift is significant.", value="")
    # matches only a single token — should NOT link
    c_single = _FakeClaim(id="single", text="Calibration improved.", value="")
    report = align_sections_with_claims([sec], [c_hit, c_single], structural_fallback=False)
    assert "hit" in sec.claim_refs
    assert "single" not in sec.claim_refs


def test_structural_fallback_links_run_claims_to_experiments():
    from app.services.evidence_alignment import align_sections_with_claims

    sec_ex = _FakeSection(
        id="s_ex",
        draft_id="d1",
        key="experiments",
        title="Experiments",
        content="Two seeds, small toy task.",
        order_index=1,
        claim_refs=[],
    )
    sec_intro = _FakeSection(
        id="s_intro",
        draft_id="d1",
        key="introduction",
        title="Introduction",
        content="We study X.",
        order_index=0,
        claim_refs=[],
    )
    claims = [
        _FakeClaim(id="c1", text="delta something", value="", run_id="run_a"),
        _FakeClaim(id="c2", text="other thing", value="", run_id="run_b"),
    ]
    align_sections_with_claims([sec_intro, sec_ex], claims, structural_fallback=True)
    assert set(sec_ex.claim_refs) == {"c1", "c2"}
    # Introduction without a textual match stays empty.
    assert sec_intro.claim_refs == []


@pytest.mark.asyncio
async def test_evidence_coverage_is_nonzero_after_draft(fresh_db):
    """Run the full draft path and confirm alignment populates claim_refs."""
    from app.core.models import DraftSection
    from app.core.schemas import (
        DraftGenerateIn,
        IdeaGenerateIn,
        ProjectCreateIn,
        ProviderCredentialIn,
        RunStartIn,
        SpecGenerateIn,
    )
    from app.db.session import SessionLocal
    from app.services import (
        DraftService,
        ExperimentRunnerService,
        IdeaGenerationService,
        ManuscriptQualityService,
        ProviderSecretService,
        ResearchBriefService,
        ResultAnalysisService,
        SpecService,
    )

    with SessionLocal() as db:
        ProviderSecretService(db).add(
            ProviderCredentialIn(
                provider="mock", label="ev", api_key="mock-ev", is_default=True
            )
        )
        project = ResearchBriefService(db).create_project(
            ProjectCreateIn(
                title="Coverage test",
                student_name="x",
                mentor_name="x",
                research_direction="test evidence alignment",
            )
        )
    with SessionLocal() as db:
        ideas = await IdeaGenerationService(db).generate(
            project.id, IdeaGenerateIn(count=1)
        )
    with SessionLocal() as db:
        spec = await SpecService(db).generate(
            project.id, SpecGenerateIn(idea_id=ideas[0].id)
        )
    with SessionLocal() as db:
        run = await ExperimentRunnerService(db).start_and_run(
            project.id,
            RunStartIn(spec_id=spec.id, worker="claude_code", seed=0),
        )
    with SessionLocal() as db:
        await ResultAnalysisService(db).analyze(run.id)
    with SessionLocal() as db:
        await DraftService(db).generate(
            project.id,
            DraftGenerateIn(extra_instructions="__skip_polish__"),
        )

    # Evidence coverage must be non-zero because the alignment pass linked
    # the run's claims into the experiments/results sections at minimum.
    with SessionLocal() as db:
        report = ManuscriptQualityService(db).latest_report(project.id)
    assert report is not None
    assert report.evidence_coverage_ratio > 0.0, (
        f"expected evidence coverage > 0, got {report.evidence_coverage_ratio}"
    )

    # At least one section has a non-empty claim_refs list after alignment.
    with SessionLocal() as db:
        sections = db.query(DraftSection).all()
    assert any(s.claim_refs for s in sections), (
        "expected at least one section to carry claim_refs after alignment"
    )
