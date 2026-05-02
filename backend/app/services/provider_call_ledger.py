from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.providers.base import CompletionRequest
from app.services.budget_service import BudgetService
from app.utils import get_logger

logger = get_logger(__name__)


async def complete_with_ledger(
    db: Session,
    *,
    project_id: str,
    adapter,
    req: CompletionRequest,
    reference: str | None = None,
    meta: dict[str, Any] | None = None,
):
    """Run a provider call and append a provider_call budget ledger entry.

    The call result is recorded as the source of truth for non-run LLM spend
    and for per-phase transparency. We do not commit here; the owning service
    keeps transaction control and commits the ledger entry with its domain
    writes.
    """

    result = await adapter.complete(req)
    record_provider_call(
        db,
        project_id=project_id,
        req=req,
        result=result,
        reference=reference,
        meta=meta,
        commit=False,
    )
    return result


def record_provider_call(
    db: Session,
    *,
    project_id: str,
    req: CompletionRequest,
    result,
    reference: str | None = None,
    meta: dict[str, Any] | None = None,
    commit: bool = False,
) -> None:
    if getattr(result, "mock", False):
        return

    amount = float(
        getattr(result, "estimated_cost_usd", 0.0)
        or getattr(result, "estimated_cost", 0.0)
        or 0.0
    )
    raw = dict(getattr(result, "raw", {}) or {})
    usage = dict(getattr(result, "usage", {}) or {})
    ref = reference or raw.get("id") or req.phase or req.task_kind or "provider_call"
    payload = {
        "phase": req.phase,
        "task_kind": req.task_kind,
        "provider": getattr(result, "provider", None),
        "requested_model": getattr(result, "requested_model", None),
        "actual_model": getattr(result, "actual_model", None) or getattr(result, "model", None),
        "requested_reasoning_effort": getattr(result, "requested_reasoning_effort", None),
        "actual_reasoning_effort": getattr(result, "actual_reasoning_effort", None),
        "thinking_mode": getattr(result, "thinking_mode", None),
        "policy_label": getattr(result, "policy_label", None),
        "alias_status": getattr(result, "alias_status", None),
        "usage": usage,
        "latency_ms": int(getattr(result, "latency_ms", 0) or 0),
        "provider_response_id": raw.get("id"),
        "cost_estimate_missing": amount == 0.0 and bool(usage),
    }
    if meta:
        payload.update(meta)

    try:
        BudgetService(db).record(
            project_id=project_id,
            amount_usd=round(amount, 6),
            kind="provider_call",
            reference=str(ref)[:255],
            meta=payload,
            commit=commit,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "provider_call ledger entry skipped",
            extra={
                "project_id": project_id,
                "phase": req.phase,
                "task_kind": req.task_kind,
                "error": str(e),
            },
        )
