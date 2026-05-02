from __future__ import annotations

from sqlalchemy.orm import Session

from app.config import Phase, get_settings, resolve_model_policy
from app.core.enums import AuditKind, TaskKind
from app.core.models import ExperimentSpec, Idea
from app.core.schemas import SpecGenerateIn
from app.providers.base import CompletionRequest, apply_policy, apply_smoke_limits
from app.providers.router import get_provider_router
from app.services._prompts import dump_json_block, load_prompt, safe_json_object
from app.services.audit_service import AuditService
from app.services.provider_call_ledger import complete_with_ledger
from app.utils import get_logger, new_id

logger = get_logger(__name__)


class SpecService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.audit = AuditService(db)

    async def generate(self, project_id: str, payload: SpecGenerateIn) -> ExperimentSpec:
        idea = self.db.query(Idea).filter(Idea.id == payload.idea_id).first()
        if idea is None:
            raise LookupError(f"idea not found: {payload.idea_id}")
        if idea.project_id != project_id:
            raise ValueError("idea does not belong to this project")

        policy = resolve_model_policy(Phase.spec_generation)
        router = get_provider_router(self.db)
        resolved = router.resolve_with_policy(policy)
        system = (
            "You are a senior research engineer drafting a concrete, minimal, "
            "falsifiable experiment specification. Answer with JSON only."
        )
        body = (
            (load_prompt("spec_generation.md") or "")
            + dump_json_block(
                "Idea",
                {
                    "title": idea.title,
                    "summary": idea.summary,
                    "hypothesis": idea.hypothesis,
                    "novelty_claim": idea.novelty_claim,
                    "target_metric": idea.target_metric,
                },
            )
            + f"\nExtra instructions: {payload.extra_instructions or '(none)'}\n"
            + dump_json_block(
                "Output schema",
                {
                    "hypothesis": "testable statement",
                    "problem_framing": "short paragraph",
                    "target_metrics": ["accuracy", "ece"],
                    "dataset_assumptions": "what dataset and why",
                    "baseline": "exact baseline description",
                    "experiment_plan": "step by step",
                    "constraints": "time, compute, data",
                    "success_criteria": ["bullet"],
                    "stop_criteria": ["bullet"],
                    "budget_estimate_usd": 0.0,
                },
            )
        )
        req = CompletionRequest(
            system=system,
            prompt=body,
            temperature=0.3,
            max_tokens=2200,
            json_mode=True,
            task_kind=TaskKind.spec_generation.value,
            extra={"idea_title": idea.title},
        )
        req = apply_policy(req, policy)
        req = apply_smoke_limits(req, get_settings())
        result = await complete_with_ledger(
            self.db,
            project_id=project_id,
            adapter=resolved.adapter,
            req=req,
            reference=f"spec_generation:{idea.id}",
            meta={"idea_id": idea.id},
        )
        parsed = safe_json_object(result.text)

        prior = (
            self.db.query(ExperimentSpec)
            .filter(ExperimentSpec.idea_id == idea.id)
            .order_by(ExperimentSpec.version.desc())
            .first()
        )
        next_version = (prior.version + 1) if prior else 1

        def _as_text(v, default: str = "") -> str:
            # Providers sometimes return a list of steps where the schema
            # asks for a paragraph. Coerce any iterable into a newline-
            # separated prose block; scalars become ``str()``.
            if v is None:
                return default
            if isinstance(v, str):
                return v
            if isinstance(v, (list, tuple)):
                return "\n".join(str(x) for x in v)
            if isinstance(v, dict):
                import json

                return json.dumps(v, default=str)
            return str(v)

        spec = ExperimentSpec(
            id=new_id("spec"),
            project_id=project_id,
            idea_id=idea.id,
            version=next_version,
            hypothesis=_as_text(parsed.get("hypothesis") or idea.hypothesis or idea.title)[:2000],
            problem_framing=_as_text(parsed.get("problem_framing") or idea.summary)[:3000],
            target_metrics=parsed.get("target_metrics") or [idea.target_metric or "accuracy"],
            dataset_assumptions=_as_text(parsed.get("dataset_assumptions")),
            baseline=_as_text(parsed.get("baseline")),
            experiment_plan=_as_text(parsed.get("experiment_plan")),
            constraints=_as_text(parsed.get("constraints")),
            success_criteria=parsed.get("success_criteria") or [],
            stop_criteria=parsed.get("stop_criteria") or [],
            budget_estimate_usd=float(parsed.get("budget_estimate_usd", 0) or 0),
            meta={
                "provider": result.provider,
                "model": result.model,
                "mock": result.mock,
                "policy": policy.as_metadata(),
            },
        )
        self.db.add(spec)
        self.audit.log(
            project_id=project_id,
            kind=AuditKind.spec_generated,
            message=f"Spec v{next_version} for {idea.title[:40]}",
            subject_kind="spec",
            subject_id=spec.id,
            payload={"idea_id": idea.id, "mock": result.mock},
        )
        self.db.commit()
        return spec

    def list(self, project_id: str, *, idea_id: str | None = None) -> list[ExperimentSpec]:
        q = self.db.query(ExperimentSpec).filter(ExperimentSpec.project_id == project_id)
        if idea_id:
            q = q.filter(ExperimentSpec.idea_id == idea_id)
        return q.order_by(ExperimentSpec.created_at.desc()).all()

    def get(self, spec_id: str) -> ExperimentSpec:
        spec = self.db.query(ExperimentSpec).filter(ExperimentSpec.id == spec_id).first()
        if spec is None:
            raise LookupError(f"spec not found: {spec_id}")
        return spec
