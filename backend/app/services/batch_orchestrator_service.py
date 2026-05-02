"""Bounded multi-idea concurrency for the pipeline.

This is intentionally small-scale. Given a project id and a list of idea ids,
we run each idea through {spec -> code worker -> experiment -> analysis}
concurrently, with a semaphore that caps parallelism at
``settings.concurrency_per_batch`` (default 2).

Design notes:

* **Fail-soft**: a failure on one idea does NOT cancel the others. We collect
  per-idea exceptions into the returned ``BatchIdeaOutcome`` list and keep
  going.
* **Per-idea artifacts**: each ExperimentRun already writes its own workspace
  at ``var/workspaces/<project>/<run_id>/`` so there is no shared state to
  worry about.
* **Own sessions**: each idea gets its own SQLAlchemy ``Session`` so there is
  no shared-session contention - the services already commit per-step.
* **Respects smoke mode**: when smoke_mode is on we cap the set of ideas to
  ``settings.max_ideas_per_run``.

The service is intentionally small; it is not a distributed scheduler. It is
safe to extend later (timeouts per idea, retries, etc.) without touching the
rest of the pipeline.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.enums import AuditKind, GateKey
from app.core.models import ExperimentRun, StudentProject
from app.core.schemas import DraftGenerateIn, RunStartIn, SpecGenerateIn
from app.db.session import SessionLocal
from app.services.approval_service import ApprovalService, GateDecision
from app.services.audit_service import AuditService
from app.services.budget_service import BudgetService
from app.services.experiment_runner_service import (
    BudgetExceededError,
    ExperimentRunnerService,
)
from app.services.result_analysis_service import ResultAnalysisService
from app.services.spec_service import SpecService
from app.utils import get_logger

logger = get_logger(__name__)


@dataclass
class BatchIdeaOutcome:
    idea_id: str
    spec_id: str | None = None
    run_id: str | None = None
    run_status: str | None = None
    result_class: str | None = None
    verdict: str | None = None
    claim_ids: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.run_status in (
            "succeeded",
            "running",
            "queued",
        )


class BatchBlockedError(Exception):
    """Raised when HITL is enabled and the batch cannot proceed yet.

    Carries enough information for the HTTP layer to translate it into a 409
    response and for the operator UI to point at the pending approval id.
    """

    def __init__(self, *, stage_key: str, reason: str, approval_id: str | None, status: str):
        super().__init__(f"batch blocked at {stage_key}: {reason}")
        self.stage_key = stage_key
        self.reason = reason
        self.approval_id = approval_id
        self.status = status  # "paused" | "blocked"


class BatchOrchestratorService:
    def __init__(self, db: Session) -> None:
        # The service stores this session for top-level audit logging only;
        # the concurrent per-idea work uses its own sessions.
        self.db = db
        self.settings = get_settings()
        self.audit = AuditService(db)

    def _pick_batch_gate(self, project_id: str) -> str:
        """Pick Gate A vs Gate B based on whether pilots already ran.

        * If no prior succeeded runs exist → Gate A (``post_shortlist``).
        * If one or more succeeded runs exist → Gate B (``post_pilot_evidence``).
        """
        # Gate B requires a prior run that actually produced pilot evidence
        # (``result_class == succeeded_valid``). A ``succeeded_invalid`` run
        # exited 0 without metrics.json, so there is nothing to evaluate
        # and the batch stays at Gate A. Keep this aligned with
        # ``pick_run_gate`` in the single-run path.
        from app.core.enums import RunResultClass  # local import: avoid cycle
        has_valid_pilot = (
            self.db.query(ExperimentRun)
            .filter(
                ExperimentRun.project_id == project_id,
                ExperimentRun.status == "succeeded",
                ExperimentRun.result_class == RunResultClass.succeeded_valid.value,
            )
            .first()
        ) is not None
        return GateKey.post_pilot_evidence.value if has_valid_pilot else GateKey.post_shortlist.value

    async def run_batch(
        self,
        *,
        project_id: str,
        idea_ids: list[str],
        worker: str = "two_step",
        seed_base: int = 0,
        concurrency: int | None = None,
        require_approval: bool = True,
    ) -> list[BatchIdeaOutcome]:
        # Smoke mode hard-caps the batch so we never accidentally burn through
        # ideas in a tiny validation run.
        ideas = list(idea_ids)
        if self.settings.smoke_mode:
            ideas = ideas[: max(1, int(self.settings.max_ideas_per_run))]

        # Pre-batch budget gate. If the project is already at/over its
        # ceiling before the batch begins, this is a batch-level business
        # rule failure (→ HTTP 402), not a per-idea error. Budgets reached
        # *mid-batch* still use the cooperative ``budget_halt`` flag below
        # so in-flight ideas are not aborted, only unstarted ones.
        try:
            summary = BudgetService(self.db).summary(project_id)
            ceiling = float(summary.get("ceiling_usd") or 0.0)
            spent = float(summary.get("spent_usd") or 0.0)
            if ceiling > 0 and spent >= ceiling:
                raise BudgetExceededError(
                    project_id=project_id,
                    spent_usd=spent,
                    ceiling_usd=ceiling,
                )
        except BudgetExceededError:
            raise

        if require_approval:
            project = (
                self.db.query(StudentProject)
                .filter(StudentProject.id == project_id)
                .first()
            )
            if project is not None and project.human_in_loop_enabled:
                # Gate A: post-shortlist. Fires before the first batch run.
                # Gate B: post-pilot-evidence. Fires when the project already
                #         has >=1 succeeded run (this is the "deeper runs"
                #         moment Gate B is designed for).
                gate_key = self._pick_batch_gate(project_id)
                gate = await ApprovalService(self.db).ensure_gate(
                    project_id=project_id,
                    stage_key=gate_key,
                    context_snapshot={
                        "reason": "batch_start",
                        "idea_count": len(ideas),
                        "worker": worker,
                        "gate_key": gate_key,
                    },
                )
                if gate.decision is GateDecision.paused:
                    raise BatchBlockedError(
                        stage_key=gate_key,
                        reason=gate.reason or "awaiting_approval",
                        approval_id=gate.approval.id if gate.approval else None,
                        status="paused",
                    )
                if gate.decision is GateDecision.blocked:
                    raise BatchBlockedError(
                        stage_key=gate_key,
                        reason=gate.reason or "blocked",
                        approval_id=gate.approval.id if gate.approval else None,
                        status="blocked",
                    )

        sem = asyncio.Semaphore(max(1, int(concurrency or self.settings.concurrency_per_batch)))

        logger.info(
            "batch orchestrator starting",
            extra={
                "project_id": project_id,
                "ideas": len(ideas),
                "worker": worker,
                "smoke_mode": self.settings.smoke_mode,
                "concurrency": sem._value,  # type: ignore[attr-defined]
            },
        )

        # Record one audit entry up front so operators can see the batch as a
        # single logical event in the timeline.
        self.audit.log(
            project_id=project_id,
            kind=AuditKind.run_started,
            message=f"Batch started: {len(ideas)} idea(s), worker={worker}",
            subject_kind="project",
            subject_id=project_id,
            payload={
                "idea_ids": ideas,
                "worker": worker,
                "smoke_mode": self.settings.smoke_mode,
            },
        )
        self.db.commit()

        # Budget ceiling: once the ledger's spent >= ceiling we stop scheduling
        # new provider-backed work. A simple shared flag is sufficient here —
        # the batch is bounded-concurrency and short-lived, so we do not need
        # a distributed primitive.
        budget_halt = {"hit": False}

        async def _one(idea_id: str, seed: int) -> BatchIdeaOutcome:
            outcome = BatchIdeaOutcome(idea_id=idea_id)
            async with sem:
                # Re-check the project budget ceiling for every idea. If an
                # earlier idea's post-run ledger write crossed the ceiling,
                # we emit a ``budget_exceeded`` outcome instead of paying for
                # another spec + code-gen pass.
                if budget_halt["hit"]:
                    outcome.error = "budget_exceeded"
                    return outcome
                try:
                    with SessionLocal() as db:
                        summary = BudgetService(db).summary(project_id)
                    ceiling = float(summary.get("ceiling_usd") or 0.0)
                    spent = float(summary.get("spent_usd") or 0.0)
                    if ceiling > 0 and spent >= ceiling:
                        budget_halt["hit"] = True
                        outcome.error = (
                            f"budget_exceeded: ${spent:.4f} >= ${ceiling:.4f}"
                        )
                        return outcome
                except Exception:  # noqa: BLE001
                    # If the summary call fails, keep going — the per-run
                    # precheck inside start_and_run will still enforce the cap.
                    pass
                try:
                    # Each idea gets a fresh session so writes don't fight.
                    with SessionLocal() as db:
                        spec = await SpecService(db).generate(
                            project_id, SpecGenerateIn(idea_id=idea_id)
                        )
                        outcome.spec_id = spec.id

                    with SessionLocal() as db:
                        run = await ExperimentRunnerService(db).start_and_run(
                            project_id,
                            RunStartIn(spec_id=outcome.spec_id, worker=worker, seed=seed),
                            # Top-level batch gate already fired; do not
                            # double-gate each idea.
                            require_approval=False,
                        )
                        outcome.run_id = run.id
                        outcome.run_status = run.status
                        outcome.result_class = run.result_class

                    # Analyse only if the run actually finished.
                    if outcome.run_id and outcome.run_status in (
                        "succeeded",
                        "failed",
                        "timed_out",
                    ):
                        with SessionLocal() as db:
                            analysis = await ResultAnalysisService(db).analyze(
                                outcome.run_id
                            )
                            outcome.verdict = analysis.verdict
                            outcome.claim_ids = list(analysis.claim_ids)
                except BudgetExceededError as e:
                    # Budget-specific: set the shared halt flag so later
                    # ideas short-circuit without their own pre-check race.
                    budget_halt["hit"] = True
                    outcome.error = (
                        f"budget_exceeded: ${e.spent_usd:.4f} "
                        f">= ${e.ceiling_usd:.4f}"
                    )
                except Exception as e:  # noqa: BLE001
                    # Fail-soft: record, don't raise.
                    outcome.error = f"{type(e).__name__}: {e}"[:500]
                    logger.warning(
                        "batch idea failed",
                        extra={"idea_id": idea_id, "err": outcome.error},
                    )
            return outcome

        outcomes = await asyncio.gather(
            *[_one(i, seed_base + k) for k, i in enumerate(ideas)],
            return_exceptions=False,
        )

        # Final batch-level audit.
        succeeded = sum(1 for o in outcomes if o.ok)
        self.audit.log(
            project_id=project_id,
            kind=AuditKind.run_finished,
            message=(
                f"Batch finished: {succeeded}/{len(outcomes)} ideas succeeded "
                f"({sum(1 for o in outcomes if o.error)} errors)"
            ),
            subject_kind="project",
            subject_id=project_id,
            payload={
                "outcomes": [
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
            },
        )
        self.db.commit()
        return outcomes
