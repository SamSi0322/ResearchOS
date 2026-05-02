"""Multi-idea concurrency test using the deterministic mock adapter."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_batch_runs_two_ideas_in_parallel(fresh_db):
    from app.core.schemas import (
        IdeaGenerateIn,
        ProjectCreateIn,
        ProviderCredentialIn,
    )
    from app.db.session import SessionLocal
    from app.services import (
        BatchOrchestratorService,
        IdeaGenerationService,
        ProviderSecretService,
        ResearchBriefService,
    )

    with SessionLocal() as db:
        project = ResearchBriefService(db).create_project(
            ProjectCreateIn(
                title="Batch test project",
                student_name="internal",
                mentor_name="internal",
                research_direction="Validate batch orchestration locally.",
                target_venues=["Internal"],
            )
        )
        ProviderSecretService(db).add(
            ProviderCredentialIn(
                provider="mock", label="batch-test", api_key="mock-batch", is_default=True
            )
        )

    with SessionLocal() as db:
        ideas = await IdeaGenerationService(db).generate(
            project.id, IdeaGenerateIn(count=3)
        )
    idea_ids = [i.id for i in ideas[:2]]

    with SessionLocal() as db:
        outcomes = await BatchOrchestratorService(db).run_batch(
            project_id=project.id,
            idea_ids=idea_ids,
            worker="claude_code",
            concurrency=2,
        )

    assert len(outcomes) == 2
    for o in outcomes:
        assert o.spec_id, f"idea {o.idea_id} never produced a spec"
        assert o.run_id, f"idea {o.idea_id} never produced a run"
        assert o.run_status == "succeeded", (
            f"idea {o.idea_id} run_status={o.run_status} err={o.error}"
        )
        assert o.result_class == "succeeded_valid"
        assert o.verdict in {"promising", "inconclusive", "rejected"}
        assert o.error is None


@pytest.mark.asyncio
async def test_batch_fail_soft_preserves_other_ideas(fresh_db, monkeypatch):
    """A crash on one idea must not sink the rest of the batch."""
    from app.core.schemas import (
        IdeaGenerateIn,
        ProjectCreateIn,
        ProviderCredentialIn,
    )
    from app.db.session import SessionLocal
    from app.services import (
        BatchOrchestratorService,
        IdeaGenerationService,
        ProviderSecretService,
        ResearchBriefService,
        SpecService,
    )

    with SessionLocal() as db:
        project = ResearchBriefService(db).create_project(
            ProjectCreateIn(
                title="Failsoft test",
                student_name="internal",
                mentor_name="internal",
                research_direction="Verify fail-soft behaviour.",
            )
        )
        ProviderSecretService(db).add(
            ProviderCredentialIn(
                provider="mock", label="fs", api_key="mock-fs", is_default=True
            )
        )
    with SessionLocal() as db:
        ideas = await IdeaGenerationService(db).generate(
            project.id, IdeaGenerateIn(count=2)
        )
    idea_ids = [i.id for i in ideas[:2]]

    # Force the first idea's spec generation to raise.
    original = SpecService.generate
    call_count = {"n": 0}

    async def flaky_generate(self, project_id, payload):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("synthetic spec generation failure")
        return await original(self, project_id, payload)

    monkeypatch.setattr(SpecService, "generate", flaky_generate)

    with SessionLocal() as db:
        outcomes = await BatchOrchestratorService(db).run_batch(
            project_id=project.id, idea_ids=idea_ids, worker="claude_code",
            concurrency=1,
        )
    errors = [o for o in outcomes if o.error]
    ok = [o for o in outcomes if o.ok]
    assert len(errors) == 1, f"expected exactly one failed idea, got {errors}"
    assert len(ok) == 1, "the surviving idea must still complete"


@pytest.mark.asyncio
async def test_batch_honors_smoke_mode_cap(fresh_db, monkeypatch):
    """When smoke_mode is on, batch orchestrator clamps to max_ideas_per_run."""
    from app.config import reset_settings_cache
    from app.core.schemas import (
        IdeaGenerateIn,
        ProjectCreateIn,
        ProviderCredentialIn,
    )
    from app.db.session import SessionLocal
    from app.services import (
        BatchOrchestratorService,
        IdeaGenerationService,
        ProviderSecretService,
        ResearchBriefService,
    )
    from app.storage.secret_store import reset_secret_store_cache

    monkeypatch.setenv("RESEARCHOS_SMOKE_MODE", "true")
    monkeypatch.setenv("RESEARCHOS_MAX_IDEAS_PER_RUN", "2")
    reset_settings_cache()
    reset_secret_store_cache()

    from app.db.base import Base
    from app.db.session import engine

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)

    with SessionLocal() as db:
        project = ResearchBriefService(db).create_project(
            ProjectCreateIn(
                title="Smoke cap",
                student_name="internal",
                mentor_name="internal",
                research_direction="Test smoke-mode cap.",
            )
        )
        ProviderSecretService(db).add(
            ProviderCredentialIn(
                provider="mock", label="cap", api_key="mock-cap", is_default=True
            )
        )
    with SessionLocal() as db:
        # Ask for more than the cap allows.
        ideas = await IdeaGenerationService(db).generate(
            project.id, IdeaGenerateIn(count=5)
        )
    assert len(ideas) <= 2, f"idea generation must respect cap, got {len(ideas)}"

    with SessionLocal() as db:
        outcomes = await BatchOrchestratorService(db).run_batch(
            project_id=project.id,
            idea_ids=[i.id for i in ideas],
            worker="claude_code",
        )
    assert len(outcomes) <= 2
