"""End-to-end smoke test running the full pipeline in mock mode.

No network calls. No real provider credentials. Exercises:

1. Project creation
2. Mock provider credential
3. Idea generation + scoring + funnel advance
4. Spec generation
5. Code worker + experiment run (real subprocess -> stdlib train.py)
6. Result analysis + claim creation
7. Draft + review + package build + zip file on disk
"""

from __future__ import annotations

import json
import logging
import zipfile
from pathlib import Path

import pytest


@pytest.mark.asyncio
async def test_full_pipeline_mock_mode(fresh_db, caplog):
    from app.core.schemas import (
        DraftGenerateIn,
        FunnelAdvanceIn,
        IdeaGenerateIn,
        PackageCreateIn,
        ProjectCreateIn,
        ProviderCredentialIn,
        ReviewRunIn,
        RunStartIn,
        SpecGenerateIn,
    )
    from app.db.session import SessionLocal
    from app.services import (
        DraftService,
        ExperimentRunnerService,
        FunnelService,
        IdeaGenerationService,
        PackageService,
        ProviderSecretService,
        ResearchBriefService,
        ResultAnalysisService,
        ReviewService,
        SpecService,
    )

    with SessionLocal() as db:
        # --- 1. Create project --------------------------------------------
        project = ResearchBriefService(db).create_project(
            ProjectCreateIn(
                title="Mock pipeline project",
                student_name="Test Student",
                mentor_name="Mentor X",
                research_direction="Test the full ResearchOS pipeline end-to-end.",
                target_venues=["Internal"],
                constraints="<=1 CPU, stdlib only",
                exploration_strategy="breadth-first",
                budget_usd=5.0,
            )
        )
        # --- 2. Register mock provider credential -------------------------
        ProviderSecretService(db).add(
            ProviderCredentialIn(
                provider="mock", label="t", api_key="mock-key-abcdef", is_default=True
            )
        )

    with SessionLocal() as db:
        # --- 3. Idea generation + funnel ---------------------------------
        ideas = await IdeaGenerationService(db).generate(
            project.id, IdeaGenerateIn(count=6)
        )
        assert len(ideas) >= 3
        scorecards = await FunnelService(db).score(project.id, stage="S0")
        assert scorecards, "scorecards should be produced"
        res = FunnelService(db).advance(
            project.id,
            FunnelAdvanceIn(from_stage="S0", to_stage="S1", keep_count=2, auto_reject=True),
        )
        assert res["promoted"], "should promote at least one idea"
        top_idea_id = res["promoted"][0]

    with SessionLocal() as db:
        # --- 4. Spec generation -------------------------------------------
        spec = await SpecService(db).generate(
            project.id, SpecGenerateIn(idea_id=top_idea_id)
        )
        assert spec.hypothesis
        spec_id = spec.id

    with SessionLocal() as db:
        # --- 5. Run experiment --------------------------------------------
        run = await ExperimentRunnerService(db).start_and_run(
            project.id, RunStartIn(spec_id=spec_id, worker="claude_code", seed=3)
        )
        assert run.status == "succeeded", f"expected succeeded, got {run.status} ({run.summary})"
        assert run.result_class == "succeeded_valid"
        assert run.metrics, "metrics.json should be parsed"
        ws = Path(run.workspace_path)
        assert (ws / "code" / "train.py").exists()
        assert (ws / "outputs" / "metrics.json").exists()
        assert (ws / "logs" / "stdout.log").exists()
        run_id = run.id

    with SessionLocal() as db:
        # --- 6. Analyse ----------------------------------------------------
        with caplog.at_level(logging.WARNING, logger="app.services.result_analysis_service"):
            analysis = await ResultAnalysisService(db).analyze(run_id)
        assert analysis.verdict in {"promising", "inconclusive", "rejected"}
        assert analysis.claim_ids, "should have produced at least one claim"
        # The mock adapter must stay shape-compatible with the analysis service.
        # If it regresses we would see "analysis provider call failed, falling back".
        fallback_msgs = [
            rec.message
            for rec in caplog.records
            if "falling back" in rec.message and "analysis" in rec.message
        ]
        assert not fallback_msgs, (
            f"analysis provider fallback triggered unexpectedly: {fallback_msgs}"
        )

    with SessionLocal() as db:
        # --- 7. Draft ------------------------------------------------------
        draft = await DraftService(db).generate(
            project.id,
            DraftGenerateIn(manuscript_title="Mock draft", target_venue="Internal"),
        )
        assert draft.sections, "draft must have sections"
        keys = {s.key for s in draft.sections}
        assert {"abstract", "introduction", "method", "experiments", "results"} <= keys
        assert draft.mock is True  # mock adapter = mock=True

    with SessionLocal() as db:
        # --- 8. Review ----------------------------------------------------
        issues = await ReviewService(db).run_reviewers(project.id, ReviewRunIn())
        assert issues, "reviewers should produce at least one issue"

    with SessionLocal() as db:
        # --- 9. Package ----------------------------------------------------
        # P2 issues default are "warn"; allow with explicit waiver.
        pkg = await PackageService(db).build(
            project.id, PackageCreateIn(allow_with_waived_p2=True, include_mock=True)
        )
        assert pkg.status == "frozen"
        assert pkg.zip_path and Path(pkg.zip_path).exists()
        with zipfile.ZipFile(pkg.zip_path) as zf:
            names = zf.namelist()
            assert "manifest.json" in names
            assert "data/project.json" in names
            assert "data/runs.json" in names
            assert any(n.startswith("manuscript/") for n in names)
            manifest = json.loads(zf.read("manifest.json"))
            assert manifest["has_mock_artifacts"] is True
            assert manifest["counts"]["runs"] >= 1
