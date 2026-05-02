from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.schemas import RunOut, RunStartIn
from app.core.schemas.experiment import RunAnalysisOut
from app.db import get_session
from app.services import ExperimentRunnerService, ResultAnalysisService
from app.services.batch_orchestrator_service import (
    BatchBlockedError,
    BatchOrchestratorService,
)
from app.services.experiment_runner_service import (
    BudgetExceededError,
    RunBlockedError,
)

router = APIRouter()


class BatchRunIn(BaseModel):
    """Payload for the backend-orchestrated batch run endpoint.

    ``idea_ids`` is required. ``worker`` defaults to the two-step builder +
    reviewer collaboration (the intended production default). ``concurrency``
    overrides the process-wide setting if set.
    """

    idea_ids: list[str] = Field(min_length=1)
    worker: str = "two_step"
    seed_base: int = 0
    concurrency: int | None = None


class BatchRunOut(BaseModel):
    outcomes: list[dict[str, Any]]
    total: int
    succeeded: int
    failed: int


@router.get("", response_model=list[RunOut])
def list_runs(project_id: str, db: Session = Depends(get_session)):
    return ExperimentRunnerService(db).list(project_id)


def _run_blocked_response(exc: RunBlockedError | BatchBlockedError) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "error": "approval_required",
            "stage_key": exc.stage_key,
            "reason": exc.reason,
            "approval_id": exc.approval_id,
            "status": exc.status,
        },
    )


def _budget_exceeded_response(exc: BudgetExceededError) -> HTTPException:
    return HTTPException(
        status_code=402,
        detail={
            "error": "budget_exceeded",
            "project_id": exc.project_id,
            "spent_usd": exc.spent_usd,
            "ceiling_usd": exc.ceiling_usd,
        },
    )


@router.post("/start", response_model=RunOut)
async def start_run(
    project_id: str,
    payload: RunStartIn,
    provider_credential_id: str | None = Query(None),
    db: Session = Depends(get_session),
):
    """Start a single run.

    When HITL is enabled on the project AND the relevant gate
    (``post_shortlist`` or ``post_pilot_evidence``) is in the project's
    ``approval_gates`` list, this endpoint returns **409** with a structured
    body describing the pending approval instead of starting the run. This
    matches the batch endpoint's behaviour — there is no single-run bypass.
    When the project's budget ceiling has already been spent, this endpoint
    returns **402**.
    """
    try:
        return await ExperimentRunnerService(db).start_and_run(
            project_id, payload, provider_credential_id=provider_credential_id
        )
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    except RunBlockedError as e:
        raise _run_blocked_response(e) from e
    except BudgetExceededError as e:
        raise _budget_exceeded_response(e) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.post("/batch", response_model=BatchRunOut)
async def batch_run(
    project_id: str,
    payload: BatchRunIn,
    db: Session = Depends(get_session),
):
    """Run multiple ideas in bounded-concurrency parallel through the
    backend orchestrator.

    Backend is the source of truth for:
      * HITL approval gating (returns 409 with a structured body)
      * budget ceiling enforcement (ledger-backed, returns 402)
      * concurrency cap (``RESEARCHOS_CONCURRENCY_PER_BATCH``)

    The frontend used to fan out spec/run creation client-side — that path
    is deprecated; browsers now call this endpoint so the orchestrator's
    gates cannot be bypassed by skipping the UI.
    """
    try:
        outcomes = await BatchOrchestratorService(db).run_batch(
            project_id=project_id,
            idea_ids=payload.idea_ids,
            worker=payload.worker,
            seed_base=payload.seed_base,
            concurrency=payload.concurrency,
        )
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    except BatchBlockedError as e:
        raise _run_blocked_response(e) from e
    except BudgetExceededError as e:
        raise _budget_exceeded_response(e) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    payloads = [
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
    succeeded = sum(1 for o in outcomes if o.ok)
    failed = sum(1 for o in outcomes if o.error)
    return BatchRunOut(
        outcomes=payloads,
        total=len(outcomes),
        succeeded=succeeded,
        failed=failed,
    )


@router.get("/{run_id}", response_model=RunOut)
def get_run(project_id: str, run_id: str, db: Session = Depends(get_session)):
    try:
        return ExperimentRunnerService(db).get(run_id)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e


@router.post("/{run_id}/analyze", response_model=RunAnalysisOut)
async def analyze_run(
    project_id: str, run_id: str, db: Session = Depends(get_session)
):
    try:
        return await ResultAnalysisService(db).analyze(run_id)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
