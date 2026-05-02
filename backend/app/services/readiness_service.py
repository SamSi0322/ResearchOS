"""Combine quality + review + mock/smoke state into a single readiness view.

Readiness tiers:

* ``internal_draft_only`` - heavy placeholders, mock inputs, or smoke mode.
* ``needs_revision``      - there are open P0 or P1 review issues, or very low
                            completeness.
* ``ready_for_mentor_review`` - non-blocking, coverage is decent, but still
                            requires human sign-off before any external use.
* ``mentor_signoff_required`` - quality + review are clean but at least one
                            P2 was not waived. This is intentionally the best
                            we can report automatically; final approval is
                            always human.

The service NEVER returns "ready_to_submit" or any phrasing that could be
mistaken for formal approval.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.enums import ReviewSeverity, ReviewState
from app.services.manuscript_quality_service import (
    ManuscriptQualityService,
    QualityReport,
)
from app.services.review_summary_service import ReviewSummaryService


class ReadinessService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.quality = ManuscriptQualityService(db)
        self.review = ReviewSummaryService(db)

    def summary(self, project_id: str) -> dict[str, Any]:
        q_report: QualityReport | None = self.quality.latest_report(project_id)
        q_dict = q_report.as_dict() if q_report else None
        review = self.review.summary(project_id)
        settings = get_settings()

        smoke_mode = bool(settings.smoke_mode)
        has_mock = bool(q_dict and q_dict.get("has_mock_inputs"))
        placeholder_count = int(q_dict.get("placeholder_count", 0)) if q_dict else 0
        completeness = float(q_dict.get("draft_completeness_score", 0.0)) if q_dict else 0.0
        coverage = float(q_dict.get("evidence_coverage_ratio", 0.0)) if q_dict else 0.0

        blocking_open = int(review["blocking_open"])
        major_open = int(review["major_open"])
        minor_open = int(
            review["by_severity"].get(ReviewSeverity.P2.value, 0)
        ) - int(review["waived"])
        minor_open = max(0, minor_open)

        reasons: list[str] = []
        if q_dict is None:
            tier = "no_draft"
            reasons.append("No draft has been generated yet.")
        elif has_mock or smoke_mode:
            tier = "internal_draft_only"
            if smoke_mode:
                reasons.append("Smoke-mode budgets were active when draft was produced.")
            if has_mock:
                reasons.append("Draft inherits MOCK experimental evidence.")
        elif blocking_open or major_open:
            tier = "needs_revision"
            if blocking_open:
                reasons.append(f"{blocking_open} open P0 (blocking) review issue(s).")
            if major_open:
                reasons.append(f"{major_open} open P1 (major) review issue(s).")
        elif placeholder_count > 2 or completeness < 0.4 or coverage < 0.25:
            tier = "needs_revision"
            if placeholder_count > 2:
                reasons.append(f"{placeholder_count} section placeholder(s) remain.")
            if completeness < 0.4:
                reasons.append(
                    f"Draft completeness score {completeness:.2f} is below the 0.40 revision threshold."
                )
            if coverage < 0.25:
                reasons.append(
                    f"Only {coverage:.0%} of stored claims are cited by the draft."
                )
        elif minor_open:
            tier = "ready_for_mentor_review"
            reasons.append(
                f"{minor_open} open P2 (minor) review issue(s) — waive or resolve before mentor sign-off."
            )
        else:
            tier = "mentor_signoff_required"
            reasons.append(
                "All automated checks passed; final approval is always a mentor decision."
            )

        package_decision = _package_decision(tier, blocking_open, major_open)

        return {
            "tier": tier,
            "package_decision": package_decision,
            "reasons": reasons,
            "quality": q_dict,
            "review": review,
            "smoke_mode": smoke_mode,
            "has_mock_inputs": has_mock,
        }


def _package_decision(tier: str, blocking_open: int, major_open: int) -> str:
    """Is the project currently packageable?

    Matches the hard rules in PackageService.build():
    * open P0 / P1 = refuse;
    * open P2 = allowed only with ``allow_with_waived_p2``.
    This function reports the *expectation*; the package service still
    enforces the guard.
    """
    if blocking_open or major_open:
        return "refused: resolve or waive open P0/P1 issues"
    if tier == "no_draft":
        return "refused: no draft to package"
    return "allowed (mentor sign-off still required for external use)"
