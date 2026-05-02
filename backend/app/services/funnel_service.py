"""Funnel stage logic.

Supports:

* scoring ideas via the structured_screening task kind (LLM)
* bulk decisions (keep / reject / promote)
* stage advancement with configurable target counts
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.config import Phase, get_settings, resolve_model_policy
from app.core.enums import AuditKind, FunnelStage, IdeaDecision, IdeaStage, TaskKind
from app.core.models import FunnelDecision, Idea, Scorecard
from app.core.schemas import FunnelAdvanceIn, IdeaDecisionIn
from app.providers.base import CompletionRequest, apply_policy, apply_smoke_limits
from app.providers.router import get_provider_router
from app.services._prompts import dump_json_block, load_prompt, safe_json_object
from app.services.audit_service import AuditService
from app.services.provider_call_ledger import complete_with_ledger
from app.utils import get_logger, new_id

logger = get_logger(__name__)

_STAGE_ORDER = [FunnelStage.S0, FunnelStage.S1, FunnelStage.S2, FunnelStage.S3, FunnelStage.S4]
_DEFAULT_TARGETS = {
    FunnelStage.S0: 50,
    FunnelStage.S1: 20,
    FunnelStage.S2: 10,
    FunnelStage.S3: 5,
    FunnelStage.S4: 1,
}


class FunnelService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.audit = AuditService(db)

    # --- scoring -----------------------------------------------------------

    async def score(self, project_id: str, stage: str) -> list[Scorecard]:
        ideas = (
            self.db.query(Idea)
            .filter(Idea.project_id == project_id, Idea.stage == stage)
            .all()
        )
        if not ideas:
            return []

        # Structured screening is the "idea_ranking" phase in our policy
        # table - the hardest evaluator (gpt-5.4-pro / xhigh) in production.
        policy = resolve_model_policy(Phase.idea_ranking)
        router = get_provider_router(self.db)
        resolved = router.resolve_with_policy(policy)
        system = (
            "You are a strict but fair research reviewer. Rate each idea on four "
            "dimensions (novelty, feasibility, rigor, impact) each from 1.0 to 5.0. "
            "Respond with JSON only. Be consistent across ideas."
        )
        items = [
            {
                "id": i.id,
                "title": i.title,
                "summary": i.summary,
                "hypothesis": i.hypothesis,
                "novelty_claim": i.novelty_claim,
                "target_metric": i.target_metric,
            }
            for i in ideas
        ]
        prompt = (
            (load_prompt("structured_screening.md") or "")
            + f"\nStage: {stage}\n"
            + dump_json_block("Ideas to score", items)
            + dump_json_block(
                "Output schema",
                {
                    "scorecards": [
                        {
                            "id": "idea id",
                            "novelty": 3.5,
                            "feasibility": 3.0,
                            "rigor": 4.0,
                            "impact": 3.0,
                            "rationale": "short reason",
                        }
                    ]
                },
            )
        )
        req = CompletionRequest(
            system=system,
            prompt=prompt,
            temperature=0.2,
            max_tokens=3500,
            json_mode=True,
            task_kind=TaskKind.structured_screening.value,
            extra={"items": items},
        )
        req = apply_policy(req, policy)
        req = apply_smoke_limits(req, get_settings())
        result = await complete_with_ledger(
            self.db,
            project_id=project_id,
            adapter=resolved.adapter,
            req=req,
            reference=f"idea_ranking:{project_id}:{stage}",
            meta={"stage": stage, "item_count": len(items)},
        )
        parsed = safe_json_object(result.text)
        rows = parsed.get("scorecards") or parsed.get("items") or []

        by_id = {i.id: i for i in ideas}
        saved: list[Scorecard] = []
        for row in rows:
            idea = by_id.get(row.get("id"))
            if idea is None:
                continue
            novelty = float(row.get("novelty", 0) or 0)
            feasibility = float(row.get("feasibility", 0) or 0)
            rigor = float(row.get("rigor", 0) or 0)
            impact = float(row.get("impact", 0) or 0)
            overall = round((novelty + feasibility + rigor + impact) / 4.0, 3)
            sc = Scorecard(
                id=new_id("sc"),
                idea_id=idea.id,
                stage=stage,
                novelty=novelty,
                feasibility=feasibility,
                rigor=rigor,
                impact=impact,
                overall=overall,
                rubric={
                    "rationale": row.get("rationale", ""),
                    "provider": result.provider,
                    "model": result.model,
                    "mock": result.mock,
                    "policy": policy.as_metadata(),
                },
            )
            self.db.add(sc)
            idea.score = overall
            idea.rationale = row.get("rationale") or idea.rationale
            saved.append(sc)

        self.db.flush()
        self.audit.log(
            project_id=project_id,
            kind=AuditKind.funnel_advanced,
            message=f"Scored {len(saved)} ideas at {stage}",
            subject_kind="project",
            subject_id=project_id,
            payload={"count": len(saved), "stage": stage, "mock": result.mock},
        )
        self.db.commit()
        return saved

    # --- decisions --------------------------------------------------------

    def apply_decision(self, idea_id: str, payload: IdeaDecisionIn) -> Idea:
        idea = self.db.query(Idea).filter(Idea.id == idea_id).first()
        if idea is None:
            raise LookupError(f"idea not found: {idea_id}")

        previous_stage = idea.stage
        previous_decision = idea.decision

        new_decision = IdeaDecision(payload.decision).value
        idea.decision = new_decision
        if payload.rationale:
            idea.rationale = payload.rationale
        to_stage = previous_stage
        if new_decision == IdeaDecision.promote.value and payload.promote_to_stage:
            to_stage = payload.promote_to_stage
            idea.stage = to_stage

        self.db.add(
            FunnelDecision(
                id=new_id("fd"),
                idea_id=idea.id,
                from_stage=previous_stage,
                to_stage=to_stage,
                decision=new_decision,
                reason=payload.rationale,
                decided_by="operator",
            )
        )
        self.audit.log(
            project_id=idea.project_id,
            kind=AuditKind.idea_decision,
            message=f"{previous_decision}->{new_decision} on {idea.title[:40]}",
            subject_kind="idea",
            subject_id=idea.id,
            actor="operator",
        )
        self.db.commit()
        return idea

    def advance(self, project_id: str, payload: FunnelAdvanceIn) -> dict:
        from_stage = FunnelStage(payload.from_stage)
        to_stage = FunnelStage(payload.to_stage)
        target = payload.keep_count or _DEFAULT_TARGETS.get(to_stage)

        q = (
            self.db.query(Idea)
            .filter(
                Idea.project_id == project_id,
                Idea.stage == from_stage.value,
                Idea.decision != IdeaDecision.reject.value,
            )
            .order_by(Idea.score.desc().nullslast(), Idea.created_at.asc())
        )
        candidates = q.all()
        promoted = candidates[:target] if target is not None else candidates
        rejected = candidates[target:] if payload.auto_reject and target is not None else []

        for idea in promoted:
            self.db.add(
                FunnelDecision(
                    id=new_id("fd"),
                    idea_id=idea.id,
                    from_stage=from_stage.value,
                    to_stage=to_stage.value,
                    decision=IdeaDecision.promote.value,
                    reason=payload.rationale,
                    decided_by="operator",
                )
            )
            idea.stage = to_stage.value
            idea.decision = IdeaDecision.promote.value

        for idea in rejected:
            self.db.add(
                FunnelDecision(
                    id=new_id("fd"),
                    idea_id=idea.id,
                    from_stage=from_stage.value,
                    to_stage=from_stage.value,
                    decision=IdeaDecision.reject.value,
                    reason="Did not make target cut",
                    decided_by="system",
                )
            )
            idea.decision = IdeaDecision.reject.value

        self.audit.log(
            project_id=project_id,
            kind=AuditKind.funnel_advanced,
            message=(
                f"Advanced {len(promoted)} ideas from {from_stage.value} to {to_stage.value}; "
                f"auto-rejected {len(rejected)}"
            ),
            subject_kind="project",
            subject_id=project_id,
            payload={
                "promoted": [i.id for i in promoted],
                "rejected": [i.id for i in rejected],
                "to_stage": to_stage.value,
            },
        )
        self.db.commit()
        return {
            "promoted": [i.id for i in promoted],
            "rejected": [i.id for i in rejected],
            "to_stage": to_stage.value,
        }

    def stage_summary(self, project_id: str) -> dict:
        by_stage: dict[str, int] = {}
        for st in _STAGE_ORDER:
            by_stage[st.value] = (
                self.db.query(Idea)
                .filter(Idea.project_id == project_id, Idea.stage == st.value)
                .count()
            )
        return {"by_stage": by_stage, "targets": {s.value: t for s, t in _DEFAULT_TARGETS.items()}}
