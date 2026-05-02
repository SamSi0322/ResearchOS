"""Closure-pass tests.

Covers the four goals of this engineering pass:

  A. HITL no-bypass on single runs + on the new /runs/batch endpoint.
  B. Budget ledger recording after a run + ceiling blocking.
  C. Real-run project-id consistency (brief + budget rows keep the same
     project_id we asked for, with no post-hoc primary-key rewrite).

The goal is NOT to exercise every branch — the existing suites already do
that. These tests pin down the behavior contracts that the polish pass
introduced so a future refactor can't silently undo them.
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_project_with_ideas(
    *,
    hitl: bool,
    idea_count: int = 2,
    budget_usd: float = 50.0,
):
    from app.core.schemas import IdeaGenerateIn, ProjectCreateIn, ProviderCredentialIn
    from app.db.session import SessionLocal
    from app.services import (
        IdeaGenerationService,
        ProviderSecretService,
        ResearchBriefService,
    )

    with SessionLocal() as db:
        ProviderSecretService(db).add(
            ProviderCredentialIn(
                provider="mock",
                label="closure-test",
                api_key="mock-key-closure",
                is_default=True,
            )
        )
        project = ResearchBriefService(db).create_project(
            ProjectCreateIn(
                title="Closure-pass test",
                student_name="owner",
                mentor_name="reviewer",
                research_direction="Exercise HITL + budget contracts.",
                budget_usd=budget_usd,
                human_in_loop_enabled=hitl,
                primary_approver_email="approver@example.com" if hitl else None,
                approval_timeout_hours=1,
                reminder_interval_hours=1,
            )
        )
        project_id = project.id
        ideas = await IdeaGenerationService(db).generate(
            project_id, IdeaGenerateIn(count=idea_count)
        )
        idea_ids = [i.id for i in ideas[:idea_count]]
    return project_id, idea_ids


async def _spec_for(project_id: str, idea_id: str):
    from app.core.schemas import SpecGenerateIn
    from app.db.session import SessionLocal
    from app.services import SpecService

    with SessionLocal() as db:
        spec = await SpecService(db).generate(
            project_id, SpecGenerateIn(idea_id=idea_id)
        )
        return spec.id


# ---------------------------------------------------------------------------
# C. Real-run project-id consistency (smallest test, exercised first)
# ---------------------------------------------------------------------------


def test_create_project_with_stable_id_does_not_orphan_related_rows(fresh_db):
    """Regression for the old `project.id = X; commit` rewrite pattern.

    `create_project(payload, project_id=X)` must create the brief and
    budget_policy rows already pointing at X. No rewrite, no orphans.
    """
    from app.core.models import BudgetPolicy, ResearchBrief
    from app.core.schemas import ProjectCreateIn
    from app.db.session import SessionLocal
    from app.services import ResearchBriefService

    stable_id = "real_run_stable_id_for_regression"
    with SessionLocal() as db:
        project = ResearchBriefService(db).create_project(
            ProjectCreateIn(
                title="Stable-id project",
                student_name="o",
                mentor_name="r",
                research_direction="x",
                budget_usd=5.0,
            ),
            project_id=stable_id,
        )
        assert project.id == stable_id

    with SessionLocal() as db:
        brief = (
            db.query(ResearchBrief)
            .filter(ResearchBrief.project_id == stable_id)
            .one_or_none()
        )
        policy = (
            db.query(BudgetPolicy)
            .filter(BudgetPolicy.project_id == stable_id)
            .one_or_none()
        )
    assert brief is not None, "brief FK was not attached to the stable id"
    assert policy is not None, "budget policy FK was not attached to the stable id"

    # No orphan rows under any other project_id — the old pattern would
    # have left rows under a generated proj_* id.
    with SessionLocal() as db:
        orphan_briefs = (
            db.query(ResearchBrief)
            .filter(ResearchBrief.project_id != stable_id)
            .count()
        )
        orphan_policies = (
            db.query(BudgetPolicy)
            .filter(BudgetPolicy.project_id != stable_id)
            .count()
        )
    assert orphan_briefs == 0
    assert orphan_policies == 0


def test_create_project_without_stable_id_still_uses_generated_id(fresh_db):
    """Back-compat: the existing call signature must still work."""
    from app.core.schemas import ProjectCreateIn
    from app.db.session import SessionLocal
    from app.services import ResearchBriefService

    with SessionLocal() as db:
        project = ResearchBriefService(db).create_project(
            ProjectCreateIn(
                title="Auto-id project",
                student_name="o",
                mentor_name="r",
                research_direction="x",
                budget_usd=5.0,
            )
        )
    assert project.id.startswith("proj_")


# ---------------------------------------------------------------------------
# B. Budget ledger recording + ceiling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_writes_budget_ledger_entry(fresh_db, monkeypatch):
    """A successful run must create at least one BudgetLedgerEntry so the
    project budget summary reflects real spend. Mock runs contribute zero,
    so we patch the run's total_estimated_cost after code generation but
    before the ledger write to simulate a paid real-provider call.
    """
    from app.core.enums import ReviewState  # noqa: F401 -- ensure enum loaded
    from app.core.models import BudgetLedgerEntry, ExperimentRun
    from app.core.schemas import RunStartIn
    from app.db.session import SessionLocal
    from app.services import ExperimentRunnerService

    project_id, idea_ids = await _make_project_with_ideas(hitl=False, idea_count=1)
    spec_id = await _spec_for(project_id, idea_ids[0])

    # Patch total_estimated_cost to a positive number by post-processing the
    # run row immediately after the runner sets it but before the ledger
    # call fires. Since the ledger call reads `run.total_estimated_cost`
    # directly, overriding that attribute on the live ORM object is enough.
    real_start = ExperimentRunnerService.start_and_run

    async def _patched(self, project_id, payload, **kw):
        run = await real_start(self, project_id, payload, **kw)
        return run

    # We don't need to patch the service; instead, stub the code worker so
    # it reports a paid cost. The simplest lever is monkey-patching
    # CodeWorkerService.generate_code to add 'estimated_cost_usd' into
    # code_info.
    from app.services import code_worker_service as cws

    real_generate = cws.CodeWorkerService.generate_code

    async def _paid_generate(self, **kw):
        code_info = await real_generate(self, **kw)
        # Pretend this run cost 2.50 USD on the provider.
        code_info["estimated_cost_usd"] = 2.5
        return code_info

    monkeypatch.setattr(cws.CodeWorkerService, "generate_code", _paid_generate)

    with SessionLocal() as db:
        run = await ExperimentRunnerService(db).start_and_run(
            project_id,
            RunStartIn(spec_id=spec_id, worker="claude_code"),
        )
        run_id = run.id

    with SessionLocal() as db:
        entries = (
            db.query(BudgetLedgerEntry)
            .filter(BudgetLedgerEntry.project_id == project_id)
            .all()
        )
    kinds = {e.kind for e in entries}
    refs = {e.reference for e in entries}
    assert kinds == {"run"}
    assert refs == {run_id}
    assert any(
        abs((e.meta or {}).get("aggregate_estimated_cost_usd", 0.0) - 2.5) < 1e-6
        for e in entries
    ), [e.meta for e in entries]
    assert all(e.amount_usd == 0.0 for e in entries)


@pytest.mark.asyncio
async def test_provider_call_writes_budget_ledger_entry(fresh_db):
    """Every real provider call should be visible in the budget ledger."""
    from app.providers.base import CompletionRequest, CompletionResult
    from app.db.session import SessionLocal
    from app.services.budget_service import BudgetService
    from app.services.provider_call_ledger import complete_with_ledger

    class _Adapter:
        async def complete(self, req):
            return CompletionResult(
                provider="openai",
                model="gpt-5.4-pro",
                text="{}",
                usage={"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
                requested_model="gpt-5.4-pro",
                actual_model="gpt-5.4-pro",
                requested_reasoning_effort="xhigh",
                actual_reasoning_effort="high",
                estimated_cost_usd=0.0036,
                raw={"id": "resp_test"},
            )

    project_id, _ = await _make_project_with_ideas(hitl=False, idea_count=0)

    with SessionLocal() as db:
        result = await complete_with_ledger(
            db,
            project_id=project_id,
            adapter=_Adapter(),
            req=CompletionRequest(
                prompt="x",
                phase="draft_generation",
                task_kind="draft_generation",
                reasoning_effort="xhigh",
            ),
            reference="draft_generation:test",
        )
        assert result.model == "gpt-5.4-pro"
        db.commit()

    with SessionLocal() as db:
        summary = BudgetService(db).summary(project_id)
    assert summary["provider_call_entries"] == 1
    assert summary["provider_call_spent_usd"] == pytest.approx(0.0036)
    assert summary["by_kind"]["provider_call"] == pytest.approx(0.0036)


@pytest.mark.asyncio
async def test_run_blocked_when_budget_ceiling_already_spent(fresh_db):
    """If the ledger already shows spent_usd >= ceiling_usd, start_and_run
    must raise BudgetExceededError before spending more."""
    from app.core.schemas import RunStartIn
    from app.db.session import SessionLocal
    from app.services import ExperimentRunnerService
    from app.services.budget_service import BudgetService
    from app.services.experiment_runner_service import BudgetExceededError

    project_id, idea_ids = await _make_project_with_ideas(
        hitl=False, idea_count=1, budget_usd=5.0
    )
    spec_id = await _spec_for(project_id, idea_ids[0])

    # Drop a ledger entry that already matches the ceiling.
    with SessionLocal() as db:
        BudgetService(db).record(
            project_id=project_id,
            amount_usd=5.0,
            kind="manual",
            reference="seed",
        )

    with SessionLocal() as db:
        with pytest.raises(BudgetExceededError) as exc:
            await ExperimentRunnerService(db).start_and_run(
                project_id,
                RunStartIn(spec_id=spec_id, worker="claude_code"),
            )
    assert exc.value.ceiling_usd == pytest.approx(5.0)
    assert exc.value.spent_usd >= exc.value.ceiling_usd


@pytest.mark.asyncio
async def test_budget_exceeded_is_402_on_runs_start(fresh_db):
    from fastapi import HTTPException

    from app.api.routes.runs import start_run
    from app.core.schemas import RunStartIn
    from app.db.session import SessionLocal
    from app.services.budget_service import BudgetService

    project_id, idea_ids = await _make_project_with_ideas(
        hitl=False, idea_count=1, budget_usd=2.0
    )
    spec_id = await _spec_for(project_id, idea_ids[0])

    with SessionLocal() as db:
        BudgetService(db).record(
            project_id=project_id,
            amount_usd=2.0,
            kind="manual",
            reference="seed",
        )

    with SessionLocal() as db:
        with pytest.raises(HTTPException) as exc:
            await start_run(
                project_id,
                RunStartIn(spec_id=spec_id, worker="claude_code"),
                db=db,
            )
    assert exc.value.status_code == 402
    assert exc.value.detail["error"] == "budget_exceeded"
    assert exc.value.detail["ceiling_usd"] == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# A. HITL no-bypass
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_run_is_gated_under_hitl(fresh_db):
    """The /runs/start endpoint must surface approval blocking with a
    structured 409 — i.e. the batch gate is NOT bypassable by running ideas
    one at a time through the single-run route."""
    from fastapi import HTTPException

    from app.api.routes.runs import start_run
    from app.core.schemas import RunStartIn
    from app.db.session import SessionLocal

    project_id, idea_ids = await _make_project_with_ideas(hitl=True, idea_count=1)
    spec_id = await _spec_for(project_id, idea_ids[0])

    with SessionLocal() as db:
        with pytest.raises(HTTPException) as exc:
            await start_run(
                project_id,
                RunStartIn(spec_id=spec_id, worker="claude_code"),
                db=db,
            )
    assert exc.value.status_code == 409
    body = exc.value.detail
    assert body["error"] == "approval_required"
    assert body["stage_key"] == "post_shortlist"
    assert body["approval_id"]


@pytest.mark.asyncio
async def test_batch_run_route_is_gated_under_hitl(fresh_db):
    """The new /runs/batch endpoint goes through the backend orchestrator
    and must surface the same structured 409."""
    from fastapi import HTTPException

    from app.api.routes.runs import BatchRunIn, batch_run
    from app.db.session import SessionLocal

    project_id, idea_ids = await _make_project_with_ideas(hitl=True, idea_count=2)

    with SessionLocal() as db:
        with pytest.raises(HTTPException) as exc:
            await batch_run(
                project_id,
                BatchRunIn(idea_ids=idea_ids, worker="claude_code"),
                db=db,
            )
    assert exc.value.status_code == 409
    body = exc.value.detail
    assert body["error"] == "approval_required"
    assert body["stage_key"] in (
        "post_shortlist",
        "post_pilot_evidence",
    )


@pytest.mark.asyncio
async def test_batch_run_route_happy_path_returns_outcomes(fresh_db):
    """Without HITL, /runs/batch should run cleanly and return per-idea
    outcomes in the documented shape."""
    from app.api.routes.runs import BatchRunIn, batch_run
    from app.db.session import SessionLocal

    project_id, idea_ids = await _make_project_with_ideas(hitl=False, idea_count=2)
    with SessionLocal() as db:
        out = await batch_run(
            project_id,
            BatchRunIn(idea_ids=idea_ids, worker="claude_code", concurrency=1),
            db=db,
        )
    body = out.model_dump()
    assert body["total"] == len(idea_ids)
    assert len(body["outcomes"]) == len(idea_ids)
    for o in body["outcomes"]:
        assert set(o.keys()) >= {
            "idea_id",
            "spec_id",
            "run_id",
            "run_status",
            "result_class",
            "verdict",
            "claim_ids",
            "error",
        }


def test_frontend_batch_client_uses_backend_route():
    """Static check: the frontend API client must call ``/projects/{id}/runs/batch``
    instead of fanning out spec/run creation client-side.
    """
    from pathlib import Path

    api_ts = Path(__file__).resolve().parents[2] / "frontend" / "src" / "lib" / "api.ts"
    src = api_ts.read_text(encoding="utf-8")
    # The new batch entry point is named ``batchRun`` and POSTs to /runs/batch.
    assert "batchRun" in src
    assert "/runs/batch" in src
    # The old client-side fanout API must be gone — it took idea_ids and
    # iterated api.listSpecs + api.generateSpec + api.startRun in the browser.
    assert "batchSelected" not in src, (
        "frontend still exposes the legacy client-side batch fanout"
    )


# ---------------------------------------------------------------------------
# Package manifest ledger surfacing
# ---------------------------------------------------------------------------


def test_cost_summary_embeds_ledger_when_provided():
    from app.services.package_service import _cost_summary

    summary = _cost_summary([], ledger_summary={
        "ceiling_usd": 10.0,
        "spent_usd": 3.5,
        "remaining_usd": 6.5,
        "warn": False,
        "entries": 2,
    })
    assert "ledger" in summary
    assert summary["ledger"]["spent_usd"] == pytest.approx(3.5)
    assert summary["ledger"]["ceiling_usd"] == pytest.approx(10.0)


def test_cost_summary_without_ledger_is_back_compatible():
    from app.services.package_service import _cost_summary

    summary = _cost_summary([])
    assert "ledger" not in summary
    assert summary["explanation"].startswith("Estimated cost")


# ---------------------------------------------------------------------------
# A2: predictive budget, batch pre-exhaustion, gate-B-requires-valid-pilot
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_predictive_budget_blocks_when_estimate_would_overspend(fresh_db):
    """Spec.budget_estimate_usd must gate the precheck.

    Scenario: ceiling=5.0, already spent=4.0, next spec estimates 2.0.
    The retrospective rule (spent >= ceiling) would allow the run; the
    predictive rule must refuse because 4.0 + 2.0 > 5.0.
    """
    from app.core.models import ExperimentSpec
    from app.core.schemas import RunStartIn
    from app.db.session import SessionLocal
    from app.services import ExperimentRunnerService
    from app.services.budget_service import BudgetService
    from app.services.experiment_runner_service import BudgetExceededError

    project_id, idea_ids = await _make_project_with_ideas(
        hitl=False, idea_count=1, budget_usd=5.0
    )
    spec_id = await _spec_for(project_id, idea_ids[0])

    # Seed 4.0 of spend, then stamp the spec with a 2.0 estimate.
    with SessionLocal() as db:
        BudgetService(db).record(
            project_id=project_id, amount_usd=4.0, kind="manual", reference="seed"
        )
        spec = db.query(ExperimentSpec).filter(ExperimentSpec.id == spec_id).first()
        spec.budget_estimate_usd = 2.0
        db.commit()

    with SessionLocal() as db:
        with pytest.raises(BudgetExceededError) as exc:
            await ExperimentRunnerService(db).start_and_run(
                project_id,
                RunStartIn(spec_id=spec_id, worker="claude_code"),
            )
    assert exc.value.ceiling_usd == pytest.approx(5.0)
    assert exc.value.spent_usd == pytest.approx(4.0)


@pytest.mark.asyncio
async def test_predictive_budget_allows_when_estimate_fits(fresh_db):
    """When spent + estimate <= ceiling, the precheck must pass."""
    from app.core.models import ExperimentSpec
    from app.core.schemas import RunStartIn
    from app.db.session import SessionLocal
    from app.services import ExperimentRunnerService
    from app.services.budget_service import BudgetService

    project_id, idea_ids = await _make_project_with_ideas(
        hitl=False, idea_count=1, budget_usd=10.0
    )
    spec_id = await _spec_for(project_id, idea_ids[0])

    with SessionLocal() as db:
        BudgetService(db).record(
            project_id=project_id, amount_usd=2.0, kind="manual", reference="seed"
        )
        spec = db.query(ExperimentSpec).filter(ExperimentSpec.id == spec_id).first()
        spec.budget_estimate_usd = 3.0  # 2 + 3 = 5 <= 10
        db.commit()

    with SessionLocal() as db:
        run = await ExperimentRunnerService(db).start_and_run(
            project_id,
            RunStartIn(spec_id=spec_id, worker="claude_code"),
        )
    assert run.id


@pytest.mark.asyncio
async def test_batch_pre_exhausted_returns_402(fresh_db):
    """If the project is already at/over ceiling at batch start, /runs/batch
    must return a structured 402 — not a 200 with per-idea error strings."""
    from fastapi import HTTPException

    from app.api.routes.runs import BatchRunIn, batch_run
    from app.db.session import SessionLocal
    from app.services.budget_service import BudgetService

    project_id, idea_ids = await _make_project_with_ideas(
        hitl=False, idea_count=2, budget_usd=3.0
    )
    with SessionLocal() as db:
        BudgetService(db).record(
            project_id=project_id, amount_usd=3.0, kind="manual", reference="seed"
        )

    with SessionLocal() as db:
        with pytest.raises(HTTPException) as exc:
            await batch_run(
                project_id,
                BatchRunIn(idea_ids=idea_ids, worker="claude_code"),
                db=db,
            )
    assert exc.value.status_code == 402
    body = exc.value.detail
    assert body["error"] == "budget_exceeded"
    assert body["ceiling_usd"] == pytest.approx(3.0)


@pytest.mark.asyncio
async def test_gate_selection_ignores_succeeded_invalid_runs(fresh_db):
    """A prior ``status=succeeded, result_class=succeeded_invalid`` run is NOT
    pilot evidence. Gate selection must stay at ``post_shortlist`` until at
    least one ``succeeded_valid`` run exists."""
    from app.core.enums import RunResultClass, RunStatus
    from app.core.models import ExperimentRun
    from app.db.session import SessionLocal
    from app.services.batch_orchestrator_service import BatchOrchestratorService
    from app.services.experiment_runner_service import pick_run_gate
    from app.utils import new_id

    project_id, idea_ids = await _make_project_with_ideas(hitl=True, idea_count=1)

    # Insert a bare succeeded-but-invalid run directly. We don't need the
    # full run pipeline for this — the gate picker only reads status + result_class.
    with SessionLocal() as db:
        db.add(
            ExperimentRun(
                id=new_id("run"),
                project_id=project_id,
                spec_id="spec_placeholder",
                idea_id=idea_ids[0],
                workspace_path="",
                status=RunStatus.succeeded.value,
                result_class=RunResultClass.succeeded_invalid.value,
                seed=0,
                provider_routing={},
                config={},
            )
        )
        db.commit()

    with SessionLocal() as db:
        single_gate = pick_run_gate(db, project_id)
        batch_gate = BatchOrchestratorService(db)._pick_batch_gate(project_id)
    assert single_gate == "post_shortlist"
    assert batch_gate == "post_shortlist"

    # Now add a valid pilot run. Both paths should advance to Gate B.
    with SessionLocal() as db:
        db.add(
            ExperimentRun(
                id=new_id("run"),
                project_id=project_id,
                spec_id="spec_placeholder",
                idea_id=idea_ids[0],
                workspace_path="",
                status=RunStatus.succeeded.value,
                result_class=RunResultClass.succeeded_valid.value,
                seed=0,
                provider_routing={},
                config={},
            )
        )
        db.commit()

    with SessionLocal() as db:
        single_gate = pick_run_gate(db, project_id)
        batch_gate = BatchOrchestratorService(db)._pick_batch_gate(project_id)
    assert single_gate == "post_pilot_evidence"
    assert batch_gate == "post_pilot_evidence"
