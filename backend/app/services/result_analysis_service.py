"""Compare a run's metrics against its baseline and produce claim objects.

* Runs the result_analysis task kind against the router to produce a verdict.
* Creates Claim rows backed by evidence refs (metrics.json + run id).
* Updates the idea's funnel decision automatically when the verdict is
  decisive (promote on success, reject on terminal failure).
"""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.enums import AuditKind, IdeaDecision, RunResultClass, TaskKind
from app.core.models import Claim, ExperimentRun, FunnelDecision, Idea
from app.core.schemas.experiment import RunAnalysisOut
from app.config import Phase, resolve_model_policy
from app.providers.base import CompletionRequest, apply_policy, apply_smoke_limits
from app.providers.router import get_provider_router
from app.services._prompts import dump_json_block, load_prompt, safe_json_object
from app.services.audit_service import AuditService
from app.services.provider_call_ledger import complete_with_ledger
from app.utils import get_logger, new_id

logger = get_logger(__name__)


class ResultAnalysisService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.audit = AuditService(db)

    async def analyze(self, run_id: str, *, auto_update_funnel: bool = True) -> RunAnalysisOut:
        run = self.db.query(ExperimentRun).filter(ExperimentRun.id == run_id).first()
        if run is None:
            raise LookupError(f"run not found: {run_id}")

        metrics = run.metrics or {}
        baseline = metrics.get("baseline") or {}
        variant = metrics.get("variant") or {}
        if not metrics:
            metrics_path = Path(run.workspace_path) / "outputs" / "metrics.json"
            if metrics_path.exists():
                try:
                    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
                    run.metrics = metrics
                except Exception:
                    metrics = {}

        policy = resolve_model_policy(Phase.result_analysis)
        router = get_provider_router(self.db)
        resolved = router.resolve_with_policy(policy)
        prompt = (
            (load_prompt("result_analysis.md") or "")
            + dump_json_block("metrics", metrics)
            + dump_json_block(
                "run_meta",
                {
                    "status": run.status,
                    "result_class": run.result_class,
                    "exit_code": run.exit_code,
                    "mock": run.mock,
                },
            )
            + dump_json_block(
                "Output schema",
                {
                    "verdict": "promising|inconclusive|rejected",
                    "delta": {"accuracy": 0.0},
                    "claims": [
                        {"text": "string", "kind": "quantitative|qualitative", "value": "0.123", "quantitative": True}
                    ],
                },
            )
        )
        system = (
            "You are a rigorous empirical reviewer. Do not fabricate numbers. "
            "Respond with JSON only. Each claim must have its numeric `value` "
            "sourced from the metrics blob; do not invent numbers."
        )
        try:
            req = CompletionRequest(
                system=system,
                prompt=prompt,
                temperature=0.2,
                max_tokens=1200,
                json_mode=True,
                task_kind=TaskKind.result_analysis.value,
                extra={"metrics": metrics, "baseline": baseline},
            )
            req = apply_policy(req, policy)
            req = apply_smoke_limits(req, get_settings())
            result = await complete_with_ledger(
                self.db,
                project_id=run.project_id,
                adapter=resolved.adapter,
                req=req,
                reference=f"result_analysis:{run.id}",
                meta={"run_id": run.id, "idea_id": run.idea_id},
            )
            parsed = safe_json_object(result.text)
        except Exception as e:  # noqa: BLE001
            logger.warning("analysis provider call failed, falling back: %s", e)
            parsed = self._fallback_analysis(metrics, baseline, variant)
            result = None  # type: ignore

        verdict = parsed.get("verdict") or "inconclusive"
        delta = parsed.get("delta") or self._delta(baseline, variant)
        claim_rows = parsed.get("claims") or []

        saved_claim_ids: list[str] = []
        for c in claim_rows:
            text = (c.get("text") or "").strip()
            if not text:
                continue
            evidence_refs = [{"type": "run", "id": run.id, "artifact": "metrics.json"}]
            claim = Claim(
                id=new_id("clm"),
                project_id=run.project_id,
                idea_id=run.idea_id,
                run_id=run.id,
                text=text[:2000],
                kind=(c.get("kind") or "quantitative").strip(),
                value=str(c.get("value")) if c.get("value") is not None else None,
                quantitative=bool(c.get("quantitative")),
                evidence_refs=evidence_refs,
                mock=run.mock,
            )
            self.db.add(claim)
            saved_claim_ids.append(claim.id)

        promoted = False
        if auto_update_funnel:
            idea = self.db.query(Idea).filter(Idea.id == run.idea_id).first()
            if idea is not None:
                if (
                    run.result_class == RunResultClass.succeeded_valid.value
                    and verdict == "promising"
                ):
                    previous = idea.stage
                    next_stage = self._next_stage(previous)
                    if next_stage and idea.stage != next_stage:
                        idea.stage = next_stage
                        idea.decision = IdeaDecision.promote.value
                        self.db.add(
                            FunnelDecision(
                                id=new_id("fd"),
                                idea_id=idea.id,
                                from_stage=previous,
                                to_stage=next_stage,
                                decision=IdeaDecision.promote.value,
                                reason=f"auto-promoted from result analysis of run {run.id}",
                                decided_by="system",
                            )
                        )
                        promoted = True
                elif run.result_class in (
                    RunResultClass.failed_terminal.value,
                    RunResultClass.succeeded_invalid.value,
                ) and verdict == "rejected":
                    idea.decision = IdeaDecision.reject.value
                    self.db.add(
                        FunnelDecision(
                            id=new_id("fd"),
                            idea_id=idea.id,
                            from_stage=idea.stage,
                            to_stage=idea.stage,
                            decision=IdeaDecision.reject.value,
                            reason=f"auto-rejected from result analysis of run {run.id}",
                            decided_by="system",
                        )
                    )

        self.audit.log(
            project_id=run.project_id,
            kind=AuditKind.result_validated,
            message=f"Analyzed run {run.id}: verdict={verdict}",
            subject_kind="run",
            subject_id=run.id,
            payload={
                "verdict": verdict,
                "delta": delta,
                "claim_ids": saved_claim_ids,
                "promoted": promoted,
                "mock": run.mock,
                "policy": policy.as_metadata(),
            },
        )
        self.db.commit()

        return RunAnalysisOut(
            run_id=run.id,
            result_class=run.result_class or RunResultClass.succeeded_invalid.value,
            verdict=verdict,
            metrics=metrics or {},
            baseline_delta=delta or {},
            promoted_idea=promoted,
            claim_ids=saved_claim_ids,
        )

    @staticmethod
    def _delta(baseline: dict, variant: dict) -> dict:
        out: dict[str, float] = {}
        for k in set(baseline) | set(variant):
            try:
                out[k] = round(float(variant.get(k, 0)) - float(baseline.get(k, 0)), 4)
            except Exception:  # noqa: BLE001
                continue
        return out

    def _fallback_analysis(self, metrics: dict, baseline: dict, variant: dict) -> dict:
        delta = self._delta(baseline, variant)
        verdict = "inconclusive"
        if delta:
            best = max(delta.items(), key=lambda kv: kv[1])
            if best[1] > 0.005:
                verdict = "promising"
            elif best[1] < -0.005:
                verdict = "rejected"
        claims: list[dict] = []
        for k, v in delta.items():
            claims.append(
                {
                    "text": f"Variant changes {k} by {v:+.4f} vs baseline on the local toy task.",
                    "kind": "quantitative",
                    "value": f"{v:+.4f}",
                    "quantitative": True,
                }
            )
        return {"verdict": verdict, "delta": delta, "claims": claims}

    @staticmethod
    def _next_stage(stage: str) -> str | None:
        order = ["S0", "S1", "S2", "S3", "S4"]
        try:
            i = order.index(stage)
            return order[min(i + 1, len(order) - 1)]
        except ValueError:
            return None
