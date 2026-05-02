"""Deterministic claim ↔ draft-section alignment.

Providers sometimes omit ``claim_refs`` on draft sections even when the
claim rows exist. This helper runs after draft assembly and attaches
``claim_refs`` using three monotone strategies:

1. **Value match** - if the claim's ``value`` string (e.g. ``"+0.0342"``) or
   any digit token from it appears in the section body, link.
2. **Keyword match** - strong tokens from the claim's ``text`` (length ≥ 5,
   alphabetic, not in a small stopword list) that occur in the section
   body cause a link.
3. **Structural fallback** - for the ``experiments`` and ``results``
   sections, any claim with a ``run_id`` is linked as a last-resort
   structural anchor, because those sections describe the runs the claims
   came from.

We never invent claim ids, and we never drop claim_refs that were already
there - this pass is strictly additive.

All operations are in-place on SQLAlchemy instances; the caller is
responsible for committing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Sequence

from app.core.models import Claim, DraftSection
from app.utils import get_logger

logger = get_logger(__name__)


_DIGIT_RE = re.compile(r"[+-]?\d+(?:[\.,]\d+)?%?")
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_\-]{4,}")
_STRUCTURAL_SECTION_KEYS = {"experiments", "results", "method"}

# Small stopword list to reduce false positives on generic words. Kept
# intentionally tiny; the goal is fewer junk matches, not NLP completeness.
_STOPWORDS = {
    "draft", "mock", "smoke", "abstract", "section", "paper", "research",
    "results", "result", "method", "methods", "variant", "baseline",
    "project", "experiments", "experiment", "appendix", "figure", "table",
    "placeholder", "fallback", "evidence", "claim", "claims", "manuscript",
    "scientific", "local", "model", "provider",
}


@dataclass
class AlignmentReport:
    added_refs: int
    newly_cited_claims: int
    sections_updated: int
    sections_seen: int
    claims_seen: int


def align_sections_with_claims(
    sections: Sequence[DraftSection],
    claims: Sequence[Claim],
    *,
    structural_fallback: bool = True,
) -> AlignmentReport:
    if not sections:
        return AlignmentReport(0, 0, 0, 0, len(claims))

    claim_by_id = {c.id: c for c in claims}
    already_cited: set[str] = set()
    for s in sections:
        for ref in s.claim_refs or []:
            if isinstance(ref, str):
                already_cited.add(ref)

    added_refs = 0
    newly_cited: set[str] = set()
    sections_updated = 0

    for section in sections:
        content = section.content or ""
        if not content.strip():
            continue
        haystack = content.lower()
        existing_refs = list(section.claim_refs or [])
        existing_set = {r for r in existing_refs if isinstance(r, str)}
        new_refs: list[str] = []

        for claim in claims:
            if claim.id in existing_set:
                continue
            if _claim_matches_section(claim=claim, section=section, haystack=haystack):
                new_refs.append(claim.id)
                existing_set.add(claim.id)

        # Structural fallback for the sections that canonically *describe*
        # the runs: after trying content-based matches, inject every
        # run-backed claim into experiments / results / method.
        if (
            structural_fallback
            and section.key in _STRUCTURAL_SECTION_KEYS
        ):
            for claim in claims:
                if claim.id in existing_set:
                    continue
                if claim.run_id:
                    new_refs.append(claim.id)
                    existing_set.add(claim.id)

        if new_refs:
            section.claim_refs = existing_refs + new_refs
            added_refs += len(new_refs)
            sections_updated += 1
            for rid in new_refs:
                if rid not in already_cited:
                    newly_cited.add(rid)

    return AlignmentReport(
        added_refs=added_refs,
        newly_cited_claims=len(newly_cited),
        sections_updated=sections_updated,
        sections_seen=len(sections),
        claims_seen=len(claims),
    )


def _claim_matches_section(*, claim: Claim, section: DraftSection, haystack: str) -> bool:
    # 1. value match
    value = (claim.value or "").strip()
    if value:
        if value.lower() in haystack:
            return True
        for m in _DIGIT_RE.finditer(value):
            tok = m.group(0).strip().strip("%+-.,")
            if tok and len(tok) >= 3 and tok in haystack:
                return True

    # 2. strong-token keyword match on claim.text
    text = claim.text or ""
    tokens = {
        m.group(0).lower()
        for m in _WORD_RE.finditer(text)
    }
    meaningful = [t for t in tokens if t not in _STOPWORDS]
    # Require two overlapping tokens to reduce false positives on common
    # English prose.
    overlap = sum(1 for t in meaningful if t in haystack)
    if overlap >= 2:
        return True
    return False
