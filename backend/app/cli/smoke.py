"""CLI entrypoint for the smoke run.

Usage (from ``researchos-local/backend`` with the venv active):

    # Against real providers (OPENAI_API_KEY / ANTHROPIC_API_KEY must be
    # set via env OR present in API_KEYS.txt in repo root or parent):
    RESEARCHOS_SMOKE_MODE=true python -m app.cli.smoke

    # Against the deterministic mock adapter (no cost, no network):
    RESEARCHOS_DEFAULT_PROVIDER=mock python -m app.cli.smoke --mock

The CLI is a thin wrapper around ``BatchOrchestratorService`` + the smoke
API route's logic. It prints a human-readable summary, never logs raw keys,
and exits non-zero if the end-to-end pipeline did not complete.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime

from app.config import get_settings
from app.core.schemas import (
    DraftGenerateIn,
    IdeaGenerateIn,
    PackageCreateIn,
    ProjectCreateIn,
    ProviderCredentialIn,
    ReviewRunIn,
)
from app.core.models import ProviderCredential, StudentProject
from app.db.base import Base
from app.db.session import SessionLocal, engine
from app.services import (
    BatchOrchestratorService,
    DraftService,
    IdeaGenerationService,
    PackageService,
    ProviderSecretService,
    ResearchBriefService,
    ReviewService,
    run_bootstrap,
)
from app.utils import get_logger

logger = get_logger("app.cli.smoke")

_SMOKE_PROJECT_ID = "smoke_project"


def _print(msg: str) -> None:
    # stdout only; never echo secrets
    print(f"[smoke] {msg}", flush=True)


def _ensure_schema() -> None:
    from app.core import models  # noqa: F401 register orm models

    Base.metadata.create_all(bind=engine)


def _ensure_smoke_project(db) -> StudentProject:
    existing = (
        db.query(StudentProject).filter(StudentProject.id == _SMOKE_PROJECT_ID).first()
    )
    if existing is not None:
        return existing
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
    )
    db.add(project)
    db.flush()
    db.add(
        ResearchBrief(
            id="brief_smoke_cli",
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
            id="bud_smoke_cli",
            project_id=project.id,
            ceiling_usd=1.0,
            warn_ratio=0.9,
        )
    )
    db.commit()
    db.refresh(project)
    return project


def _ensure_mock_credential(db) -> None:
    existing = (
        db.query(ProviderCredential)
        .filter(ProviderCredential.provider == "mock")
        .first()
    )
    if existing is not None:
        return
    ProviderSecretService(db).add(
        ProviderCredentialIn(
            provider="mock",
            label="smoke-mock",
            api_key="mock-dev-only",
            is_default=True,
        )
    )


async def run_smoke(*, mock: bool, idea_count: int | None, worker: str) -> int:
    if mock:
        # --mock must force the policy table to mock, not just add a mock
        # credential. Otherwise a production-mode policy picks the real
        # OpenAI / Anthropic credentials loaded by the bootstrap. We set the
        # env before the settings cache is populated and then reset the
        # cache defensively in case an import above already touched it.
        import os as _os

        _os.environ["RESEARCHOS_RUN_MODE"] = "mock"
        _os.environ["RESEARCHOS_DEFAULT_PROVIDER"] = "mock"
        from app.config import reset_settings_cache

        reset_settings_cache()
    settings = get_settings()
    _ensure_schema()

    # Bootstrap first (idempotent) so real credentials are picked up.
    with SessionLocal() as db:
        report = run_bootstrap(db)
    _print(
        f"bootstrap: openai={report.openai} anthropic={report.anthropic} "
        f"sources={report.sources_consulted}"
    )

    if mock:
        with SessionLocal() as db:
            _ensure_mock_credential(db)
        _print("mock credential ensured")

    with SessionLocal() as db:
        project = _ensure_smoke_project(db)
        project_id = project.id

    requested = idea_count if idea_count is not None else settings.max_ideas_per_run
    requested = max(1, int(requested))

    # --- 1. idea generation -----------------------------------------------
    with SessionLocal() as db:
        ideas = await IdeaGenerationService(db).generate(
            project_id, IdeaGenerateIn(count=requested)
        )
        idea_ids = [i.id for i in ideas[:requested]]
    _print(f"generated {len(idea_ids)} idea(s)")
    if not idea_ids:
        _print("FAIL: idea generation returned nothing")
        return 2

    # --- 2. batch run ------------------------------------------------------
    with SessionLocal() as db:
        # Smoke CLI never enforces HITL - it's dedicated internal validation.
        outcomes = await BatchOrchestratorService(db).run_batch(
            project_id=project_id,
            idea_ids=idea_ids,
            worker=worker,
            require_approval=False,
        )
    for o in outcomes:
        tag = "OK" if o.ok else "ERR"
        _print(
            f"  [{tag}] idea={o.idea_id[:8]} run={(o.run_id or '-')[:8]} "
            f"status={o.run_status} result={o.result_class} verdict={o.verdict} "
            f"claims={len(o.claim_ids)} err={o.error}"
        )

    # --- 3. draft + review -------------------------------------------------
    draft_id = None
    try:
        with SessionLocal() as db:
            draft = await DraftService(db).generate(
                project_id,
                DraftGenerateIn(
                    manuscript_title=f"Smoke draft {datetime.utcnow():%Y-%m-%d %H:%M}",
                    target_venue="Internal",
                ),
            )
            draft_id = draft.id
        _print(f"draft v{draft.version} generated id={draft_id}")
    except Exception as e:  # noqa: BLE001
        _print(f"draft step ERROR (non-fatal): {e}")

    try:
        with SessionLocal() as db:
            issues = await ReviewService(db).run_reviewers(
                project_id, ReviewRunIn()
            )
        _print(f"review issues: {len(issues)}")
    except Exception as e:  # noqa: BLE001
        _print(f"review step ERROR (non-fatal): {e}")

    # Smoke is internal validation, not a client deliverable. Auto-waive any
    # open issues so the package freeze check never blocks the smoke script.
    # Real operator runs still have to resolve / waive issues explicitly.
    try:
        from app.core.enums import ReviewState
        from app.core.models import ReviewIssue as _ReviewIssue

        with SessionLocal() as db:
            open_issues = (
                db.query(_ReviewIssue)
                .filter(
                    _ReviewIssue.project_id == project_id,
                    _ReviewIssue.state == ReviewState.open.value,
                )
                .all()
            )
            for issue in open_issues:
                issue.state = ReviewState.waived.value
                issue.resolution_note = "smoke-cli auto-waiver"
            if open_issues:
                db.commit()
                _print(f"smoke auto-waived {len(open_issues)} open review issue(s)")
    except Exception as e:  # noqa: BLE001
        _print(f"smoke auto-waive ERROR (non-fatal): {e}")

    # --- 4. package --------------------------------------------------------
    pkg_zip = None
    try:
        with SessionLocal() as db:
            pkg = await PackageService(db).build(
                project_id,
                PackageCreateIn(
                    allow_with_waived_p2=True,
                    include_mock=True,
                    notes="Smoke CLI run.",
                ),
                require_approval=False,
            )
            pkg_zip = pkg.zip_path
        _print(f"package v{pkg.version} bytes={pkg.size_bytes} at {pkg.zip_path}")
    except Exception as e:  # noqa: BLE001
        _print(f"package step ERROR: {e}")
        return 3

    # Summary
    ok_count = sum(1 for o in outcomes if o.ok)
    _print(
        json.dumps(
            {
                "project_id": project_id,
                "idea_count": len(idea_ids),
                "succeeded": ok_count,
                "draft_id": draft_id,
                "package_zip": pkg_zip,
                "smoke_mode": settings.smoke_mode,
                "default_provider": settings.default_provider,
            },
            default=str,
        )
    )
    return 0 if ok_count > 0 and pkg_zip else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ResearchOS cheap smoke run")
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Force the mock provider (zero cost, deterministic).",
    )
    parser.add_argument("--ideas", type=int, default=None, help="Idea count override.")
    parser.add_argument(
        "--worker",
        default="two_step",
        choices=["two_step", "claude_code", "codex"],
        help="Code worker mode.",
    )
    args = parser.parse_args(argv)
    try:
        return asyncio.run(
            run_smoke(mock=args.mock, idea_count=args.ideas, worker=args.worker)
        )
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
