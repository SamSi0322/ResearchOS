"""Human-in-the-loop approval gates.

Responsibilities:

* Persist ``ApprovalRequest`` rows tied to ``(project_id, stage_key)``.
* Decide whether a pipeline step may proceed:

    * project.human_in_loop_enabled is false → always proceed.
    * HITL on + gate not in project.approval_gates → proceed.
    * HITL on + gate in approval_gates + no approval exists yet → create a
      request, send email / outbox entry, return ``PAUSED``.
    * HITL on + open pending request → return ``PAUSED`` (do NOT spawn a
      duplicate).
    * HITL on + latest approval was ``approve`` → proceed.
    * HITL on + latest approval was ``reject`` / ``clarification_requested``
      / ``expired`` → return ``BLOCKED`` so callers can report why.

* Handle decide() with audit + follow-up email.
* Handle reminder_scan() + expiration_scan() as idempotent operators.

This service is intentionally the only place the pipeline talks to the email
abstraction. Services like ``BatchOrchestratorService`` call
``ensure_gate()`` and react to the returned ``GateResult``.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.enums import (
    ApprovalDecision,
    ApprovalStatus,
    AuditKind,
    GateKey,
)
from app.core.models import ApprovalRequest, StudentProject
from app.services.audit_service import AuditService
from app.services.email_service import EmailService, get_email_service
from app.utils import get_logger, new_id

logger = get_logger(__name__)


class GateDecision(str, Enum):
    proceed = "proceed"
    paused = "paused"
    blocked = "blocked"


@dataclass
class GateResult:
    decision: GateDecision
    approval: ApprovalRequest | None = None
    reason: str | None = None

    @property
    def can_proceed(self) -> bool:
        return self.decision is GateDecision.proceed


class ApprovalService:
    def __init__(self, db: Session, email: EmailService | None = None) -> None:
        self.db = db
        self.audit = AuditService(db)
        self.email = email or get_email_service()
        self.settings = get_settings()

    # --- public / pipeline side --------------------------------------

    async def ensure_gate(
        self,
        *,
        project_id: str,
        stage_key: str,
        context_snapshot: dict[str, Any] | None = None,
    ) -> GateResult:
        project = (
            self.db.query(StudentProject)
            .filter(StudentProject.id == project_id)
            .first()
        )
        if project is None:
            raise LookupError(f"project not found: {project_id}")

        # Short circuit when HITL is off OR the gate isn't in the project's
        # enabled gate list.
        if not project.human_in_loop_enabled:
            return GateResult(decision=GateDecision.proceed, reason="hitl_off")
        enabled_gates = project.approval_gates or []
        if stage_key not in enabled_gates:
            return GateResult(
                decision=GateDecision.proceed, reason=f"gate_{stage_key}_not_enabled"
            )

        latest = self._latest_for_gate(project_id, stage_key)
        if latest is None:
            approval = await self._create_request(
                project=project,
                stage_key=stage_key,
                context_snapshot=context_snapshot or {},
            )
            return GateResult(
                decision=GateDecision.paused,
                approval=approval,
                reason="approval_created",
            )

        if latest.status == ApprovalStatus.approved.value:
            return GateResult(decision=GateDecision.proceed, approval=latest)
        if latest.status == ApprovalStatus.pending.value:
            return GateResult(
                decision=GateDecision.paused,
                approval=latest,
                reason="approval_still_pending",
            )
        if latest.status in {
            ApprovalStatus.rejected.value,
            ApprovalStatus.clarification_requested.value,
            ApprovalStatus.expired.value,
            ApprovalStatus.canceled.value,
        }:
            return GateResult(
                decision=GateDecision.blocked,
                approval=latest,
                reason=f"approval_{latest.status}",
            )
        # Unknown state - be conservative.
        return GateResult(
            decision=GateDecision.blocked,
            approval=latest,
            reason=f"unexpected_status:{latest.status}",
        )

    # --- CRUD + decide -----------------------------------------------

    def list_for_project(self, project_id: str) -> list[ApprovalRequest]:
        return (
            self.db.query(ApprovalRequest)
            .filter(ApprovalRequest.project_id == project_id)
            .order_by(ApprovalRequest.created_at.desc())
            .all()
        )

    def list_pending(self) -> list[ApprovalRequest]:
        return (
            self.db.query(ApprovalRequest)
            .filter(ApprovalRequest.status == ApprovalStatus.pending.value)
            .order_by(ApprovalRequest.requested_at.asc())
            .all()
        )

    def get(self, approval_id: str) -> ApprovalRequest:
        row = (
            self.db.query(ApprovalRequest)
            .filter(ApprovalRequest.id == approval_id)
            .first()
        )
        if row is None:
            raise LookupError(f"approval not found: {approval_id}")
        return row

    def get_by_token(self, token: str) -> ApprovalRequest:
        row = (
            self.db.query(ApprovalRequest)
            .filter(ApprovalRequest.token == token)
            .first()
        )
        if row is None:
            raise LookupError("approval token not found")
        return row

    async def decide(
        self,
        approval_id: str,
        *,
        decision: str,
        note: str | None = None,
        actor: str = "operator",
    ) -> ApprovalRequest:
        approval = self.get(approval_id)
        if approval.status != ApprovalStatus.pending.value:
            raise ValueError(
                f"approval already resolved (status={approval.status})"
            )

        try:
            dec = ApprovalDecision(decision)
        except ValueError as e:
            raise ValueError(
                "decision must be one of: approve | reject | request_changes"
            ) from e

        now = datetime.utcnow()
        approval.decision = dec.value
        approval.decision_note = note
        approval.resolved_at = now

        if dec is ApprovalDecision.approve:
            approval.status = ApprovalStatus.approved.value
            audit_kind = AuditKind.approval_approved
            resume_kind = AuditKind.pipeline_resumed
            subject_msg = "approved"
        elif dec is ApprovalDecision.reject:
            approval.status = ApprovalStatus.rejected.value
            audit_kind = AuditKind.approval_rejected
            resume_kind = None
            subject_msg = "rejected"
        else:
            approval.status = ApprovalStatus.clarification_requested.value
            audit_kind = AuditKind.approval_clarification_requested
            resume_kind = None
            subject_msg = "clarification requested"

        self.audit.log(
            project_id=approval.project_id,
            kind=audit_kind,
            message=f"{subject_msg} at gate {approval.stage_key} by {actor}",
            subject_kind="approval",
            subject_id=approval.id,
            actor=actor,
            payload={
                "stage_key": approval.stage_key,
                "decision": dec.value,
                "note": note,
            },
        )
        if resume_kind is not None:
            self.audit.log(
                project_id=approval.project_id,
                kind=resume_kind,
                subject_kind="project",
                subject_id=approval.project_id,
                actor=actor,
                message=f"pipeline resumed past gate {approval.stage_key}",
            )
        self.db.commit()

        # Fire-and-forget confirmation email; failures should never roll back
        # the operator's decision.
        try:
            await self._send_decision_email(approval=approval, actor=actor)
        except Exception as e:  # noqa: BLE001
            logger.warning("decision email skipped: %s", e)
        return approval

    async def reminder_scan(self) -> list[ApprovalRequest]:
        """Send reminders for pending approvals whose interval elapsed.

        Idempotent: advances ``reminder_count`` and ``last_reminder_at`` so a
        second call in the same interval is a no-op. Returns the list of
        approvals that actually had a reminder sent this call.
        """
        now = datetime.utcnow()
        pending = self.list_pending()
        reminded: list[ApprovalRequest] = []
        for approval in pending:
            # Resolve project for its reminder interval override.
            project = (
                self.db.query(StudentProject)
                .filter(StudentProject.id == approval.project_id)
                .first()
            )
            interval = (
                int(project.reminder_interval_hours)
                if project and project.reminder_interval_hours
                else 24
            )
            # Baseline: the last reminder (or the original request) plus the
            # interval. ``last_reminder_sent_at`` is the explicit field used
            # for this guard; ``last_reminder_at`` is kept as a back-compat
            # mirror so existing queries keep working.
            anchor = (
                approval.last_reminder_sent_at
                or approval.last_reminder_at
                or approval.requested_at
            )
            if now - anchor < timedelta(hours=interval):
                continue
            if approval.timeout_at and now >= approval.timeout_at:
                # Would expire anyway; skip reminder here.
                continue
            await self._send_reminder_email(approval=approval)
            approval.reminder_count += 1
            approval.last_reminder_at = now
            approval.last_reminder_sent_at = now
            self.audit.log(
                project_id=approval.project_id,
                kind=AuditKind.approval_reminder_sent,
                message=f"reminder {approval.reminder_count} sent for gate {approval.stage_key}",
                subject_kind="approval",
                subject_id=approval.id,
                payload={"reminder_count": approval.reminder_count},
            )
            reminded.append(approval)
        if reminded:
            self.db.commit()
        return reminded

    def expiration_scan(self) -> list[ApprovalRequest]:
        now = datetime.utcnow()
        pending = self.list_pending()
        expired: list[ApprovalRequest] = []
        for approval in pending:
            if approval.timeout_at and now >= approval.timeout_at:
                approval.status = ApprovalStatus.expired.value
                approval.resolved_at = now
                self.audit.log(
                    project_id=approval.project_id,
                    kind=AuditKind.approval_expired,
                    message=f"approval expired at gate {approval.stage_key}",
                    subject_kind="approval",
                    subject_id=approval.id,
                )
                expired.append(approval)
        if expired:
            self.db.commit()
        return expired

    # --- helpers -----------------------------------------------------

    def _latest_for_gate(self, project_id: str, stage_key: str) -> ApprovalRequest | None:
        return (
            self.db.query(ApprovalRequest)
            .filter(
                ApprovalRequest.project_id == project_id,
                ApprovalRequest.stage_key == stage_key,
            )
            .order_by(ApprovalRequest.created_at.desc())
            .first()
        )

    async def _create_request(
        self,
        *,
        project: StudentProject,
        stage_key: str,
        context_snapshot: dict[str, Any],
    ) -> ApprovalRequest:
        approver = (project.primary_approver_email or "").strip()
        cc = list(project.cc_emails or [])
        if not approver:
            # Still create the row so the operator can see + action it in the
            # UI, but mark the email attempt as skipped.
            approver = self.settings.smtp_sender
        timeout_hours = int(project.approval_timeout_hours or 72)
        now = datetime.utcnow()
        approval = ApprovalRequest(
            id=new_id("apr"),
            project_id=project.id,
            stage_key=stage_key,
            status=ApprovalStatus.pending.value,
            approver_email=approver,
            cc_emails=cc,
            requested_at=now,
            timeout_at=now + timedelta(hours=timeout_hours),
            token=secrets.token_urlsafe(18),
            context_snapshot=context_snapshot or {},
        )
        self.db.add(approval)
        self.db.flush()

        self.audit.log(
            project_id=project.id,
            kind=AuditKind.approval_created,
            message=f"approval requested at gate {stage_key}",
            subject_kind="approval",
            subject_id=approval.id,
            payload={
                "stage_key": stage_key,
                "timeout_at": approval.timeout_at.isoformat(),
                "approver_email": approver,
            },
        )
        self.audit.log(
            project_id=project.id,
            kind=AuditKind.pipeline_paused,
            message=f"pipeline paused at gate {stage_key}",
            subject_kind="project",
            subject_id=project.id,
        )
        self.db.commit()

        try:
            result = await self._send_initial_email(approval=approval, project=project)
            if result.outbox_path:
                approval.outbox_path = result.outbox_path
                self.db.commit()
        except Exception as e:  # noqa: BLE001
            logger.warning("approval email skipped: %s", e)
        return approval

    def _links(self, approval: ApprovalRequest) -> dict[str, str]:
        base = self.settings.console_base_url.rstrip("/")
        token = approval.token
        return {
            "approve": f"{base}/approvals/{token}?decision=approve",
            "reject": f"{base}/approvals/{token}?decision=reject",
            "clarify": f"{base}/approvals/{token}?decision=request_changes",
            "console": f"{base}/projects/{approval.project_id}/approvals",
            "api_action": (
                f"{base}/api/approvals/token/{token}"
            ),
        }

    async def _send_initial_email(
        self, *, approval: ApprovalRequest, project: StudentProject
    ):
        links = self._links(approval)
        body = (
            f"Approval requested for project: {project.title}\n"
            f"Gate: {approval.stage_key}\n"
            f"Deadline: {approval.timeout_at:%Y-%m-%d %H:%M UTC}\n"
            f"\n"
            f"Context summary (truncated):\n"
            f"{_render_context(approval.context_snapshot)}\n"
            f"\n"
            f"Actions:\n"
            f"  approve:   {links['approve']}\n"
            f"  reject:    {links['reject']}\n"
            f"  clarify:   {links['clarify']}\n"
            f"  console:   {links['console']}\n"
            f"\n"
            f"If no action is taken before the deadline, the approval will be marked expired.\n"
            f"\n"
            f"-- ResearchOS internal (local)\n"
        )
        return await self.email.send(
            to=approval.approver_email,
            cc=approval.cc_emails or [],
            subject=f"[ResearchOS] Approval needed: {project.title} / {approval.stage_key}",
            body_text=body,
        )

    async def _send_reminder_email(self, *, approval: ApprovalRequest):
        links = self._links(approval)
        body = (
            f"Reminder: this ResearchOS approval request is still pending.\n"
            f"Gate: {approval.stage_key}\n"
            f"Requested: {approval.requested_at:%Y-%m-%d %H:%M UTC}\n"
            f"Deadline: {approval.timeout_at:%Y-%m-%d %H:%M UTC}\n"
            f"\n"
            f"Actions:\n"
            f"  approve: {links['approve']}\n"
            f"  reject:  {links['reject']}\n"
            f"  clarify: {links['clarify']}\n"
            f"  console: {links['console']}\n"
        )
        return await self.email.send(
            to=approval.approver_email,
            cc=approval.cc_emails or [],
            subject=f"[ResearchOS] Reminder: approval needed ({approval.stage_key})",
            body_text=body,
        )

    async def _send_decision_email(self, *, approval: ApprovalRequest, actor: str):
        body = (
            f"Decision recorded on ResearchOS approval request.\n"
            f"Gate: {approval.stage_key}\n"
            f"Decision: {approval.decision}\n"
            f"Decided by: {actor}\n"
            f"Note: {approval.decision_note or '(none)'}\n"
        )
        return await self.email.send(
            to=approval.approver_email,
            cc=approval.cc_emails or [],
            subject=(
                f"[ResearchOS] {approval.decision.upper()} "
                f"on approval {approval.stage_key}"
            ),
            body_text=body,
        )


def _render_context(ctx: dict[str, Any] | None) -> str:
    if not ctx:
        return "(no summary provided)"
    lines: list[str] = []
    for k, v in ctx.items():
        rendered = str(v)
        if len(rendered) > 240:
            rendered = rendered[:240] + "…"
        lines.append(f"  - {k}: {rendered}")
    return "\n".join(lines)
