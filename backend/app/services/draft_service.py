"""Evidence-first manuscript drafting.

Key rule: a draft must reference Claim objects for any substantive empirical
statement. We enforce this by passing the full set of Claims + selected
ExperimentRun metrics into the prompt and refusing to finalise the draft if
no claims exist yet (the caller gets a warning-laden draft instead of a
full success).
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.enums import AuditKind, DraftStatus, TaskKind
from app.core.models import (
    Artifact,
    AuditEvent,
    Claim,
    Draft,
    DraftSection,
    ExperimentRun,
    Idea,
    Manuscript,
    StudentProject,
)
from app.core.schemas import DraftGenerateIn
from app.config import Phase, resolve_model_policy
from app.providers.base import CompletionRequest, apply_policy, apply_smoke_limits
from app.providers.router import get_provider_router
from app.services._prompts import dump_json_block, load_prompt, safe_json_object
from app.services.audit_service import AuditService
from app.services.provider_call_ledger import complete_with_ledger
from app.storage import get_artifact_store
from app.utils import get_logger, new_id

import re
_DIGIT_GROUP_RE = re.compile(r"[+-]?\d+(?:[\.,]\d+)?(?:%?)")


class _SkipFullDraftGeneration(Exception):
    pass


def _has_unsupported_numbers(text: str, claims: list) -> bool:
    """Heuristic: does the polished text contain numeric tokens that do not
    appear in any existing claim's `text` or `value` field?

    Used as a cheap fabrication guard for the second-pass polish. Digit tokens
    that appear in the claim payload are considered "already seen" and are
    allowed to stay in the polished prose.
    """
    if not text:
        return False
    tokens = set(m.group(0).strip() for m in _DIGIT_GROUP_RE.finditer(text))
    if not tokens:
        return False
    corpus_parts: list[str] = []
    for c in claims:
        corpus_parts.append(str(getattr(c, "text", "") or ""))
        corpus_parts.append(str(getattr(c, "value", "") or ""))
    corpus = " ".join(corpus_parts)
    for t in tokens:
        # Whitelist common structural numbers like "1" in bullet enumeration and
        # small integers that appear alone - these cannot meaningfully leak a
        # fabricated metric.
        bare = t.strip("%+-.,")
        if bare and len(bare) <= 2 and bare.isdigit():
            continue
        if t not in corpus:
            return True
    return False

logger = get_logger(__name__)

_DEFAULT_SECTION_KEYS = [
    ("abstract", "Abstract"),
    ("introduction", "Introduction"),
    ("method", "Method"),
    ("experiments", "Experiments"),
    ("results", "Results"),
    ("discussion", "Discussion"),
    ("limitations", "Limitations"),
    ("conclusion", "Conclusion"),
]
_PLACEHOLDER_MARKERS = (
    "[placeholder]",
    "awaiting review",
    "awaiting mentor review",
    "left to the mentor team during review",
    "left to the reviewer during review",
    "to be discussed by mentor team",
    "to be discussed during review",
    "tbd",
)
_QUALITATIVE_SECTION_KEYS = {
    "introduction",
    "method",
    "discussion",
    "limitations",
    "conclusion",
}


class DraftService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.audit = AuditService(db)

    async def generate(self, project_id: str, payload: DraftGenerateIn) -> Draft:
        project = self.db.query(StudentProject).filter(StudentProject.id == project_id).first()
        if project is None:
            raise LookupError(f"project not found: {project_id}")

        claims = self.db.query(Claim).filter(Claim.project_id == project_id).all()
        run_query = self.db.query(ExperimentRun).filter(ExperimentRun.project_id == project_id)
        if payload.include_run_ids:
            run_query = run_query.filter(ExperimentRun.id.in_(payload.include_run_ids))
        runs = run_query.order_by(ExperimentRun.created_at.desc()).all()

        has_mock = any(c.mock for c in claims) or any(r.mock for r in runs)

        manuscript = (
            self.db.query(Manuscript).filter(Manuscript.project_id == project_id).first()
        )
        manuscript_needs_insert = manuscript is None
        manuscript_title = payload.manuscript_title or (
            manuscript.title if manuscript is not None else project.title
        )
        manuscript_target_venue = payload.target_venue or (
            manuscript.target_venue
            if manuscript is not None
            else (project.target_venues[0] if project.target_venues else None)
        )
        if manuscript is None:
            manuscript = Manuscript(
                id=new_id("ms"),
                project_id=project_id,
                title=manuscript_title,
                target_venue=manuscript_target_venue,
                status=DraftStatus.drafting.value,
            )

        prior = (
            self.db.query(Draft)
            .filter(Draft.manuscript_id == manuscript.id)
            .order_by(Draft.version.desc())
            .first()
        )
        next_version = (prior.version + 1) if prior else 1

        policy = resolve_model_policy(Phase.draft_generation)
        router = get_provider_router(self.db)

        claim_payload = [
            {
                "id": c.id,
                "text": c.text,
                "kind": c.kind,
                "value": c.value,
                "quantitative": c.quantitative,
                "evidence_refs": c.evidence_refs,
                "mock": c.mock,
            }
            for c in claims
        ]
        runs_payload = []
        for r in runs:
            runs_payload.append(
                {
                    "id": r.id,
                    "status": r.status,
                    "result_class": r.result_class,
                    "metrics": r.metrics,
                    "mock": r.mock,
                    "summary": r.summary,
                }
            )
        run_analysis = self._run_analysis_map(project_id, runs)
        idea_context = self._idea_context_for_runs(runs)
        analysis_summary = self._analysis_summary(
            runs, claims, run_analysis, idea_context
        )

        system = (
            "You are an honest scientific writer drafting an evidence-first "
            "research manuscript for a technical reader. You MUST NOT invent numbers. Every numeric claim in "
            "results / experiments / abstract must reference an existing Claim by id. "
            "Write complete paper-style sections, not a shell full of placeholders. "
            "When evidence is limited, state the uncertainty in prose instead of "
            "writing '[PLACEHOLDER]', 'awaiting review', or similar filler. "
            "Results must be organized around empirical themes instead of a raw claim list. "
            "Use idea titles instead of opaque run ids in prose whenever titles are available. "
            "Do not expose raw claim ids or run ids in visible prose; use claim_refs and "
            "evidence_refs fields for traceability. Method, results, discussion, and "
            "limitations must be substantive multi-paragraph sections. "
            "Discussion, limitations, and conclusion must still read like full sections. "
            "Respond with JSON only."
        )
        prompt = (
            (load_prompt("drafting.md") or "")
            + dump_json_block(
                "Project context",
                {
                    "title": project.title,
                    "research_direction": project.research_direction,
                    "constraints": project.constraints,
                    "exploration_strategy": project.exploration_strategy,
                    "target_venue": manuscript_target_venue or "n/a",
                    "target_venues": project.target_venues,
                    "has_mock_inputs": has_mock,
                },
            )
            + dump_json_block("Claims", claim_payload)
            + dump_json_block("Run summaries", runs_payload)
            + dump_json_block("Run analyses", run_analysis)
            + dump_json_block("Run idea context", idea_context)
            + dump_json_block("Analysis summary", analysis_summary)
            + f"\nExtra instructions: {payload.extra_instructions or '(none)'}\n"
            + dump_json_block(
                "Output schema",
                {
                    "sections": [
                        {
                            "key": "abstract|introduction|method|experiments|results|discussion|limitations|conclusion",
                            "title": "readable title",
                            "content": "markdown",
                            "claim_refs": ["claim ids"],
                            "evidence_refs": [{"type": "run", "id": "run id"}],
                        }
                    ]
                },
            )
        )
        if not self.db.new and not self.db.dirty and not self.db.deleted:
            # Release SQLite read locks before slow provider calls. Draft rows are
            # written only after provider/fallback text has been produced.
            self.db.commit()

        sectional_first = os.environ.get(
            "RESEARCHOS_DRAFT_SECTIONAL_FIRST", "true"
        ).strip().lower() not in {"0", "false", "no", "off"} and "pro" in policy.model.lower()

        try:
            if sectional_first:
                parsed = {"sections": []}
                result = None  # type: ignore
                provider_fallback = None
                raise _SkipFullDraftGeneration()
            base_req = CompletionRequest(
                system=system,
                prompt=prompt,
                temperature=0.3,
                max_tokens=policy.max_output_tokens,
                json_mode=True,
                task_kind=TaskKind.draft_generation.value,
                extra={
                    "project_title": project.title,
                    "target_metric": (project.target_venues or ["n/a"])[0],
                    "claims": claim_payload,
                },
            )
            result, provider_fallback = await self._complete_with_provider_fallback(
                router=router,
                primary_policy=policy,
                base_req=base_req,
                settings=get_settings(),
                phase_label="draft_generation",
                project_id=project_id,
                reference=f"draft_generation:{manuscript.id}:v{next_version}",
            )
            parsed = safe_json_object(result.text)
        except _SkipFullDraftGeneration:
            parsed = {"sections": []}
            result = None  # type: ignore
            provider_fallback = None
        except Exception as e:  # noqa: BLE001
            logger.warning("draft provider call failed, falling back: %s", e)
            parsed = {"sections": []}
            result = None  # type: ignore
            provider_fallback = None

        if manuscript_needs_insert:
            self.db.add(manuscript)
        else:
            if payload.manuscript_title and manuscript.title != manuscript_title:
                manuscript.title = manuscript_title
            if payload.target_venue and manuscript.target_venue != manuscript_target_venue:
                manuscript.target_venue = manuscript_target_venue
        if prior is not None:
            prior.status = DraftStatus.superseded.value

        # Provider shape drift: accept {"sections":[...]}, {"items":[...]} or a
        # bare list - the normaliser already puts bare lists at "items".
        sections_input = (
            parsed.get("sections")
            or parsed.get("items")
            or []
        )
        by_key = {s.get("key"): s for s in sections_input if isinstance(s, dict)}
        section_generation_meta: dict[str, object] = {}
        if any(
            not (by_key.get(key) or {}).get("content")
            or _is_placeholder_text(str((by_key.get(key) or {}).get("content") or ""))
            for key, _ in _DEFAULT_SECTION_KEYS
        ):
            by_key, section_generation_meta, section_result = (
                await self._fill_missing_sections_with_provider(
                    by_key=by_key,
                    project=project,
                    claims=claims,
                    runs=runs,
                    has_mock=has_mock,
                    run_analysis=run_analysis,
                    idea_context=idea_context,
                    analysis_summary=analysis_summary,
                    router=router,
                    policy=policy,
                    settings=get_settings(),
                    project_id=project_id,
                    manuscript_id=manuscript.id,
                    draft_version=next_version,
                )
            )
            if result is None and section_result is not None:
                result = section_result
        fallback_section_keys: list[str] = []

        draft = Draft(
            id=new_id("draft"),
            manuscript_id=manuscript.id,
            version=next_version,
            status=DraftStatus.drafting.value,
            claim_ids=[c.id for c in claims],
            meta={
                "provider": getattr(result, "provider", None) if result else "fallback",
                "model": getattr(result, "model", None) if result else "",
                "requested_model": getattr(result, "requested_model", None) if result else None,
                "actual_model": getattr(result, "actual_model", None) if result else None,
                "requested_reasoning_effort": (
                    getattr(result, "requested_reasoning_effort", None) if result else None
                ),
                "actual_reasoning_effort": (
                    getattr(result, "actual_reasoning_effort", None) if result else None
                ),
                "mock": getattr(result, "mock", False) if result else False,
                "claims_total": len(claims),
                "runs_total": len(runs),
                "has_mock_inputs": has_mock,
                "policy": policy.as_metadata(),
                "provider_fallback": provider_fallback,
                "provider_text_len": len(getattr(result, "text", "") or "") if result else 0,
                "provider_sections_count": len(by_key),
                "fallback_section_keys": fallback_section_keys,
                "sectional_generation": section_generation_meta,
            },
            notes=("This draft was generated with at least one MOCK-tagged artifact or run."
                   if has_mock
                   else None),
            mock=has_mock,
        )
        self.db.add(draft)
        self.db.flush()

        for order, (key, title) in enumerate(_DEFAULT_SECTION_KEYS):
            payload_sec = by_key.get(key) or {}
            content = (payload_sec.get("content") or "").strip()
            used_fallback = False
            if not content or _is_placeholder_text(content):
                fallback_section_keys.append(key)
                content = self._fallback_section(
                    key,
                    project,
                    claims,
                    runs,
                    has_mock,
                    run_analysis=run_analysis,
                    idea_context=idea_context,
                    analysis_summary=analysis_summary,
                )
                used_fallback = True
            claim_refs = [
                ref for ref in (payload_sec.get("claim_refs") or []) if isinstance(ref, str)
            ]
            if used_fallback and not claim_refs:
                claim_refs = self._default_claim_refs_for_section(key, claims)
            ds = DraftSection(
                id=new_id("sec"),
                draft_id=draft.id,
                key=key,
                title=payload_sec.get("title") or title,
                content=content,
                order_index=order,
                claim_refs=claim_refs,
                evidence_refs=list(payload_sec.get("evidence_refs") or []),
            )
            self.db.add(ds)

        draft.meta = {
            **(draft.meta or {}),
            "fallback_section_keys": fallback_section_keys,
            "provider_sections_count": len(by_key),
            "sectional_generation": section_generation_meta,
        }

        manuscript.status = DraftStatus.in_review.value

        # --- Deterministic evidence alignment ---------------------------------
        # Providers (especially the reviewer tier) often omit section-level
        # claim_refs even when the Claim rows exist. We run a strictly
        # additive alignment pass right after the sections are created so the
        # manuscript quality / readiness signals are based on real coverage.
        try:
            from app.services.evidence_alignment import align_sections_with_claims

            # SessionLocal is autoflush=False, so the pending DraftSection
            # adds from the loop above are not visible to a query yet. Flush
            # so the alignment pass sees the sections we just created.
            self.db.flush()
            saved_sections = (
                self.db.query(DraftSection)
                .filter(DraftSection.draft_id == draft.id)
                .order_by(DraftSection.order_index.asc())
                .all()
            )
            alignment = align_sections_with_claims(
                saved_sections, claims, structural_fallback=True
            )
            logger.info(
                "evidence alignment ran: added=%s sections_updated=%s "
                "sections_seen=%s claims_seen=%s",
                alignment.added_refs,
                alignment.sections_updated,
                alignment.sections_seen,
                alignment.claims_seen,
            )
            if alignment.added_refs:
                draft.meta = {
                    **(draft.meta or {}),
                    "evidence_alignment": {
                        "added_refs": alignment.added_refs,
                        "newly_cited_claims": alignment.newly_cited_claims,
                        "sections_updated": alignment.sections_updated,
                    },
                }
                self.audit.log(
                    project_id=project_id,
                    kind=AuditKind.draft_revised,
                    message=(
                        f"Evidence alignment added {alignment.added_refs} claim_refs "
                        f"across {alignment.sections_updated} section(s)"
                    ),
                    subject_kind="draft",
                    subject_id=draft.id,
                    payload={
                        "added_refs": alignment.added_refs,
                        "newly_cited_claims": alignment.newly_cited_claims,
                    },
                )
                self.db.flush()
        except Exception as e:  # noqa: BLE001
            logger.warning("evidence alignment skipped: %s", e)

        # --- Second-pass polish ------------------------------------------------
        # Optional refinement: ask the provider to tighten wording for each
        # non-placeholder section, WITHOUT introducing new claims or numbers.
        # Fallbacks and provider errors are intentionally non-fatal.
        settings = get_settings()
        if payload.extra_instructions != "__skip_polish__":  # escape hatch for tests
            try:
                await self._polish_sections_in_place(
                    draft=draft,
                    project=project,
                    claims=claims,
                    settings=settings,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("second-pass polish skipped: %s", e)

        # Persist the full draft as an artifact blob too for ZIP packaging.
        blob = {
            "manuscript": {
                "id": manuscript.id,
                "title": manuscript_title,
                "target_venue": manuscript_target_venue,
            },
            "version": next_version,
            "sections": [
                {
                    "key": ds.key,
                    "title": ds.title,
                    "content": ds.content,
                    "claim_refs": ds.claim_refs,
                    "evidence_refs": ds.evidence_refs,
                }
                for ds in self.db.query(DraftSection).filter(DraftSection.draft_id == draft.id).all()
            ],
            "claim_ids": draft.claim_ids,
            "mock": draft.mock,
        }
        artifact_store = get_artifact_store()
        stored = artifact_store.write_text(
            project_id,
            f"manuscripts/{manuscript.id}/draft_v{next_version}.json",
            json.dumps(blob, indent=2, default=str),
        )
        self.db.add(
            Artifact(
                id=new_id("art"),
                project_id=project_id,
                run_id=None,
                kind="draft",
                name=f"draft_v{next_version}.json",
                path=str(stored.path),
                sha256=stored.sha256,
                size_bytes=stored.size_bytes,
                mock=draft.mock,
                meta={"manuscript_id": manuscript.id, "draft_id": draft.id},
            )
        )

        self.audit.log(
            project_id=project_id,
            kind=AuditKind.draft_created,
            message=f"Draft v{next_version} of {manuscript.title[:40]}",
            subject_kind="draft",
            subject_id=draft.id,
            payload={"manuscript_id": manuscript.id, "mock": draft.mock},
        )
        self.db.commit()
        return draft

    async def _polish_sections_in_place(
        self,
        *,
        draft: Draft,
        project: StudentProject,
        claims: list[Claim],
        settings,
    ) -> None:
        """Refine wording on already-drafted sections.

        Hard rules enforced via the system prompt and by the post-parse
        sanitiser below:

        * no new numeric claims,
        * no new claim_refs beyond those already on the section,
        * sections flagged as ``[PLACEHOLDER]`` stay placeholders unless the
          relevant claims exist in the project, in which case we upgrade
          strictly from those claims,
        * section key / order_index are never changed,
        * we always preserve the non-empty fallback copy.
        """
        sections = (
            self.db.query(DraftSection)
            .filter(DraftSection.draft_id == draft.id)
            .order_by(DraftSection.order_index.asc())
            .all()
        )
        if not sections:
            return

        allowed_claim_ids = {c.id for c in claims}
        payload_sections = [
            {
                "key": s.key,
                "title": s.title,
                "content": s.content,
                "claim_refs": list(s.claim_refs or []),
            }
            for s in sections
        ]
        claim_payload = [
            {"id": c.id, "text": c.text, "value": c.value, "kind": c.kind, "mock": c.mock}
            for c in claims
        ]

        # Polish is its own phase in the model policy so we can tighten it
        # separately (e.g. same model, different temperature).
        policy = resolve_model_policy(Phase.draft_polish)
        router = get_provider_router(self.db)
        system = (
            "You are refining manuscript sections that were produced in an "
            "earlier pass. You MUST NOT introduce new numeric claims, new "
            "citations, new tables, or new claim_ids beyond those listed in "
            "`claims`. Preserve each section's `key`. Replace placeholder-style "
            "text with complete bounded prose whenever the section can be "
            "written from the project context and existing evidence. "
            "Discussion, limitations, and conclusion may be qualitative and "
            "do not require claim_ids unless they add empirical assertions. "
            "Tighten wording, improve structure, and fix grammar only. "
            "Keep manuscript-style prose; avoid operational phrases like "
            "'this internal draft' unless discussing review limitations. "
            "Keep results organized by empirical theme and prefer idea titles "
            "over run ids in prose. "
            "Respond with JSON only."
        )
        prompt = (
            "Polish pass only. Do not invent evidence.\n"
            + dump_json_block(
                "Project context",
                {
                    "title": project.title,
                    "research_direction": project.research_direction,
                    "constraints": project.constraints,
                    "exploration_strategy": project.exploration_strategy,
                    "target_venues": project.target_venues,
                },
            )
            + dump_json_block("Sections", payload_sections)
            + dump_json_block("Claims (only these claim_ids are allowed)", claim_payload)
            + dump_json_block(
                "Output schema",
                {
                    "sections": [
                        {
                            "key": "existing key",
                            "content": "polished markdown",
                            "claim_refs": ["subset of provided claim_ids"],
                        }
                    ]
                },
            )
        )
        base_req = CompletionRequest(
            system=system,
            prompt=prompt,
            temperature=0.2,
            max_tokens=policy.max_output_tokens,
            json_mode=True,
            task_kind=TaskKind.draft_generation.value,
            extra={"polish_pass": True},
        )
        result, provider_fallback = await self._complete_with_provider_fallback(
            router=router,
            primary_policy=policy,
            base_req=base_req,
            settings=settings,
            phase_label="draft_polish",
            project_id=project.id,
            reference=f"draft_polish:{draft.id}:v{draft.version}",
        )
        parsed = safe_json_object(result.text)
        polished = parsed.get("sections") or parsed.get("items") or []
        if not isinstance(polished, list) or not polished:
            return

        by_key = {p.get("key"): p for p in polished if isinstance(p, dict)}
        changed = 0
        for section in sections:
            update = by_key.get(section.key)
            if not isinstance(update, dict):
                continue
            new_content = str(update.get("content", "")).strip()
            if not new_content:
                continue
            if _is_placeholder_text(new_content):
                continue
            # Refuse to upgrade a placeholder without supporting claim refs.
            if (
                _is_placeholder_text(section.content)
                and not update.get("claim_refs")
                and section.key not in _QUALITATIVE_SECTION_KEYS
            ):
                continue
            # Constrain claim_refs strictly to pre-existing refs + allowed set.
            new_refs = [
                r
                for r in (update.get("claim_refs") or [])
                if isinstance(r, str) and r in allowed_claim_ids
            ]
            # Never drop an already-referenced claim.
            for r in section.claim_refs or []:
                if r not in new_refs:
                    new_refs.append(r)
            # Guard against suspicious numeric inflation - any digit group in the
            # refined text that did NOT appear in any claim.value string is
            # considered unverified and we revert. This is a cheap, conservative
            # heuristic; it does not catch everything but it catches blatant
            # fabrication.
            if _has_unsupported_numbers(new_content, claims):
                logger.info(
                    "polish pass produced unsupported numbers, reverting",
                    extra={"section_key": section.key, "draft_id": draft.id},
                )
                continue
            section.content = new_content
            section.claim_refs = new_refs
            changed += 1

        if changed:
            draft.meta = {
                **(draft.meta or {}),
                "polish_pass": {
                    "changed_sections": changed,
                    "provider_fallback": provider_fallback,
                },
            }
            self.audit.log(
                project_id=project.id,
                kind=AuditKind.draft_revised,
                message=f"Polish pass tightened {changed} section(s)",
                subject_kind="draft",
                subject_id=draft.id,
                payload={"changed_sections": changed},
            )

    def _fallback_section(
        self,
        key: str,
        project: StudentProject,
        claims: list[Claim],
        runs: list[ExperimentRun],
        has_mock: bool,
        *,
        run_analysis: dict[str, dict[str, object]],
        idea_context: dict[str, dict[str, str]],
        analysis_summary: dict[str, object],
    ) -> str:
        tag = " (MOCK EVIDENCE)" if has_mock else ""
        if key == "abstract":
            if not claims:
                return (
                    f"[PLACEHOLDER] No claims exist yet - evidence-first drafting refuses "
                    f"to auto-write an abstract for {project.title}."
                )
            highlights = self._claim_highlights(claims, limit=2)
            verdict_line = self._verdict_summary_sentence(analysis_summary, runs)
            pattern_line = self._delta_pattern_sentence(run_analysis)
            return (
                f"We evaluate {project.title} as an evidence-backed research "
                f"exploration.{tag} {verdict_line} {pattern_line} "
                "The evidence does not yet establish a robust improvement, but "
                "it narrows the next experimental decisions by identifying which "
                f"signals appeared in executed runs. The clearest supported "
                f"observations are: {highlights}"
            )
        if key == "introduction":
            return (
                f"{project.research_direction}\n\n"
                "The study frames the project as an evidence-backed exploration "
                "of the current research direction rather than as a finished "
                "scientific claim. The goal is to reduce uncertainty about which "
                "candidate interventions justify deeper validation. "
                f"Constraints: {project.constraints or 'none stated'}."
            )
        if key == "method":
            return (
                "The method follows the experiment specifications linked to this "
                "project. ResearchOS converts shortlisted ideas into executable "
                "runs, captures metrics and logs, and then extracts evidence-backed "
                "claims from the resulting artifacts. "
                f"This report summarizes the runs collected for {project.title}. "
                f"{self._method_scope_sentence(runs, analysis_summary)}"
            )
        if key == "experiments":
            blocks = self._per_run_summary_blocks(
                runs,
                claims,
                run_analysis,
                idea_context,
                limit=min(max(len(runs), 8), 12),
            )
            if blocks:
                return (
                    "The experiments below summarize the executed pilot runs "
                    "included in the evidence bundle. Each line captures the stored "
                    "analysis verdict plus the most informative evidence-backed "
                    "observation from that run.\n\n"
                    + "\n".join(f"- {block}" for block in blocks)
                )
            return "No runs have been executed yet, so this section currently records the intended experiment set rather than completed executions."
        if key == "results":
            if not claims:
                return (
                    "No evidence-backed claims have been recorded yet. This means "
                    "the project has not produced a results section that can support "
                    "empirical interpretation, and follow-up runs are still required."
                )
            paragraphs = self._theme_summary_paragraphs(
                claims,
                runs,
                run_analysis,
                idea_context,
                analysis_summary,
            )
            return "\n\n".join(paragraphs)
        if key == "discussion":
            return (
                "The current evidence should be interpreted as exploratory rather "
                "than conclusive. "
                f"{self._discussion_summary_sentence(analysis_summary, run_analysis, runs)} "
                "Modest metric gains should not be treated as a robust recipe "
                "unless they survive calibration checks, repeated seeds, and "
                "stronger baselines. "
                f"The strongest evidence-backed themes are: {self._claim_highlights(claims, limit=3)} "
                "Taken together, these observations narrow the next research "
                "decisions, but they do not yet justify a broad external claim."
            )
        if key == "limitations":
            base = (
                "This study is limited by the scope of the current evidence bundle. "
                "It reflects only the runs, claims, and artifacts captured in this "
                "cycle, so broader robustness checks, alternative baselines, and "
                "additional replications remain out of scope. The work also remains "
                "bounded by the project constraints on runtime, environment, and task scope."
            )
            if has_mock:
                return (
                    f"{base} The evidence bundle also contains MOCK evidence from the local "
                    "pipeline and must not be used for external publication."
                )
            return (
                f"{base} Any external use should first pass human review, because "
                "the manuscript has not been independently validated as a final "
                "research deliverable."
            )
        if key == "conclusion":
            if not claims:
                return (
                    "The project has established a concrete research direction and "
                    "execution path, but it has not yet accumulated enough "
                    "evidence-backed findings to support a substantive conclusion."
                )
            return (
                "The pilot evidence does not yet support advancing any candidate "
                "as a robust improvement. ResearchOS nevertheless reduced "
                "uncertainty by showing where signals did and did not appear in "
                "executed runs. The immediate value is in clarifying which "
                "hypotheses deserve deeper validation and which uncertainties "
                "still need targeted follow-up work. "
                f"{self._next_step_sentence(analysis_summary, runs)}"
            )
        return (
            "This section remains intentionally conservative because the current "
            "drafting inputs do not justify a stronger claim."
        )

    def _claim_highlights(self, claims: list[Claim], *, limit: int) -> str:
        snippets: list[str] = []
        for claim in self._salient_claims(claims, limit=limit):
            text = (claim.text or "").strip().rstrip(".")
            if text:
                snippets.append(text)
        if not snippets:
            return (
                "the current cycle has not yet produced stable evidence-backed "
                "observations"
            )
        if len(snippets) == 1:
            return snippets[0] + "."
        return "; ".join(snippets[:-1]) + "; and " + snippets[-1] + "."

    def _default_claim_refs_for_section(
        self, key: str, claims: list[Claim]
    ) -> list[str]:
        if not claims:
            return []
        if key in {"abstract", "results", "discussion", "conclusion"}:
            return [c.id for c in self._salient_claims(claims, limit=4)]
        if key == "experiments":
            return [c.id for c in self._salient_claims(claims, limit=6, one_per_run=True)]
        return []

    def _run_analysis_map(
        self, project_id: str, runs: list[ExperimentRun]
    ) -> dict[str, dict[str, object]]:
        run_ids = [r.id for r in runs]
        if not run_ids:
            return {}
        rows = (
            self.db.query(AuditEvent)
            .filter(
                AuditEvent.project_id == project_id,
                AuditEvent.kind == AuditKind.result_validated.value,
                AuditEvent.subject_kind == "run",
                AuditEvent.subject_id.in_(run_ids),
            )
            .all()
        )
        out: dict[str, dict[str, object]] = {}
        for row in rows:
            payload = row.payload or {}
            out[str(row.subject_id)] = {
                "verdict": payload.get("verdict"),
                "delta": payload.get("delta") or {},
                "claim_ids": payload.get("claim_ids") or [],
            }
        return out

    def _analysis_summary(
        self,
        runs: list[ExperimentRun],
        claims: list[Claim],
        run_analysis: dict[str, dict[str, object]],
        idea_context: dict[str, dict[str, str]],
    ) -> dict[str, object]:
        verdicts = {"promising": 0, "inconclusive": 0, "rejected": 0, "unknown": 0}
        for run in runs:
            verdict = str((run_analysis.get(run.id) or {}).get("verdict") or "unknown")
            verdicts[verdict if verdict in verdicts else "unknown"] += 1
        return {
            "total_runs": len(runs),
            "successful_runs": sum(1 for r in runs if r.status == "succeeded"),
            "verdicts": verdicts,
            "representative_findings": self._representative_findings(
                claims, runs, run_analysis, idea_context, limit=4
            ),
        }

    def _results_overview_sentence(
        self, analysis_summary: dict[str, object], runs: list[ExperimentRun]
    ) -> str:
        verdicts = analysis_summary.get("verdicts") or {}
        total = analysis_summary.get("total_runs") or len(runs)
        promising = int((verdicts or {}).get("promising") or 0)
        inconclusive = int((verdicts or {}).get("inconclusive") or 0)
        rejected = int((verdicts or {}).get("rejected") or 0)
        return (
            f"Across {total} executed runs, the stored automated analyses judged "
            f"{promising} promising, {inconclusive} inconclusive, and {rejected} rejected. "
            "The overall picture is therefore exploratory: some variants moved individual "
            "metrics, but the evidence did not establish a clearly dominant direction."
        )

    def _theme_summary_paragraphs(
        self,
        claims: list[Claim],
        runs: list[ExperimentRun],
        run_analysis: dict[str, dict[str, object]],
        idea_context: dict[str, dict[str, str]],
        analysis_summary: dict[str, object],
    ) -> list[str]:
        accuracy_claims = self._claims_matching_theme(
            claims,
            (
                "accuracy",
                "acc",
                "f1",
                "auc",
                "precision",
                "recall",
                "performance",
                "higher than baseline",
                "lower than baseline",
                "unchanged",
                "stability",
                "variance",
            ),
            exclude=("brier", "calibration", "confidence", "probability"),
            limit=3,
        )
        calibration_claims = self._claims_matching_theme(
            claims,
            ("calibration", "brier", "confidence", "probability"),
            limit=3,
        )
        efficiency_claims = self._claims_matching_theme(
            claims,
            (
                "epoch",
                "wall-clock",
                "runtime",
                "time",
                "cost",
                "convergence",
                "loss",
                "speed",
                "memory",
            ),
            limit=3,
        )
        robustness_claims = self._claims_matching_theme(
            claims,
            (
                "seed",
                "variance",
                "stability",
                "robust",
                "unchanged",
                "worse",
                "deterioration",
                "did not",
                "not supported",
                "inconclusive",
            ),
            limit=3,
        )

        accuracy_sentence = self._themed_claim_sentence(
            accuracy_claims,
            idea_context,
            default=(
                "The current claim set does not isolate a stable accuracy "
                "improvement across the evaluated candidates."
            ),
        )
        calibration_sentence = self._themed_claim_sentence(
            calibration_claims,
            idea_context,
            default=(
                "The current claim set does not yet show a separate calibration "
                "advantage, so future runs should keep calibration metrics in the "
                "success criteria rather than optimizing accuracy alone."
            ),
        )
        efficiency_sentence = self._themed_claim_sentence(
            efficiency_claims,
            idea_context,
            default=(
                "The recorded evidence does not yet establish an efficiency or "
                "convergence advantage for the tested variants."
            ),
        )
        robustness_sentence = self._themed_claim_sentence(
            robustness_claims,
            idea_context,
            default=(
                "The verdict mix remains exploratory, which means the next cycle "
                "should prioritize repeated seeds and stronger comparisons before "
                "treating any candidate as robust."
            ),
        )

        paragraphs = [
            (
                f"**Accuracy and stability.** "
                f"{self._results_overview_sentence(analysis_summary, runs)} "
                f"{self._delta_pattern_sentence(run_analysis)} "
                f"{accuracy_sentence}"
            ),
            (
                f"**Calibration.** {calibration_sentence} "
                "This matters because an accuracy movement is not sufficient if "
                "confidence quality deteriorates or remains unvalidated."
            ),
            (
                f"**Efficiency and convergence.** {efficiency_sentence} "
                "The current evidence therefore supports narrowing the hypothesis "
                "space more than it supports claiming a faster or cheaper training recipe."
            ),
            (
                f"**Robustness and interpretation.** {robustness_sentence}"
            ),
        ]

        representative = self._representative_findings(
            claims,
            runs,
            run_analysis,
            idea_context,
            limit=3,
        )
        if representative:
            paragraphs.append(
                "**Run-level interpretation.** " + " ".join(representative)
            )
        return paragraphs

    def _verdict_summary_sentence(
        self, analysis_summary: dict[str, object], runs: list[ExperimentRun]
    ) -> str:
        verdicts = analysis_summary.get("verdicts") or {}
        total = analysis_summary.get("total_runs") or len(runs)
        if not total:
            return "No analyzed runs are available yet."
        promising = int((verdicts or {}).get("promising") or 0)
        inconclusive = int((verdicts or {}).get("inconclusive") or 0)
        rejected = int((verdicts or {}).get("rejected") or 0)
        return (
            f"The current evidence bundle covers {total} analyzed runs, with "
            f"{promising} judged promising, {inconclusive} inconclusive, and "
            f"{rejected} rejected by the stored analysis step."
        )

    def _discussion_summary_sentence(
        self,
        analysis_summary: dict[str, object],
        run_analysis: dict[str, dict[str, object]],
        runs: list[ExperimentRun],
    ) -> str:
        verdicts = analysis_summary.get("verdicts") or {}
        if int((verdicts or {}).get("promising") or 0) == 0 and runs:
            detail = self._delta_pattern_sentence(run_analysis)
            return (
                "No run in the current batch crossed the threshold for a clear "
                "promising verdict, which suggests the next iteration should focus "
                "on robustness and discrimination rather than scaling the present variants unchanged. "
                f"{detail}"
            )
        return (
            "The run-level verdict mix indicates that any encouraging signals "
            "still need stronger follow-up before they should influence an external-facing conclusion."
        )

    def _method_scope_sentence(
        self, runs: list[ExperimentRun], analysis_summary: dict[str, object]
    ) -> str:
        successful = int(analysis_summary.get("successful_runs") or 0)
        return (
            f"In this cycle, {successful} of {len(runs)} runs completed successfully "
            "and were available for downstream analysis."
            if runs
            else "No executable runs were available to summarize."
        )

    def _next_step_sentence(
        self, analysis_summary: dict[str, object], runs: list[ExperimentRun]
    ) -> str:
        verdicts = analysis_summary.get("verdicts") or {}
        if int((verdicts or {}).get("promising") or 0) > 0:
            return (
                "The next step is to carry the most promising variants into a more "
                "robust validation pass with stricter comparison criteria."
            )
        if runs:
            return (
                "The next step is to tighten the experimental comparison, rerun the "
                "strongest candidates with more robust checks, and look for signals "
                "that survive beyond the current exploratory setup."
            )
        return "The next step is to execute the planned experiments and collect evidence-backed claims."

    def _per_run_summary_blocks(
        self,
        runs: list[ExperimentRun],
        claims: list[Claim],
        run_analysis: dict[str, dict[str, object]],
        idea_context: dict[str, dict[str, str]],
        *,
        limit: int,
    ) -> list[str]:
        claim_by_run = self._salient_claims_by_run(claims, limit_per_run=2)
        blocks: list[str] = []
        for run in runs[:limit]:
            verdict = str((run_analysis.get(run.id) or {}).get("verdict") or "unknown")
            snippets = claim_by_run.get(run.id) or []
            detail = " ".join(snippets[:2]) if snippets else run.summary or run.result_class or run.status
            label = self._run_label(run, idea_context)
            blocks.append(f"{label}: verdict={verdict}; {detail}")
        return blocks

    def _representative_findings(
        self,
        claims: list[Claim],
        runs: list[ExperimentRun],
        run_analysis: dict[str, dict[str, object]],
        idea_context: dict[str, dict[str, str]],
        *,
        limit: int,
    ) -> list[str]:
        claim_by_run = self._salient_claims_by_run(claims, limit_per_run=2)
        lines: list[str] = []
        for run in runs:
            snippets = claim_by_run.get(run.id) or []
            if not snippets:
                continue
            verdict = str((run_analysis.get(run.id) or {}).get("verdict") or "unknown")
            label = self._run_label(run, idea_context)
            lines.append(f"{label} ({verdict}): {' '.join(snippets[:2])}")
            if len(lines) >= limit:
                break
        if lines:
            return lines
        return [claim.text for claim in self._salient_claims(claims, limit=limit)]

    def _idea_context_for_runs(
        self, runs: list[ExperimentRun]
    ) -> dict[str, dict[str, str]]:
        idea_ids = sorted({r.idea_id for r in runs if r.idea_id})
        if not idea_ids:
            return {}
        ideas = self.db.query(Idea).filter(Idea.id.in_(idea_ids)).all()
        by_id = {
            idea.id: {
                "title": idea.title,
                "summary": idea.summary,
            }
            for idea in ideas
        }
        out: dict[str, dict[str, str]] = {}
        for run in runs:
            if run.idea_id and run.idea_id in by_id:
                out[run.id] = by_id[run.idea_id]
        return out

    def _run_label(
        self, run: ExperimentRun, idea_context: dict[str, dict[str, str]]
    ) -> str:
        title = (idea_context.get(run.id) or {}).get("title")
        return title or run.id

    def _claims_matching_theme(
        self,
        claims: list[Claim],
        tokens: tuple[str, ...],
        *,
        limit: int,
        exclude: tuple[str, ...] = (),
    ) -> list[Claim]:
        out: list[Claim] = []
        for claim in self._salient_claims(claims, limit=len(claims)):
            text = (claim.text or "").lower()
            if exclude and any(token in text for token in exclude):
                continue
            if any(token in text for token in tokens):
                out.append(claim)
            if len(out) >= limit:
                break
        return out

    def _themed_claim_sentence(
        self,
        claims: list[Claim],
        idea_context: dict[str, dict[str, str]],
        *,
        default: str,
    ) -> str:
        snippets: list[str] = []
        for claim in claims:
            text = (claim.text or "").strip()
            if not text:
                continue
            text = text.rstrip(".")
            label = (idea_context.get(claim.run_id or "") or {}).get("title")
            if label and label.lower() not in text.lower():
                text = f"For {label}, {self._lower_initial(text)}"
            snippets.append(text + ".")
        return " ".join(snippets) if snippets else default

    def _lower_initial(self, text: str) -> str:
        return text[:1].lower() + text[1:] if text else text

    def _salient_claims_by_run(
        self, claims: list[Claim], *, limit_per_run: int
    ) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        by_run: dict[str, list[Claim]] = {}
        for claim in claims:
            if claim.run_id:
                by_run.setdefault(claim.run_id, []).append(claim)
        for run_id, run_claims in by_run.items():
            out[run_id] = [
                c.text.rstrip(".") + "."
                for c in self._salient_claims(run_claims, limit=limit_per_run)
                if c.text
            ]
        return out

    def _salient_claims(
        self, claims: list[Claim], *, limit: int, one_per_run: bool = False
    ) -> list[Claim]:
        ranked = sorted(claims, key=self._claim_priority, reverse=True)
        chosen: list[Claim] = []
        seen_runs: set[str | None] = set()
        for claim in ranked:
            if self._is_low_signal_claim(claim):
                continue
            if one_per_run and claim.run_id in seen_runs:
                continue
            chosen.append(claim)
            seen_runs.add(claim.run_id)
            if len(chosen) >= limit:
                break
        if chosen:
            return chosen
        return ranked[:limit]

    def _claim_priority(self, claim: Claim) -> tuple[int, int]:
        text = (claim.text or "").lower()
        score = 0
        if claim.quantitative:
            score += 4
        if "variant" in text and "baseline" in text:
            score += 3
        if any(token in text for token in ("higher than baseline", "lower than baseline", "unchanged", "same", "worse", "improvement", "deterioration", "did not", "not supported")):
            score += 3
        if any(token in text for token in ("accuracy", "brier", "loss", "epoch", "wall-clock", "variance", "stability")):
            score += 2
        if self._is_low_signal_claim(claim):
            score -= 10
        return score, 1 if claim.quantitative else 0

    def _is_low_signal_claim(self, claim: Claim) -> bool:
        text = (claim.text or "").lower()
        return any(
            marker in text
            for marker in (
                "run completed successfully",
                "exit_code 0",
                "status succeeded",
                "status: succeeded",
            )
        )

    def _delta_pattern_sentence(
        self, run_analysis: dict[str, dict[str, object]]
    ) -> str:
        if not run_analysis:
            return "The draft currently lacks run-level analysis summaries."
        acc_positive = 0
        acc_negative = 0
        acc_flat = 0
        best_acc: float | None = None
        calib_tradeoff = 0
        for analysis in run_analysis.values():
            delta = analysis.get("delta") or {}
            acc = self._extract_delta(delta, ("accuracy", "test_accuracy"))
            brier = self._extract_delta(delta, ("brier", "brier_score"))
            if acc is None:
                continue
            if best_acc is None or acc > best_acc:
                best_acc = acc
            if acc > 0.005:
                acc_positive += 1
                if brier is not None and brier > 0.0:
                    calib_tradeoff += 1
            elif acc < -0.005:
                acc_negative += 1
            else:
                acc_flat += 1
        pieces: list[str] = []
        if best_acc is not None:
            pieces.append(
                f"The largest observed accuracy lift was about {best_acc:+.3f}."
            )
        if acc_positive or acc_negative or acc_flat:
            pieces.append(
                f"Accuracy improved meaningfully in {acc_positive} run(s), declined in {acc_negative}, and was roughly unchanged in {acc_flat}."
            )
        if calib_tradeoff:
            pieces.append(
                f"In {calib_tradeoff} of the accuracy-improving run(s), calibration worsened at the same time."
            )
        return " ".join(pieces)

    def _extract_delta(
        self, delta: object, metric_names: tuple[str, ...]
    ) -> float | None:
        if not isinstance(delta, dict):
            return None
        for key in metric_names:
            value = delta.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)
        return None

    async def _fill_missing_sections_with_provider(
        self,
        *,
        by_key: dict,
        project: StudentProject,
        claims: list[Claim],
        runs: list[ExperimentRun],
        has_mock: bool,
        run_analysis: dict,
        idea_context: dict,
        analysis_summary: dict,
        router,
        policy,
        settings,
        project_id: str,
        manuscript_id: str,
        draft_version: int,
    ):
        """Generate missing/placeholder sections one at a time with Pro.

        GPT-5 Pro/xhigh can consume an entire large single-shot output budget
        on hidden reasoning. Sectional generation keeps each call bounded while
        still using the same Pro/xhigh policy for the final manuscript text.
        """

        generated: dict[str, dict] = {}
        failed: dict[str, str] = {}
        first_result = None
        for key, title in _DEFAULT_SECTION_KEYS:
            existing = by_key.get(key) or {}
            existing_content = str(existing.get("content") or "").strip()
            if existing_content and not _is_placeholder_text(existing_content):
                generated[key] = {"source": "primary"}
                continue

            system = (
                "You are writing one section of an evidence-first technical "
                "research manuscript. Return JSON only. Do not expose raw claim "
                "ids or run ids in visible prose. Every numeric statement must be "
                "grounded in the provided claims and listed in claim_refs."
            )
            prompt = (
                f"Write only the `{key}` section titled `{title}` for the manuscript.\n"
                "The prose should be client-facing paper/report style, not an "
                "internal status update. Use multiple substantive paragraphs for "
                "method, results, discussion, and limitations. If the evidence is "
                "inconclusive, explain why and what targeted follow-up would resolve it.\n"
                + dump_json_block(
                    "Project context",
                    {
                        "title": project.title,
                        "research_direction": project.research_direction,
                        "constraints": project.constraints,
                        "exploration_strategy": project.exploration_strategy,
                        "target_venues": project.target_venues,
                        "has_mock_inputs": has_mock,
                    },
                )
                + dump_json_block(
                    "Evidence summary",
                    {
                        "analysis_summary": analysis_summary,
                        "run_analyses": run_analysis,
                        "idea_context": idea_context,
                    },
                )
                + dump_json_block(
                    "Claims available for claim_refs",
                    [
                        {
                            "id": c.id,
                            "text": c.text,
                            "kind": c.kind,
                            "value": c.value,
                            "quantitative": c.quantitative,
                            "evidence_refs": c.evidence_refs,
                        }
                        for c in claims
                    ],
                )
                + dump_json_block(
                    "Output schema",
                    {
                        "section": {
                            "key": key,
                            "title": title,
                            "content": "markdown prose for this section only",
                            "claim_refs": ["claim ids used by numeric/substantive claims"],
                            "evidence_refs": [{"type": "run", "id": "run id"}],
                        }
                    },
                )
            )
            req = CompletionRequest(
                system=system,
                prompt=prompt,
                temperature=0.2,
                max_tokens=min(int(policy.max_output_tokens or 16000), 20000),
                json_mode=True,
                task_kind=TaskKind.draft_generation.value,
                extra={"section_key": key, "sectional_generation": True},
            )
            req = apply_smoke_limits(apply_policy(req, policy), settings)
            try:
                resolved = router.resolve_with_policy(policy)
                result = await self._complete_with_retry(
                    resolved.adapter,
                    req,
                    phase_label=f"draft_section_{key}",
                    project_id=project_id,
                    reference=f"draft_section:{manuscript_id}:v{draft_version}:{key}",
                    meta={"section_key": key, "sectional_generation": True},
                )
                if first_result is None:
                    first_result = result
                parsed = safe_json_object(result.text)
                sec = parsed.get("section")
                if not isinstance(sec, dict):
                    rows = parsed.get("sections") or parsed.get("items") or []
                    sec = rows[0] if rows and isinstance(rows[0], dict) else {}
                content = str(sec.get("content") or "").strip()
                if not content or _is_placeholder_text(content):
                    raise ValueError("provider returned empty/placeholder section")
                by_key[key] = {
                    "key": key,
                    "title": sec.get("title") or title,
                    "content": content,
                    "claim_refs": [
                        r for r in (sec.get("claim_refs") or []) if isinstance(r, str)
                    ],
                    "evidence_refs": list(sec.get("evidence_refs") or []),
                }
                generated[key] = {
                    "source": "sectional_provider",
                    "text_len": len(content),
                    "model": getattr(result, "model", None),
                    "requested_reasoning_effort": getattr(
                        result, "requested_reasoning_effort", None
                    ),
                    "actual_reasoning_effort": getattr(
                        result, "actual_reasoning_effort", None
                    ),
                }
            except Exception as e:  # noqa: BLE001
                logger.warning("sectional draft generation failed for %s: %s", key, e)
                failed[key] = str(e)

        return by_key, {"generated": generated, "failed": failed}, first_result

    async def _complete_with_retry(
        self,
        adapter,
        req,
        *,
        phase_label: str,
        project_id: str,
        reference: str | None = None,
        meta: dict | None = None,
    ):
        attempts = 2
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                return await complete_with_ledger(
                    self.db,
                    project_id=project_id,
                    adapter=adapter,
                    req=req,
                    reference=reference,
                    meta={"phase_label": phase_label, **(meta or {})},
                )
            except Exception as e:  # noqa: BLE001
                last_error = e
                if attempt >= attempts:
                    break
                logger.warning(
                    "%s provider call failed on attempt %s/%s; retrying: %s",
                    phase_label,
                    attempt,
                    attempts,
                    e,
                )
                await asyncio.sleep(0.75)
        assert last_error is not None
        raise last_error

    async def _complete_with_provider_fallback(
        self,
        *,
        router,
        primary_policy,
        base_req: CompletionRequest,
        settings,
        phase_label: str,
        project_id: str,
        reference: str | None = None,
    ):
        primary_req = apply_smoke_limits(apply_policy(base_req, primary_policy), settings)
        primary_resolved = router.resolve_with_policy(primary_policy)
        try:
            result = await self._complete_with_retry(
                primary_resolved.adapter,
                primary_req,
                phase_label=phase_label,
                project_id=project_id,
                reference=reference,
                meta={"provider_role": "primary"},
            )
            return result, None
        except Exception as primary_error:  # noqa: BLE001
            fallback_policy = self._secondary_policy_for_draft_phase(primary_policy)
            if fallback_policy is None or fallback_policy.provider == primary_policy.provider:
                raise primary_error
            logger.warning(
                "%s primary provider failed; trying secondary provider %s/%s: %s",
                phase_label,
                fallback_policy.provider,
                fallback_policy.model,
                primary_error,
            )
            fallback_req = apply_smoke_limits(
                apply_policy(base_req, fallback_policy), settings
            )
            fallback_req = replace(fallback_req, max_tokens=primary_req.max_tokens)
            fallback_resolved = router.resolve_with_policy(fallback_policy)
            result = await self._complete_with_retry(
                fallback_resolved.adapter,
                fallback_req,
                phase_label=f"{phase_label}_secondary",
                project_id=project_id,
                reference=reference,
                meta={"provider_role": "secondary"},
            )
            return result, {
                "reason": str(primary_error),
                "primary": primary_policy.as_metadata(),
                "secondary": fallback_policy.as_metadata(),
            }

    def _secondary_policy_for_draft_phase(self, primary_policy):
        if primary_policy.phase not in {
            Phase.draft_generation.value,
            Phase.draft_polish.value,
        }:
            return None
        fallback = resolve_model_policy(Phase.manuscript_review)
        return replace(
            fallback,
            phase=primary_policy.phase,
            max_output_tokens=max(
                getattr(primary_policy, "max_output_tokens", 0),
                getattr(fallback, "max_output_tokens", 0),
            ),
            temperature=min(
                getattr(primary_policy, "temperature", 0.2),
                getattr(fallback, "temperature", 0.2),
            ),
        )

    # --- queries -----------------------------------------------------------

    def list(self, project_id: str) -> list[Manuscript]:
        return (
            self.db.query(Manuscript)
            .filter(Manuscript.project_id == project_id)
            .order_by(Manuscript.created_at.desc())
            .all()
        )

    def get_draft(self, draft_id: str) -> Draft:
        d = self.db.query(Draft).filter(Draft.id == draft_id).first()
        if d is None:
            raise LookupError(f"draft not found: {draft_id}")
        return d


def _is_placeholder_text(text: str | None) -> bool:
    if not text:
        return False
    lowered = text.strip().lower()
    return any(marker in lowered for marker in _PLACEHOLDER_MARKERS)
