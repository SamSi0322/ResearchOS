"""Smoke mode API routes.

Provides two endpoints:

* GET  /api/smoke/health   - reports which providers are configured and which
                             bootstrap path (env / file / existing) filled them,
                             without ever echoing a raw key.
* POST /api/smoke/run      - runs the full pipeline on a tiny cheap budget:
                             1. ensure a "smoke" project exists
                             2. generate N ideas (clamped by settings)
                             3. batch-run K ideas concurrently
                             4. generate a draft + run reviewers
                             5. build a package

The purpose is to validate that the **real** provider path works without
burning through money. Concrete limits live in settings
(``smoke_max_tokens``, ``max_ideas_per_run``, ``concurrency_per_batch``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.config import Phase, active_run_mode, get_settings, resolve_model_policy
from app.core.enums import TaskKind
from app.core.models import ProviderCredential, StudentProject
from app.core.schemas import (
    DraftGenerateIn,
    IdeaGenerateIn,
    PackageCreateIn,
    ProjectCreateIn,
    ProviderValidationResult,
    ReviewRunIn,
    ValidationCategory,
)
from app.db import get_session
from app.providers.router import get_provider_router
from app.services import (
    BatchOrchestratorService,
    DraftService,
    FunnelService,
    IdeaGenerationService,
    PackageService,
    ResearchBriefService,
    ReviewService,
    run_bootstrap,
)
from app.services.provider_validation import (
    build_result,
    classify_exception,
    persist_validation,
)
from app.utils import get_logger

logger = get_logger(__name__)

router = APIRouter()

_SMOKE_PROJECT_ID = "smoke_project"


class SmokeHealthOut(BaseModel):
    smoke_mode: bool
    run_mode: str = "production"
    # Every runtime call in ResearchOS flows through the provider adapters.
    # Pinning this literal here makes it obvious in the /smoke/health payload
    # that the runtime does not depend on interactive Claude Code / Codex
    # sessions, even when the backend is serving a real wire-level call.
    execution_mode: str = "headless_api"
    openai_ready: bool
    anthropic_ready: bool
    credentials: list[dict[str, Any]] = Field(default_factory=list)
    settings_snapshot: dict[str, Any] = Field(default_factory=dict)
    bootstrap: dict[str, Any] = Field(default_factory=dict)


class SmokeRunIn(BaseModel):
    idea_count: int | None = None
    worker: str = "two_step"
    concurrency: int | None = None
    include_package: bool = True
    provider_profile: str = "default"


class SmokeRunOut(BaseModel):
    project_id: str
    smoke_mode: bool
    idea_ids: list[str]
    batch: list[dict[str, Any]]
    draft_id: str | None = None
    review_issue_count: int = 0
    package_id: str | None = None
    package_zip_path: str | None = None
    messages: list[str] = Field(default_factory=list)


# --- GET /health ----------------------------------------------------------


@router.get("/health", response_model=SmokeHealthOut)
def smoke_health(db: Session = Depends(get_session)) -> SmokeHealthOut:
    """Report config + which providers are currently bootable.

    Never returns raw API keys. Returns masked previews + which sources the
    credential came from.
    """
    settings = get_settings()
    report = run_bootstrap(db)

    creds: list[dict[str, Any]] = []
    openai_ready = False
    anthropic_ready = False
    for c in db.query(ProviderCredential).all():
        creds.append(
            {
                "id": c.id,
                "provider": c.provider,
                "label": c.label,
                "masked_preview": c.masked_preview,
                "default_model": c.default_model,
                "is_default": c.is_default,
                "default_for": c.default_for,
            }
        )
        if c.provider == "openai":
            openai_ready = True
        elif c.provider == "anthropic":
            anthropic_ready = True

    return SmokeHealthOut(
        smoke_mode=settings.smoke_mode,
        run_mode=active_run_mode().value,
        openai_ready=openai_ready,
        anthropic_ready=anthropic_ready,
        credentials=creds,
        settings_snapshot={
            "max_ideas_per_run": settings.max_ideas_per_run,
            "concurrency_per_batch": settings.concurrency_per_batch,
            "smoke_max_tokens": settings.smoke_max_tokens,
            "smoke_prompt_budget_chars": settings.smoke_prompt_budget_chars,
            "smoke_request_timeout": settings.smoke_request_timeout,
            "openai_smoke_model": settings.openai_smoke_model,
            "anthropic_smoke_model": settings.anthropic_smoke_model,
            "openai_credential_test_model": settings.openai_credential_test_model,
            "anthropic_credential_test_model": settings.anthropic_credential_test_model,
            "default_provider": settings.default_provider,
        },
        bootstrap={
            "openai": report.openai,
            "anthropic": report.anthropic,
            "sources_consulted": report.sources_consulted,
            "file_path": report.file_path,
            "file_keys_found": report.file_keys_found,
        },
    )


# --- POST /run ------------------------------------------------------------


async def _ensure_smoke_project(db: Session) -> StudentProject:
    existing = (
        db.query(StudentProject).filter(StudentProject.id == _SMOKE_PROJECT_ID).first()
    )
    if existing is not None:
        return existing

    # Create the project row directly so we can use the stable smoke id.
    # ResearchBriefService would generate a random id, and renaming it
    # post-commit would strand the FK rows (brief, budget policy, audit).
    from app.core.enums import ProjectStatus
    from app.core.models import BudgetPolicy, ResearchBrief

    project = StudentProject(
        id=_SMOKE_PROJECT_ID,
        title="Smoke mode: real-provider validation",
        status=ProjectStatus.active.value,
        student_name="internal_smoke",
        mentor_name="internal_smoke",
        research_direction=(
            "Validate the full ResearchOS chain end-to-end with real provider "
            "calls and cheap models."
        ),
        target_venues=["Internal only"],
        constraints="<=2 min wall-clock, stdlib-only experiment, smoke budget",
        exploration_strategy="focused",
        provider_profile="default",
        notes="Auto-created by /api/smoke/run. Safe to delete.",
    )
    db.add(project)
    db.flush()
    db.add(
        ResearchBrief(
            id=f"brief_smoke",
            project_id=project.id,
            research_direction=project.research_direction,
            constraints=project.constraints,
            target_venues=project.target_venues,
            budget_usd=1.0,
            strategy=project.exploration_strategy,
        )
    )
    db.add(
        BudgetPolicy(
            id=f"bud_smoke",
            project_id=project.id,
            ceiling_usd=1.0,
            warn_ratio=0.9,
        )
    )
    db.commit()
    db.refresh(project)
    return project


@router.post("/run", response_model=SmokeRunOut)
async def smoke_run(
    payload: SmokeRunIn, db: Session = Depends(get_session)
) -> SmokeRunOut:
    settings = get_settings()
    messages: list[str] = []

    # Auto-seed credentials on demand so the endpoint works even if the caller
    # hit it before the server started up fully.
    run_bootstrap(db)

    project = await _ensure_smoke_project(db)

    # --- 1. idea generation ------------------------------------------------
    requested = payload.idea_count or settings.max_ideas_per_run
    if settings.smoke_mode:
        requested = min(requested, settings.max_ideas_per_run)
    ideas = await IdeaGenerationService(db).generate(
        project.id, IdeaGenerateIn(count=requested)
    )
    # Only keep the top N by whatever score the adapter produced (mock returns
    # deterministic order, real adapters have no score yet).
    idea_ids = [i.id for i in ideas[:requested]]
    messages.append(f"generated {len(idea_ids)} idea(s)")

    if not idea_ids:
        raise HTTPException(500, "idea generation returned nothing")

    # --- 2. batch run across those ideas ----------------------------------
    # Smoke runs never gate on HITL: the smoke project is internal validation.
    outcomes = await BatchOrchestratorService(db).run_batch(
        project_id=project.id,
        idea_ids=idea_ids,
        worker=payload.worker,
        seed_base=0,
        concurrency=payload.concurrency,
        require_approval=False,
    )
    batch_json = [
        {
            "idea_id": o.idea_id,
            "spec_id": o.spec_id,
            "run_id": o.run_id,
            "run_status": o.run_status,
            "result_class": o.result_class,
            "verdict": o.verdict,
            "claim_ids": o.claim_ids,
            "error": o.error,
        }
        for o in outcomes
    ]
    messages.append(
        f"batch: {sum(1 for o in outcomes if o.ok)}/{len(outcomes)} ideas succeeded"
    )

    # --- 3. draft + review -------------------------------------------------
    draft = None
    try:
        draft = await DraftService(db).generate(
            project.id,
            DraftGenerateIn(
                manuscript_title=f"Smoke draft {datetime.utcnow():%Y-%m-%d %H:%M}",
                target_venue="Internal",
            ),
        )
        messages.append(f"draft v{draft.version} generated")
    except Exception as e:  # noqa: BLE001
        messages.append(f"draft generation skipped: {e}")

    review_count = 0
    try:
        issues = await ReviewService(db).run_reviewers(project.id, ReviewRunIn())
        review_count = len(issues)
        messages.append(f"review issues: {review_count}")
    except Exception as e:  # noqa: BLE001
        messages.append(f"review step skipped: {e}")

    # --- 4. package --------------------------------------------------------
    pkg_id = None
    pkg_zip = None
    if payload.include_package:
        try:
            pkg = await PackageService(db).build(
                project.id,
                PackageCreateIn(
                    allow_with_waived_p2=True,
                    include_mock=True,
                    notes="Smoke run package.",
                ),
                require_approval=False,
            )
            pkg_id = pkg.id
            pkg_zip = pkg.zip_path
            messages.append(f"package v{pkg.version} at {pkg.zip_path}")
        except Exception as e:  # noqa: BLE001
            messages.append(f"package step skipped: {e}")

    return SmokeRunOut(
        project_id=project.id,
        smoke_mode=settings.smoke_mode,
        idea_ids=idea_ids,
        batch=batch_json,
        draft_id=draft.id if draft else None,
        review_issue_count=review_count,
        package_id=pkg_id,
        package_zip_path=pkg_zip,
        messages=messages,
    )


# --- POST /ping -----------------------------------------------------------


class SmokePingIn(BaseModel):
    provider: str  # "openai" | "anthropic"
    prompt: str = "Respond with exactly the token OK."


# Which pipeline phase's policy each provider exercises when pinged. We pick
# the phase that the production table routes to that provider so the ping
# touches the exact adapter+model combination a real run would use.
_PHASE_BY_PROVIDER: dict[str, Phase] = {
    "openai": Phase.code_generation,
    "anthropic": Phase.idea_generation,
}


@router.post("/ping", response_model=ProviderValidationResult)
async def smoke_ping(
    payload: SmokePingIn, db: Session = Depends(get_session)
) -> ProviderValidationResult:
    """Exercise the resolved runtime policy path for a given provider.

    Returns the canonical ``ProviderValidationResult`` shape. The call goes
    through ``resolve_model_policy`` + ``router.resolve_with_policy`` so the
    exact model id, alias, and reasoning effort a real pipeline phase would
    use is exercised — not a generic test model. For plain credential
    validation see ``/api/providers/test``.
    """
    import time

    from app.providers.base import CompletionRequest, apply_policy, apply_smoke_limits

    run_bootstrap(db)
    settings = get_settings()

    phase = _PHASE_BY_PROVIDER.get(payload.provider)
    if phase is None:
        res = build_result(
            ok=False,
            category=ValidationCategory.config_error,
            provider=payload.provider,
            requested_model=None,
            actual_model=None,
            http_status=None,
            provider_error_code=None,
            upstream_message=f"unsupported provider: {payload.provider}",
            response_preview=None,
            latency_ms=0,
        )
        persist_validation(db, result=res, credential_id=None, source="smoke_ping")
        return res

    # Resolve using the SAME path a real run would use. In smoke mode the
    # policy table returns the cheap smoke models; in production mode the
    # production ones; mock mode short-circuits and never touches the wire.
    policy = resolve_model_policy(phase)
    router_ = get_provider_router(db)
    resolved = router_.resolve_with_policy(policy)

    if resolved.provider != payload.provider:
        res = build_result(
            ok=False,
            category=ValidationCategory.config_error,
            provider=payload.provider,
            requested_model=policy.model,
            actual_model=None,
            http_status=None,
            provider_error_code=None,
            upstream_message=(
                f"no {payload.provider} credential configured for phase "
                f"{phase.value}; router resolved to {resolved.provider}"
            ),
            response_preview=None,
            latency_ms=0,
        )
        persist_validation(db, result=res, credential_id=None, source="smoke_ping")
        return res

    req = CompletionRequest(
        prompt=payload.prompt,
        task_kind="ping",
        temperature=0.0,
        max_tokens=8,
    )
    req = apply_policy(req, policy)
    req = apply_smoke_limits(req, settings)
    started = time.time()
    try:
        result = await resolved.adapter.complete(req)
        latency_ms = int((time.time() - started) * 1000)
        res = build_result(
            ok=True,
            category=ValidationCategory.ok,
            provider=resolved.provider,
            requested_model=policy.model,
            actual_model=getattr(result, "actual_model", None) or resolved.model,
            http_status=200,
            provider_error_code=None,
            upstream_message=None,
            response_preview=(result.text or "")[:80] or None,
            latency_ms=latency_ms or result.latency_ms,
        )
    except Exception as exc:  # noqa: BLE001
        latency_ms = int((time.time() - started) * 1000)
        classified = classify_exception(exc)
        res = build_result(
            ok=False,
            category=classified.category,
            provider=resolved.provider,
            requested_model=policy.model,
            actual_model=resolved.model,
            http_status=classified.http_status,
            provider_error_code=classified.provider_error_code,
            upstream_message=classified.message,
            response_preview=None,
            latency_ms=latency_ms,
        )
    persist_validation(
        db, result=res, credential_id=resolved.credential_id, source="smoke_ping"
    )
    return res
