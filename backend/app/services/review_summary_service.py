"""Aggregate review issues into a single summary dict.

Small, deterministic. Used by the PackageService to drop
``review_summary.json`` into the ZIP, by the ReadinessService to compute
release posture, and by the frontend to draw the review tab header.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.core.enums import ReviewerClass, ReviewSeverity, ReviewState
from app.core.models import ReviewIssue


class ReviewSummaryService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def summary(self, project_id: str) -> dict[str, Any]:
        issues = (
            self.db.query(ReviewIssue)
            .filter(ReviewIssue.project_id == project_id)
            .all()
        )

        by_severity: dict[str, int] = {s.value: 0 for s in ReviewSeverity}
        by_reviewer: dict[str, int] = {r.value: 0 for r in ReviewerClass}
        by_state: dict[str, int] = {s.value: 0 for s in ReviewState}

        blocking_open = 0
        major_open = 0
        waived = 0
        resolved = 0

        for i in issues:
            by_severity[i.severity] = by_severity.get(i.severity, 0) + 1
            by_reviewer[i.reviewer_class] = by_reviewer.get(i.reviewer_class, 0) + 1
            by_state[i.state] = by_state.get(i.state, 0) + 1
            if i.state == ReviewState.open.value:
                if i.severity == ReviewSeverity.P0.value:
                    blocking_open += 1
                elif i.severity == ReviewSeverity.P1.value:
                    major_open += 1
            elif i.state == ReviewState.waived.value:
                waived += 1
            elif i.state == ReviewState.resolved.value:
                resolved += 1

        return {
            "total": len(issues),
            "by_severity": by_severity,
            "by_reviewer_class": by_reviewer,
            "by_state": by_state,
            "blocking_open": blocking_open,
            "major_open": major_open,
            "waived": waived,
            "resolved": resolved,
        }
