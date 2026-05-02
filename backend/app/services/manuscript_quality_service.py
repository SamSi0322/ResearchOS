"""Compute structural quality diagnostics for a draft.

Produces a single ``QualityReport`` dict the package / readiness layer
consumes. It never calls the provider - this is pure analysis of what is
already stored in the DB.

Fields (all lowercase snake_case):

* ``placeholder_count``             : int - sections whose body contains a
                                      ``[PLACEHOLDER]`` marker or is empty.
* ``empty_section_count``           : int - sections whose body is blank.
* ``unsupported_claim_reference_count``
                                    : int - claim ids referenced by sections
                                      that do NOT exist in the project's
                                      Claim table.
* ``evidence_coverage_ratio``       : float in [0,1] - fraction of claims
                                      referenced by at least one section.
* ``draft_completeness_score``      : float in [0,1] - composite metric. See
                                      ``_completeness`` below.
* ``has_mock_inputs``               : bool
* ``has_smoke_inputs``              : bool
* ``section_coverage``              : dict[str, bool] - per-section key, did
                                      it produce non-placeholder content?
* ``claim_count``                   : int
* ``run_count``                     : int
* ``notes``                         : list[str] - human-readable hints for
                                      the operator.

Used by ``PackageService`` to write ``quality.json`` into the ZIP and by the
``ReadinessService`` + PDF renderer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.models import Claim, Draft, DraftSection, ExperimentRun, Manuscript
from app.utils import get_logger

logger = get_logger(__name__)


_PLACEHOLDER_TOKENS = ("[PLACEHOLDER]", "[placeholder]", "MOCK draft", "[FALLBACK]")


@dataclass
class QualityReport:
    placeholder_count: int
    empty_section_count: int
    unsupported_claim_reference_count: int
    evidence_coverage_ratio: float
    draft_completeness_score: float
    has_mock_inputs: bool
    has_smoke_inputs: bool
    section_coverage: dict[str, bool]
    claim_count: int
    run_count: int
    notes: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "placeholder_count": self.placeholder_count,
            "empty_section_count": self.empty_section_count,
            "unsupported_claim_reference_count": self.unsupported_claim_reference_count,
            "evidence_coverage_ratio": round(self.evidence_coverage_ratio, 4),
            "draft_completeness_score": round(self.draft_completeness_score, 4),
            "has_mock_inputs": self.has_mock_inputs,
            "has_smoke_inputs": self.has_smoke_inputs,
            "section_coverage": self.section_coverage,
            "claim_count": self.claim_count,
            "run_count": self.run_count,
            "notes": self.notes,
        }


class ManuscriptQualityService:
    def __init__(self, db: Session) -> None:
        self.db = db

    # --- public ---------------------------------------------------------

    def latest_report(self, project_id: str) -> QualityReport | None:
        """Return a quality report for the newest draft of this project."""
        manuscript = (
            self.db.query(Manuscript)
            .filter(Manuscript.project_id == project_id)
            .order_by(Manuscript.created_at.desc())
            .first()
        )
        if manuscript is None:
            return None
        draft = (
            self.db.query(Draft)
            .filter(Draft.manuscript_id == manuscript.id)
            .order_by(Draft.version.desc())
            .first()
        )
        if draft is None:
            return None
        return self.report_for_draft(draft)

    def report_for_draft(self, draft: Draft) -> QualityReport:
        settings = get_settings()
        sections = (
            self.db.query(DraftSection)
            .filter(DraftSection.draft_id == draft.id)
            .order_by(DraftSection.order_index.asc())
            .all()
        )
        project_claims = (
            self.db.query(Claim)
            .filter(Claim.project_id == _project_id_for_draft(self.db, draft))
            .all()
        )
        runs = (
            self.db.query(ExperimentRun)
            .filter(ExperimentRun.project_id == _project_id_for_draft(self.db, draft))
            .all()
        )

        claim_ids = {c.id for c in project_claims}
        section_coverage: dict[str, bool] = {}

        placeholder_count = 0
        empty_section_count = 0
        unsupported_refs = 0
        referenced_claim_ids: set[str] = set()

        for sec in sections:
            content = (sec.content or "").strip()
            is_placeholder = False
            if not content:
                empty_section_count += 1
                is_placeholder = True
            elif any(tok in content for tok in _PLACEHOLDER_TOKENS):
                placeholder_count += 1
                is_placeholder = True
            section_coverage[sec.key] = not is_placeholder

            for ref in sec.claim_refs or []:
                if not isinstance(ref, str):
                    continue
                if ref not in claim_ids:
                    unsupported_refs += 1
                else:
                    referenced_claim_ids.add(ref)

        if project_claims:
            evidence_coverage_ratio = len(referenced_claim_ids) / len(project_claims)
        else:
            evidence_coverage_ratio = 0.0

        has_mock = (
            draft.mock
            or any(c.mock for c in project_claims)
            or any(r.mock for r in runs)
        )
        has_smoke = bool(getattr(settings, "smoke_mode", False)) or bool(
            draft.meta and draft.meta.get("has_smoke_inputs")
        )

        completeness = _completeness(
            sections=sections,
            placeholder_count=placeholder_count,
            empty_section_count=empty_section_count,
            unsupported_refs=unsupported_refs,
            evidence_coverage_ratio=evidence_coverage_ratio,
            has_mock=has_mock,
        )

        notes: list[str] = []
        if placeholder_count:
            notes.append(
                f"{placeholder_count} section(s) still contain placeholder text."
            )
        if empty_section_count:
            notes.append(f"{empty_section_count} section(s) are empty.")
        if unsupported_refs:
            notes.append(
                f"{unsupported_refs} claim reference(s) do not resolve to any stored claim."
            )
        if evidence_coverage_ratio < 0.25 and project_claims:
            notes.append(
                "Less than a quarter of stored claims are cited by the manuscript."
            )
        if has_mock:
            notes.append("Draft inherits MOCK experimental evidence.")
        if has_smoke:
            notes.append("Draft was generated while smoke-mode budgets were active.")

        return QualityReport(
            placeholder_count=placeholder_count,
            empty_section_count=empty_section_count,
            unsupported_claim_reference_count=unsupported_refs,
            evidence_coverage_ratio=evidence_coverage_ratio,
            draft_completeness_score=completeness,
            has_mock_inputs=has_mock,
            has_smoke_inputs=has_smoke,
            section_coverage=section_coverage,
            claim_count=len(project_claims),
            run_count=len(runs),
            notes=notes,
        )


def _project_id_for_draft(db: Session, draft: Draft) -> str:
    m = db.query(Manuscript).filter(Manuscript.id == draft.manuscript_id).first()
    return m.project_id if m else ""


def _completeness(
    *,
    sections: list[DraftSection],
    placeholder_count: int,
    empty_section_count: int,
    unsupported_refs: int,
    evidence_coverage_ratio: float,
    has_mock: bool,
) -> float:
    """Compose a single 0..1 score.

    Weight structure so that:
    * having zero sections = 0.
    * every section non-placeholder = high base.
    * evidence coverage adds up to 0.3.
    * each unsupported claim ref shaves a small amount.
    * mock inputs cap the score below 0.9 so a mock manuscript cannot claim
      full readiness.
    """
    if not sections:
        return 0.0
    n = len(sections)
    non_placeholder = max(0, n - placeholder_count - empty_section_count)
    base = 0.55 * (non_placeholder / n)
    ev = 0.30 * max(0.0, min(1.0, evidence_coverage_ratio))
    structural_bonus = 0.15 if n >= 6 else 0.10
    penalty = min(0.2, 0.03 * max(0, unsupported_refs))
    score = base + ev + structural_bonus - penalty
    if has_mock:
        score = min(score, 0.9)
    return max(0.0, min(1.0, score))
