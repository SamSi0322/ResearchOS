"""Idea generation + normalization + deduplication.

The actual LLM call goes through the router to whichever provider is
configured (including mock). Output is expected to be a JSON object of the
shape ``{"ideas": [{title, summary, hypothesis, novelty_claim, target_metric,
cluster_tag}, ...]}``. We tolerate the model returning partial objects and
fill in reasonable defaults.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from sqlalchemy.orm import Session

from app.config import Phase, get_settings, resolve_model_policy
from app.core.enums import AuditKind, IdeaDecision, IdeaStage, TaskKind
from app.core.models import Idea, StudentProject
from app.core.schemas import IdeaGenerateIn
from app.providers.base import CompletionRequest, apply_policy, apply_smoke_limits
from app.providers.router import get_provider_router
from app.services._prompts import (
    dump_json_block,
    load_prompt,
    safe_json_object,
    salvage_object_list,
)
from app.services.audit_service import AuditService
from app.services.context_bundle_service import load_bundle_context
from app.services.provider_call_ledger import complete_with_ledger
from app.utils import get_logger, new_id

logger = get_logger(__name__)


def _slug(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-zA-Z0-9]+", " ", s).strip().lower()
    return re.sub(r"\s+", " ", s)


_IDEA_LIST_KEYS = ("ideas", "items", "research_ideas", "candidates", "proposals")


def _extract_idea_rows(text: str) -> list[dict[str, Any]]:
    parsed = safe_json_object(text)
    for key in _IDEA_LIST_KEYS:
        rows = parsed.get(key)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return salvage_object_list(text, *_IDEA_LIST_KEYS)


class IdeaGenerationService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.audit = AuditService(db)

    async def generate(self, project_id: str, payload: IdeaGenerateIn) -> list[Idea]:
        project = self._project(project_id)
        settings = get_settings()

        # Smoke mode clamps the number of ideas we will pay for.
        requested = int(payload.count or 1)
        if settings.smoke_mode:
            requested = min(requested, int(settings.max_ideas_per_run))

        policy = resolve_model_policy(Phase.idea_generation)
        router = get_provider_router(self.db)
        resolved = router.resolve_with_policy(policy)

        system = (
            "You are an internal research ideation assistant for a mentor team. "
            "Generate diverse, concrete, falsifiable ideas that can be piloted on "
            "one or two GPUs in under 48 hours. Return exactly the requested number "
            "of ideas as JSON only; do not return extras."
        )
        header = load_prompt("idea_generation.md") or ""
        # Background material supplied by the operator at intake. We keep the
        # digest small (settings.context_bundle_snippet_char_limit governs it)
        # so it fits even when smoke-mode prompt budgets are active.
        bundle_budget = min(
            6000,
            int(getattr(settings, "context_bundle_snippet_char_limit", 3000)) * 2,
        )
        bundle_ctx = load_bundle_context(
            self.db, project_id=project.id, char_budget=bundle_budget
        )
        bundle_block = ""
        if bundle_ctx.get("snippets"):
            bundle_block = dump_json_block(
                "Background material uploaded at project intake (use as grounding; do not quote verbatim)",
                {
                    "files": [e.get("path") for e in bundle_ctx.get("files", [])[:40]],
                    "snippets": bundle_ctx["snippets"],
                },
            )
        prompt = (
            f"{header}\n"
            f"Project title: {project.title}\n"
            f"Research direction:\n{project.research_direction}\n"
            f"Constraints: {project.constraints or 'none'}\n"
            f"Target venues: {', '.join(project.target_venues or []) or 'n/a'}\n"
            f"Exploration strategy: {project.exploration_strategy or 'breadth-first'}\n"
            f"Count: exactly {requested}\n"
            f"Extra context: {payload.extra_context or '(none)'}\n"
            f"{bundle_block}"
            + dump_json_block(
                "Output schema",
                {
                    "ideas": [
                        {
                            "title": "short phrase",
                            "summary": "1-2 sentences",
                            "hypothesis": "what we expect to find",
                            "novelty_claim": "what is new",
                            "target_metric": "single measurable metric",
                            "cluster_tag": "broad family e.g. regularisation | tokenization",
                        }
                    ]
                },
            )
            + (
                f"\nRules: return exactly {requested} idea object(s), no more and no less. "
                "Keep each field concise and return only valid JSON with the schema above."
            )
        )

        req = CompletionRequest(
            system=system,
            prompt=prompt,
            temperature=0.9,
            max_tokens=3500,
            json_mode=True,
            task_kind=TaskKind.idea_generation.value,
            extra={"count": requested},
        )
        req = apply_policy(req, policy)
        req = apply_smoke_limits(req, settings)
        result = await complete_with_ledger(
            self.db,
            project_id=project_id,
            adapter=resolved.adapter,
            req=req,
            reference=f"idea_generation:{project_id}",
            meta={"requested_count": requested},
        )
        raw_ideas = _extract_idea_rows(result.text)

        logger.info(
            "idea_generation received",
            extra={
                "project_id": project_id,
                "provider": result.provider,
                "model": result.model,
                "count": len(raw_ideas),
                "mock": result.mock,
            },
        )
        if not raw_ideas and (result.raw or {}).get("stop_reason") == "max_tokens":
            logger.warning(
                "idea_generation provider output truncated before valid JSON closed",
                extra={"project_id": project_id, "provider": result.provider, "model": result.model},
            )

        seen: set[str] = set()
        saved: list[Idea] = []
        for i, raw in enumerate(raw_ideas):
            if len(saved) >= requested:
                break
            title = (raw.get("title") or f"Candidate #{i + 1}").strip()
            summary = (raw.get("summary") or "").strip()
            key = _slug(f"{title}|{summary}")[:160]
            if key in seen:
                continue
            seen.add(key)

            idea = Idea(
                id=new_id("idea"),
                project_id=project.id,
                title=title[:255],
                summary=summary or title,
                hypothesis=(raw.get("hypothesis") or "").strip() or None,
                novelty_claim=(raw.get("novelty_claim") or "").strip() or None,
                target_metric=(raw.get("target_metric") or "").strip() or None,
                cluster_tag=(raw.get("cluster_tag") or "").strip() or None,
                stage=IdeaStage.S0.value,
                decision=IdeaDecision.pending.value,
                meta={
                    "provider": result.provider,
                    "model": result.model,
                    "mock": result.mock,
                    "raw_index": i,
                    "phase": policy.phase,
                    "policy": policy.policy_label,
                    "reasoning_effort": policy.reasoning_effort,
                    "thinking_mode": policy.thinking_mode,
                },
            )
            self.db.add(idea)
            saved.append(idea)

        self.db.flush()

        self.audit.log(
            project_id=project.id,
            kind=AuditKind.ideas_generated,
            message=f"Generated {len(saved)} ideas",
            subject_kind="project",
            subject_id=project.id,
            payload={
                "count": len(saved),
                "provider": result.provider,
                "model": result.model,
                "mock": result.mock,
                "policy": policy.as_metadata(),
            },
        )
        self.db.commit()
        return saved

    def list(self, project_id: str, *, stage: str | None = None) -> list[Idea]:
        q = self.db.query(Idea).filter(Idea.project_id == project_id)
        if stage:
            q = q.filter(Idea.stage == stage)
        return q.order_by(Idea.created_at.asc()).all()

    def get(self, idea_id: str) -> Idea:
        idea = self.db.query(Idea).filter(Idea.id == idea_id).first()
        if idea is None:
            raise LookupError(f"idea not found: {idea_id}")
        return idea

    def _project(self, project_id: str) -> StudentProject:
        p = self.db.query(StudentProject).filter(StudentProject.id == project_id).first()
        if p is None:
            raise LookupError(f"project not found: {project_id}")
        return p
