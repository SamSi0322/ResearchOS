"""Lightweight background reminder loop.

Runs ``ApprovalService.reminder_scan`` (and ``expiration_scan``) on a fixed
interval inside the FastAPI event loop. Deliberately not a real scheduler —
we do NOT want Celery / Redis / APScheduler in this local MVP. The loop is:

* Gated by ``RESEARCHOS_REMINDER_INTERVAL_MINUTES`` (0 = off, default).
* Started from the FastAPI startup hook; cancelled on shutdown.
* Best-effort: a failure in one tick is logged and the loop continues.

Operators who want precise control can still POST ``/api/approvals/scan/reminders``
at any time — the scan is idempotent.
"""

from __future__ import annotations

import asyncio

from app.config import get_settings
from app.db.session import SessionLocal
from app.services.approval_service import ApprovalService
from app.utils import get_logger

logger = get_logger(__name__)


async def scan_pending_approvals() -> dict:
    """Run one reminder + expiration scan. Returns a compact summary.

    Safe to call from anywhere (startup loop, a CLI, or tests).
    """
    with SessionLocal() as db:
        svc = ApprovalService(db)
        expired = svc.expiration_scan()
        reminded = await svc.reminder_scan()
    return {
        "reminded": [a.id for a in reminded],
        "reminded_count": len(reminded),
        "expired": [a.id for a in expired],
        "expired_count": len(expired),
    }


async def _reminder_loop(interval_seconds: int) -> None:
    logger.info(
        "approval reminder loop started", extra={"interval_s": interval_seconds}
    )
    try:
        while True:
            try:
                summary = await scan_pending_approvals()
                if summary["reminded_count"] or summary["expired_count"]:
                    logger.info("approval reminder tick", extra=summary)
            except Exception as e:  # noqa: BLE001
                # Never let a bad tick kill the loop.
                logger.warning("approval reminder tick failed: %s", e)
            await asyncio.sleep(interval_seconds)
    except asyncio.CancelledError:
        logger.info("approval reminder loop cancelled")
        raise


def start_reminder_loop() -> asyncio.Task | None:
    """Spawn the background task if the env flag enables it.

    Returns the ``Task`` so the caller (FastAPI startup) can stash it and
    cancel it on shutdown. Returns ``None`` when the loop is disabled.
    """
    settings = get_settings()
    minutes = int(getattr(settings, "reminder_scan_interval_minutes", 0) or 0)
    if minutes <= 0:
        logger.info(
            "approval reminder loop disabled "
            "(set RESEARCHOS_REMINDER_INTERVAL_MINUTES > 0 to enable)"
        )
        return None
    seconds = max(60, minutes * 60)  # never poll faster than once a minute
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        return None
    return loop.create_task(_reminder_loop(seconds))
