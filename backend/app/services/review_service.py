"""Review issue generation + issue-state management.

Independent reviewer passes produce review issues. Each issue is tied to a
subject (draft / run / claim / spec / package). Package freeze rules live in
`package_service.py`; this service exposes the CRUD + the LLM-generation
entry point.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.enums import AuditKind, ReviewerClass, ReviewSeverity, ReviewState, TaskKind
from app.core.models import (
    Claim,
    Draft,
    DraftSection,
    ExperimentRun,
    Manuscript,
    ReviewIssue,
)
from app.core.schemas import ReviewIssueIn, ReviewRunIn
from app.config import Phase, resolve_model_policy
from app.providers.base import CompletionRequest, apply_policy, apply_smoke_limits
from app.providers.router import get_provider_router
from app.services._prompts import dump_json_block, load_prompt, safe_json_object
from app.services.audit_service import AuditService
from app.services.provider_call_ledger import complete_with_ledger
from app.utils import get_logger, new_id

logger = get_logger(__name__)

_DEFAULT_REVIEWERS = [
    ReviewerClass.methodology,
    ReviewerClass.statistics,
    ReviewerClass.novelty,
    ReviewerClass.reproducibility,
    ReviewerClass.manuscript,
]


class ReviewService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.audit = AuditService(db)

    async def run_reviewers(self, project_id: str, payload: ReviewRunIn) -> list[ReviewIssue]:
        reviewer_classes = payload.reviewer_classes or [r.value for r in _DEFAULT_REVIEWERS]

        draft = None
        if payload.draft_id:
            draft = self.db.query(Draft).filter(Draft.id == payload.draft_id).first()
            if draft is None:
                raise LookupError(f"draft not found: {payload.draft_id}")
        else:
            manuscript = (
                self.db.query(Manuscript).filter(Manuscript.project_id == project_id).first()
            )
            if manuscript is not None:
                draft = (
                    self.db.query(Draft)
                    .filter(Draft.manuscript_id == manuscript.id)
                    .order_by(Draft.version.desc())
                    .first()
                )

        runs = (
            self.db.query(ExperimentRun)
            .filter(ExperimentRun.project_id == project_id)
            .order_by(ExperimentRun.created_at.desc())
            .all()
        )
        claims = self.db.query(Claim).filter(Claim.project_id == project_id).all()

        sections_payload = []
        if draft is not None:
            for sec in (
                self.db.query(DraftSection).filter(DraftSection.draft_id == draft.id).all()
            ):
                sections_payload.append(
                    {
                        "key": sec.key,
                        "title": sec.title,
                        "content": sec.content,
                        "claim_refs": sec.claim_refs,
                        "evidence_refs": sec.evidence_refs,
                    }
                )

        policy = resolve_model_policy(Phase.manuscript_review)
        router = get_provider_router(self.db)
        resolved = router.resolve_with_policy(policy)
        system = (
            "You are a panel of independent reviewers. For each reviewer class you are "
            "asked to emulate, produce 1-3 issues. Respond with JSON only. Severity is "
            "one of P0, P1, P2, P3. P0 = blocking, P1 = major, P2 = minor, P3 = info."
        )
        body = (
            (load_prompt("review.md") or "")
            + f"\nReviewer classes: {reviewer_classes}\n"
            + dump_json_block("Draft sections", sections_payload)
            + dump_json_block(
                "Runs",
                [
                    {
                        "id": r.id,
                        "status": r.status,
                        "result_class": r.result_class,
                        "metrics": r.metrics,
                        "mock": r.mock,
                    }
                    for r in runs
                ],
            )
            + dump_json_block(
                "Claims",
                [
                    {"id": c.id, "text": c.text, "value": c.value, "mock": c.mock}
                    for c in claims
                ],
            )
            + f"\nExtra instructions: {payload.extra_instructions or '(none)'}\n"
            + dump_json_block(
                "Output schema",
                {
                    "issues": [
                        {
                            "reviewer_class": "methodology|statistics|novelty|reproducibility|manuscript|package",
                            "severity": "P0|P1|P2|P3",
                            "subject_kind": "draft|run|claim|spec|package",
                            "subject_id": "id",
                            "description": "what is wrong",
                            "evidence": "why",
                            "suggested_remediation": "what to do",
                        }
                    ]
                },
            )
        )

        try:
            req = CompletionRequest(
                system=system,
                prompt=body,
                temperature=0.2,
                max_tokens=3500,
                json_mode=True,
                task_kind=TaskKind.review.value,
                extra={"reviewer_classes": reviewer_classes},
            )
            req = apply_policy(req, policy)
            req = apply_smoke_limits(req, get_settings())
            result = await complete_with_ledger(
                self.db,
                project_id=project_id,
                adapter=resolved.adapter,
                req=req,
                reference=f"manuscript_review:{draft.id if draft else project_id}",
                meta={
                    "draft_id": draft.id if draft else None,
                    "reviewer_classes": reviewer_classes,
                },
            )
            parsed = safe_json_object(result.text)
            # Providers have drifted between returning {"issues": [...]} and
            # returning the same payload as a bare array (normalised to
            # {"items": [...]} by safe_json_object). Accept both shapes so we
            # never silently drop real reviewer output.
            raw = (
                parsed.get("issues")
                or parsed.get("items")
                or parsed.get("reviews")
                or []
            )
            mock = result.mock
        except Exception as e:  # noqa: BLE001
            logger.warning("review provider call failed, falling back: %s", e)
            raw = self._fallback_issues(reviewer_classes, draft, runs, claims)
            mock = True

        saved: list[ReviewIssue] = []
        for entry in raw:
            reviewer_class = entry.get("reviewer_class") or "methodology"
            try:
                reviewer_class_value = ReviewerClass(reviewer_class).value
            except ValueError:
                reviewer_class_value = ReviewerClass.methodology.value
            severity = entry.get("severity") or "P2"
            try:
                severity_value = ReviewSeverity(severity).value
            except ValueError:
                severity_value = ReviewSeverity.P2.value

            subject_kind = entry.get("subject_kind") or ("draft" if draft else "run")
            subject_id = entry.get("subject_id") or (draft.id if draft else (runs[0].id if runs else project_id))

            issue = ReviewIssue(
                id=new_id("iss"),
                project_id=project_id,
                draft_id=draft.id if draft else None,
                subject_kind=subject_kind,
                subject_id=subject_id,
                reviewer_class=reviewer_class_value,
                severity=severity_value,
                state=ReviewState.open.value,
                description=(entry.get("description") or "").strip()[:2000] or "(no description)",
                evidence=(entry.get("evidence") or "").strip() or None,
                suggested_remediation=(entry.get("suggested_remediation") or "").strip() or None,
                meta={"mock": mock, "policy": policy.as_metadata()},
            )
            self.db.add(issue)
            saved.append(issue)
            self.audit.log(
                project_id=project_id,
                kind=AuditKind.review_issue_opened,
                message=f"{issue.reviewer_class} {issue.severity}: {issue.description[:60]}",
                subject_kind="review_issue",
                subject_id=issue.id,
                payload={"reviewer_class": issue.reviewer_class, "severity": issue.severity},
            )

        self.db.commit()
        return saved

    def update(self, issue_id: str, payload: ReviewIssueIn) -> ReviewIssue:
        issue = self.db.query(ReviewIssue).filter(ReviewIssue.id == issue_id).first()
        if issue is None:
            raise LookupError(f"issue not found: {issue_id}")
        previous_state = issue.state
        if payload.severity:
            issue.severity = ReviewSeverity(payload.severity).value
        if payload.state:
            issue.state = ReviewState(payload.state).value
        if payload.resolution_note is not None:
            issue.resolution_note = payload.resolution_note

        if issue.state != previous_state:
            if issue.state == ReviewState.resolved.value:
                self.audit.log(
                    project_id=issue.project_id,
                    kind=AuditKind.review_issue_resolved,
                    subject_kind="review_issue",
                    subject_id=issue.id,
                    message=f"resolved by operator: {issue.resolution_note or ''}",
                )
            elif issue.state == ReviewState.waived.value:
                self.audit.log(
                    project_id=issue.project_id,
                    kind=AuditKind.review_issue_waived,
                    subject_kind="review_issue",
                    subject_id=issue.id,
                    message=f"waived by operator: {issue.resolution_note or ''}",
                    actor="operator",
                )
        self.db.commit()
        return issue

    def list(self, project_id: str) -> list[ReviewIssue]:
        return (
            self.db.query(ReviewIssue)
            .filter(ReviewIssue.project_id == project_id)
            .order_by(ReviewIssue.created_at.desc())
            .all()
        )

    def _fallback_issues(
        self,
        reviewer_classes: list[str],
        draft,
        runs,
        claims,
    ) -> list[dict]:
        issues = []
        for rc in reviewer_classes:
            subject_kind = "draft" if draft else "run"
            subject_id = draft.id if draft else (runs[0].id if runs else "")
            sev = "P2"
            desc = f"[FALLBACK] Generic {rc} review note - provider call unavailable."
            if rc == "statistics" and claims:
                sev = "P1"
                desc = "Need multi-seed aggregation before trusting single-seed claims."
            elif rc == "reproducibility":
                desc = "Document the exact seed, dataset hash, and environment."
            issues.append(
                {
                    "reviewer_class": rc,
                    "severity": sev,
                    "subject_kind": subject_kind,
                    "subject_id": subject_id,
                    "description": desc,
                    "evidence": "derived from local fallback",
                    "suggested_remediation": "Re-run reviewers with provider credentials configured.",
                }
            )
        return issues
