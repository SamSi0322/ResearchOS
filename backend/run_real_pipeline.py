"""One-shot real-run orchestrator for ResearchOS.

Not a generic service — this is the operator script that:

  1. Probes OpenAI + Anthropic for the strongest working model on the user's
     keys (future-dated ids are expected to 404; we fall through the list).
  2. Overrides the production model policy with the probed ids, disables
     the alias layer, and caps per-call output tokens.
  3. Installs a process-wide cost guard that adds up every CompletionResult
     and raises ``BudgetExceeded`` before the next call if we'd exceed the
     $30 cap.
  4. Executes a reduced funnel (20 -> 8 -> 3) with real experiments, real
     claims, a real draft, and a package when review blockers allow freeze.

Outputs a compact report with the models selected, total cost, funnel IDs,
and package status. Exit code 0 on a completed research cycle, even when
packaging is intentionally blocked by open review issues; non-zero on budget
exhaustion or unrecovered pipeline failure.

Run:
    cd backend
    .venv/Scripts/python.exe run_real_pipeline.py

Env overrides honoured (all optional):
    RESEARCHOS_REAL_RUN_BUDGET_USD      default 30.0
    RESEARCHOS_REAL_RUN_MAX_TOKENS      default 4096
    RESEARCHOS_REAL_RUN_IDEAS           default 20
    RESEARCHOS_REAL_RUN_TOP_EXP         default 8
    RESEARCHOS_REAL_RUN_TOP_FINAL       default 3
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Env overrides MUST be set before any `app.*` import. Anything that pulls
# `app.config.get_settings` caches the settings instance, and per-phase
# policy overrides are read at call time but run_mode is not.
# ---------------------------------------------------------------------------
import os as _os

_os.environ.setdefault("RESEARCHOS_RUN_MODE", "production")
_os.environ.setdefault("RESEARCHOS_SMOKE_MODE", "false")
_os.environ.setdefault("RESEARCHOS_DISABLE_ALIAS", "true")

import asyncio
import json
import sys
import time
import traceback
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import httpx  # noqa: E402

BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


def log(msg: str) -> None:
    print(f"[real] {datetime.utcnow():%H:%M:%S} {msg}", flush=True)


# ---------------------------------------------------------------------------
# 1. Probe providers for working models
# ---------------------------------------------------------------------------

# Ordered strongest -> weakest. Future-dated ids appear first; if they 404
# we fall through to widely-deployed ones.
OPENAI_CANDIDATES = [
    "gpt-5.2",
    "gpt-5.1",
    "gpt-5",
    "gpt-4.1",
    "gpt-5-mini",
    "gpt-4.1-mini",
    "gpt-4o",
]
ANTHROPIC_CANDIDATES = [
    "claude-3-7-opus",
    "claude-opus-4-5",
    "claude-opus-4-1",
    "claude-sonnet-4-5",
    "claude-3-5-sonnet-20240620",
    "claude-3-opus-20240229",
    "claude-haiku-4-5",
]


async def _probe_openai(api_key: str, candidates: Iterable[str]) -> tuple[str | None, list[dict]]:
    """Try each candidate with an 8-token ping; return the first that works."""
    from app.providers.base import CompletionRequest
    from app.providers.openai_adapter import OpenAIProvider

    trace: list[dict] = []
    for model in candidates:
        adapter = OpenAIProvider(api_key, model=model, timeout=30.0)
        try:
            res = await adapter.complete(
                CompletionRequest(prompt="Respond with OK.", max_tokens=32)
            )
            trace.append(
                {
                    "model": model,
                    "ok": True,
                    "latency_ms": res.latency_ms,
                    "text": (res.text or "")[:40],
                }
            )
            return model, trace
        except Exception as e:  # noqa: BLE001
            status = getattr(e, "status", None)
            msg = str(e)[:140]
            trace.append({"model": model, "ok": False, "status": status, "msg": msg})
            # 401/403 is a key-level failure; no point trying more models.
            if status in (401, 403):
                break
    return None, trace


async def _probe_anthropic(api_key: str, candidates: Iterable[str]) -> tuple[str | None, list[dict]]:
    from app.providers.anthropic_adapter import AnthropicProvider
    from app.providers.base import CompletionRequest

    trace: list[dict] = []
    for model in candidates:
        adapter = AnthropicProvider(api_key, model=model, timeout=30.0)
        try:
            res = await adapter.complete(
                CompletionRequest(prompt="Respond with OK.", max_tokens=32)
            )
            trace.append(
                {
                    "model": model,
                    "ok": True,
                    "latency_ms": res.latency_ms,
                    "text": (res.text or "")[:40],
                }
            )
            return model, trace
        except Exception as e:  # noqa: BLE001
            status = getattr(e, "status", None)
            msg = str(e)[:140]
            trace.append({"model": model, "ok": False, "status": status, "msg": msg})
            if status in (401, 403):
                break
    return None, trace


async def probe_available_models() -> dict:
    """Probe both providers using keys from API_KEYS.txt / env. Return the
    strongest working model per provider plus full probe trace."""
    from app.config import get_settings
    from app.config.api_keys_file import resolve_api_keys_file

    settings = get_settings()

    # Resolve keys: env wins, then API_KEYS.txt.
    openai_key = _os.environ.get("OPENAI_API_KEY", "").strip() or None
    anthropic_key = _os.environ.get("ANTHROPIC_API_KEY", "").strip() or None
    if not (openai_key and anthropic_key):
        parsed = resolve_api_keys_file(
            override=settings.api_keys_file_override,
            candidates=list(settings.api_keys_file_candidates),
        )
        pairs = getattr(parsed, "pairs", {}) or {}
        openai_key = openai_key or pairs.get("OPENAI_API_KEY")
        anthropic_key = anthropic_key or pairs.get("ANTHROPIC_API_KEY")

    if not openai_key or not anthropic_key:
        raise SystemExit(
            "Missing provider keys. Set OPENAI_API_KEY / ANTHROPIC_API_KEY or "
            "add them to API_KEYS.txt."
        )

    log("probing OpenAI…")
    openai_model, openai_trace = await _probe_openai(openai_key, OPENAI_CANDIDATES)
    log("probing Anthropic…")
    anthropic_model, anthropic_trace = await _probe_anthropic(anthropic_key, ANTHROPIC_CANDIDATES)

    return {
        "openai": openai_model,
        "anthropic": anthropic_model,
        "openai_trace": openai_trace,
        "anthropic_trace": anthropic_trace,
    }


# ---------------------------------------------------------------------------
# 2. Runtime policy overrides
# ---------------------------------------------------------------------------


def apply_runtime_policy_overrides(openai_model: str, anthropic_model: str) -> None:
    """Set per-phase env overrides so every phase uses a verified model.

    ``_production_table`` reads these each call, so no cache reset is needed
    for this particular knob — but we flush the settings cache anyway to pick
    up RESEARCHOS_RUN_MODE / RESEARCHOS_DISABLE_ALIAS reliably.
    """
    _os.environ["RESEARCHOS_MODEL_IDEA_GENERATION"] = anthropic_model
    _os.environ["RESEARCHOS_MODEL_IDEA_RANKING"] = openai_model
    _os.environ["RESEARCHOS_MODEL_SPEC_GENERATION"] = anthropic_model
    _os.environ["RESEARCHOS_MODEL_CODE_GENERATION"] = openai_model
    _os.environ["RESEARCHOS_MODEL_CODE_REVIEW"] = openai_model
    _os.environ["RESEARCHOS_MODEL_RESULT_ANALYSIS"] = openai_model
    _os.environ["RESEARCHOS_MODEL_DRAFT_GENERATION"] = anthropic_model
    _os.environ["RESEARCHOS_MODEL_DRAFT_POLISH"] = anthropic_model
    _os.environ["RESEARCHOS_MODEL_MANUSCRIPT_REVIEW"] = openai_model
    # Reasoning effort per spec.
    _os.environ["RESEARCHOS_REASONING_IDEA_RANKING"] = "high"
    _os.environ["RESEARCHOS_REASONING_CODE_GENERATION"] = "low"
    _os.environ["RESEARCHOS_REASONING_CODE_REVIEW"] = "high"
    _os.environ["RESEARCHOS_REASONING_RESULT_ANALYSIS"] = "medium"
    _os.environ["RESEARCHOS_REASONING_MANUSCRIPT_REVIEW"] = "high"

    from app.config import reset_settings_cache

    reset_settings_cache()


# ---------------------------------------------------------------------------
# 3. Budget guard + per-call token cap
# ---------------------------------------------------------------------------


class BudgetExceeded(RuntimeError):
    pass


class BudgetGuard:
    def __init__(self, cap_usd: float, max_tokens: int) -> None:
        self.cap_usd = float(cap_usd)
        self.max_tokens = int(max_tokens)
        self.used_usd = 0.0
        self.by_phase: dict[str, float] = {}
        self.calls: list[dict] = []

    def precheck(self, phase_label: str) -> None:
        if self.used_usd >= self.cap_usd:
            raise BudgetExceeded(
                f"budget_exceeded before {phase_label}: "
                f"used ${self.used_usd:.4f} of ${self.cap_usd:.2f}"
            )

    def record(self, *, phase: str, cost: float, model: str) -> None:
        self.used_usd += float(cost)
        self.by_phase[phase] = round(self.by_phase.get(phase, 0.0) + float(cost), 6)
        self.calls.append(
            {"phase": phase, "cost": round(cost, 6), "model": model, "ts": time.time()}
        )

    def summary(self) -> dict:
        return {
            "cap_usd": self.cap_usd,
            "used_usd": round(self.used_usd, 6),
            "remaining_usd": round(self.cap_usd - self.used_usd, 6),
            "by_phase": self.by_phase,
            "call_count": len(self.calls),
        }


def install_budget_and_token_caps(guard: BudgetGuard) -> None:
    """Patch the real adapter classes so every completion:

    * has ``max_tokens`` clamped to ``guard.max_tokens``,
    * is blocked if the budget is already exhausted,
    * has its cost added to the guard after returning.

    The mock adapter is NOT patched (it's not used in a real run). We patch
    per-class rather than the abstract base so only production adapters are
    affected.
    """
    from app.providers.anthropic_adapter import AnthropicProvider
    from app.providers.base import CompletionRequest
    from app.providers.openai_adapter import OpenAIProvider

    def _wrap(cls):
        original = cls.complete

        async def capped(self, req: CompletionRequest, *, _orig=original):
            from app.config.model_alias import supports_reasoning_effort

            phase = req.phase or req.task_kind or "unknown"
            guard.precheck(phase)
            # Slow reasoning models (gpt-5-series with medium/high effort on
            # a long code-gen prompt) regularly take >60s to first token.
            # The policy table's default 60s timeout cancels the call before
            # the model finishes, producing silent failures — we bump to 240s
            # here so real code generation actually lands.
            if hasattr(self, "_timeout"):
                try:
                    self._timeout = max(float(self._timeout or 0), 240.0)
                except (TypeError, ValueError):
                    pass
            # Token budget logic:
            # - Cap every call at guard.max_tokens (default 8192).
            # - For reasoning-capable models with non-minimal effort, enforce a
            #   floor of 4096 because reasoning tokens are billed against the
            #   output budget. A lower cap starves the actual JSON output and
            #   results in truncated / empty responses.
            effective = int(req.max_tokens or 0)
            wire_model = req.model or getattr(self, "model", "") or ""
            is_reasoning = supports_reasoning_effort(wire_model) and (
                req.reasoning_effort in ("low", "medium", "high", "xhigh")
            )
            if is_reasoning:
                effective = max(effective, 4096)
            if effective > guard.max_tokens:
                effective = guard.max_tokens
            if effective and effective != req.max_tokens:
                req = replace(req, max_tokens=effective)
            result = await _orig(self, req)
            # Prefer the split estimate; fall back to blended.
            cost = float(
                getattr(result, "estimated_cost_usd", 0.0)
                or getattr(result, "estimated_cost", 0.0)
                or 0.0
            )
            model_id = getattr(result, "actual_model", None) or getattr(
                result, "model", ""
            )
            guard.record(phase=phase, cost=cost, model=model_id)
            if guard.used_usd > guard.cap_usd:
                raise BudgetExceeded(
                    f"budget_exceeded after {phase}: "
                    f"used ${guard.used_usd:.4f} of ${guard.cap_usd:.2f}"
                )
            return result

        cls.complete = capped

    _wrap(OpenAIProvider)
    _wrap(AnthropicProvider)


# ---------------------------------------------------------------------------
# 4. Project + pipeline
# ---------------------------------------------------------------------------


PROJECT_ID = f"real_run_{datetime.utcnow():%Y%m%d_%H%M%S}"

PROJECT_BRIEF = dict(
    title="Small-scale exploration: training stability tricks on a toy classifier",
    student_name="real_run_student",
    mentor_name="real_run_mentor",
    research_direction=(
        "Compare small, falsifiable training-time regularisation / curriculum / "
        "stopping tricks against a simple vanilla baseline on a synthetic binary "
        "classification task. Each idea must run in under 90 seconds on CPU "
        "using Python 3.11 + numpy only. Measure accuracy, calibration (Brier), "
        "and epochs-to-converge. Report deltas vs. baseline and be explicit about "
        "noise / overlap."
    ),
    target_venues=["Internal technical report"],
    constraints=(
        "CPU-only, <=90s wall-clock per run, Python 3.11 stdlib plus numpy ONLY. "
        "No torch, no sklearn, no pandas, no matplotlib, no network access. "
        "Metrics must be real numbers derived from actual data, never hard-coded."
    ),
    exploration_strategy="focused",
    provider_profile="default",
)


def _ensure_fresh_project() -> str:
    from app.core.models import StudentProject
    from app.core.schemas import ProjectCreateIn
    from app.db.session import SessionLocal
    from app.services import ResearchBriefService

    with SessionLocal() as db:
        existing = db.query(StudentProject).filter(StudentProject.id == PROJECT_ID).first()
        if existing is not None:
            return existing.id
        # Pass the stable id through so the brief + budget_policy rows are
        # FK'd to the same id from the start. The previous pattern
        # (create with generated id, then rewrite project.id afterwards)
        # orphaned related rows against the old generated proj_* id.
        ResearchBriefService(db).create_project(
            ProjectCreateIn(
                **PROJECT_BRIEF,
                budget_usd=30.0,
                notes="Orchestrator run with $30 cap, reduced 20->8->3 funnel.",
            ),
            project_id=PROJECT_ID,
        )
    return PROJECT_ID


def _clean_conflicting_credentials() -> None:
    """Remove any stored ``mock`` credential from earlier sessions so the
    router never routes a real-run phase through mock.  Keeps real openai /
    anthropic credentials intact."""
    from app.core.models import ProviderCredential
    from app.db.session import SessionLocal

    with SessionLocal() as db:
        for c in db.query(ProviderCredential).filter(
            ProviderCredential.provider == "mock"
        ).all():
            log(f"removing lingering mock credential {c.id}")
            db.delete(c)
        db.commit()


def _diversity_pick(ideas: list[Any], k: int) -> list[Any]:
    """Pick ``k`` ideas, round-robin across ``cluster_tag`` then by score.

    Falls back to score-only when cluster_tag is missing.
    """
    by_cluster: dict[str, list[Any]] = {}
    for i in ideas:
        tag = (getattr(i, "cluster_tag", None) or "unclustered").lower()
        by_cluster.setdefault(tag, []).append(i)
    for tag, group in by_cluster.items():
        group.sort(
            key=lambda x: (getattr(x, "score", None) or 0.0, getattr(x, "created_at", 0)),
            reverse=True,
        )
    chosen: list[Any] = []
    cluster_order = sorted(
        by_cluster.keys(), key=lambda t: -max(
            (getattr(i, "score", None) or 0.0) for i in by_cluster[t]
        )
    )
    while len(chosen) < k and any(by_cluster.values()):
        for tag in cluster_order:
            bucket = by_cluster.get(tag) or []
            if not bucket:
                continue
            chosen.append(bucket.pop(0))
            if len(chosen) >= k:
                break
    return chosen


async def run_pipeline(
    *,
    guard: BudgetGuard,
    n_ideas: int,
    n_exp: int,
    n_final: int,
    probe: dict,
) -> dict:
    from app.core.enums import FunnelStage
    from app.core.models import Claim, ExperimentRun, Idea
    from app.core.schemas import (
        DraftGenerateIn,
        FunnelAdvanceIn,
        IdeaDecisionIn,
        IdeaGenerateIn,
        PackageCreateIn,
        ReviewRunIn,
        RunStartIn,
        SpecGenerateIn,
    )
    from app.db.session import SessionLocal
    from app.services import (
        DraftService,
        ExperimentRunnerService,
        FunnelService,
        IdeaGenerationService,
        PackageService,
        ResultAnalysisService,
        ReviewService,
        SpecService,
    )

    project_id = _ensure_fresh_project()
    log(f"project: {project_id}")

    # --- Phase 1: idea generation --------------------------------------
    guard.precheck("idea_generation")
    with SessionLocal() as db:
        ideas = await IdeaGenerationService(db).generate(
            project_id, IdeaGenerateIn(count=n_ideas)
        )
        idea_ids = [i.id for i in ideas]
    log(
        f"generated {len(idea_ids)} ideas (cumulative ${guard.used_usd:.4f})"
    )
    if not idea_ids:
        raise RuntimeError("idea_generation returned nothing")

    # --- Phase 2: ranking ----------------------------------------------
    guard.precheck("idea_ranking")
    with SessionLocal() as db:
        scores = await FunnelService(db).score(project_id, FunnelStage.S0.value)
    log(
        f"scored {len(scores)} ideas (cumulative ${guard.used_usd:.4f})"
    )

    # --- Phase 2b: diversity-aware top-8 promotion ---------------------
    with SessionLocal() as db:
        fresh = (
            db.query(Idea)
            .filter(Idea.project_id == project_id, Idea.stage == "S0")
            .all()
        )
        top = _diversity_pick(fresh, n_exp)
        # Promote the chosen ones to S1, reject the rest.
        top_ids = {i.id for i in top}
        for idea in fresh:
            if idea.id in top_ids:
                FunnelService(db).apply_decision(
                    idea.id,
                    IdeaDecisionIn(
                        decision="promote",
                        rationale="diversity-aware top-8",
                        promote_to_stage=FunnelStage.S1.value,
                    ),
                )
            else:
                FunnelService(db).apply_decision(
                    idea.id,
                    IdeaDecisionIn(decision="reject", rationale="not in top-8"),
                )
    log(f"promoted {len(top_ids)} ideas to S1")

    # --- Phase 3+4+5+6: spec, run, analyze per promoted idea ----------
    per_idea: list[dict] = []
    with SessionLocal() as db:
        promoted = (
            db.query(Idea)
            .filter(Idea.project_id == project_id, Idea.stage == "S1")
            .order_by(Idea.score.desc().nullslast())
            .all()
        )
        promoted_ids = [i.id for i in promoted]

    for idx, idea_id in enumerate(promoted_ids):
        try:
            guard.precheck(f"spec_generation[{idx}]")
            with SessionLocal() as db:
                spec = await SpecService(db).generate(
                    project_id,
                    SpecGenerateIn(
                        idea_id=idea_id,
                        extra_instructions=(
                            "Hard constraints: Python 3.11 stdlib + numpy only. "
                            "No torch, no sklearn, no pandas, no network. "
                            "The experiment must synthesise its own tiny dataset "
                            "and produce a metrics.json with real, measured numbers. "
                            "Both baseline and variant must be evaluated on the same "
                            "held-out split."
                        ),
                    ),
                )
                spec_id = spec.id
            log(
                f"[{idx + 1}/{len(promoted_ids)}] spec OK "
                f"(cumulative ${guard.used_usd:.4f})"
            )

            guard.precheck(f"run[{idx}]")
            with SessionLocal() as db:
                run = await ExperimentRunnerService(db).start_and_run(
                    project_id,
                    RunStartIn(
                        spec_id=spec_id,
                        worker="two_step",
                        seed=42 + idx,
                        extra_instructions=(
                            "Do not fabricate metrics. All numbers must be derived "
                            "from actual data produced by the code."
                        ),
                    ),
                )
                run_id = run.id
                run_status = run.status
                run_result = run.result_class
                run_mock = run.mock
                run_cost = float(run.total_estimated_cost or 0.0)
            log(
                f"[{idx + 1}/{len(promoted_ids)}] run {run_id[:16]} "
                f"status={run_status} result={run_result} mock={run_mock} "
                f"cost_this_run=${run_cost:.4f} "
                f"cumulative=${guard.used_usd:.4f}"
            )

            guard.precheck(f"analyze[{idx}]")
            claim_count = 0
            verdict = None
            try:
                with SessionLocal() as db:
                    analysis = await ResultAnalysisService(db).analyze(run_id)
                    verdict = getattr(analysis, "verdict", None)
                    claim_count = len(getattr(analysis, "claim_ids", []) or [])
                log(
                    f"[{idx + 1}/{len(promoted_ids)}] analyzed verdict={verdict} "
                    f"claims={claim_count} (cumulative ${guard.used_usd:.4f})"
                )
            except Exception as e:  # noqa: BLE001
                log(f"[{idx + 1}/{len(promoted_ids)}] analyze ERROR: {e}")

            # If the run failed, surface its stderr tail so the operator can
            # see *why* without having to open the workspace dir.
            if run_status != "succeeded":
                try:
                    with SessionLocal() as db:
                        from app.core.models import ExperimentRun as _Run

                        row = (
                            db.query(_Run).filter(_Run.id == run_id).first()
                        )
                        if row is not None:
                            stderr_tail = (row.stderr_log or "")[-600:].strip()
                            stdout_tail = (row.stdout_log or "")[-400:].strip()
                            summary = (row.summary or "").strip()
                            if summary:
                                log(
                                    f"[{idx + 1}/{len(promoted_ids)}] summary: "
                                    f"{summary[:400]}"
                                )
                            if stderr_tail:
                                log(
                                    f"[{idx + 1}/{len(promoted_ids)}] stderr: "
                                    f"{stderr_tail[:400]}"
                                )
                            elif stdout_tail:
                                log(
                                    f"[{idx + 1}/{len(promoted_ids)}] stdout: "
                                    f"{stdout_tail[:400]}"
                                )
                            log(
                                f"[{idx + 1}/{len(promoted_ids)}] workspace="
                                f"{row.workspace_path} exit={row.exit_code}"
                            )
                except Exception as e:  # noqa: BLE001
                    log(f"[{idx + 1}/{len(promoted_ids)}] stderr readback: {e}")

            per_idea.append(
                {
                    "idea_id": idea_id,
                    "spec_id": spec_id,
                    "run_id": run_id,
                    "run_status": run_status,
                    "result_class": run_result,
                    "verdict": verdict,
                    "claim_count": claim_count,
                    "run_cost_usd": run_cost,
                }
            )
        except BudgetExceeded as e:
            log(f"BUDGET HALTED during idea {idx + 1}: {e}")
            per_idea.append(
                {"idea_id": idea_id, "error": "budget_exceeded", "detail": str(e)}
            )
            break
        except Exception as e:  # noqa: BLE001
            log(f"[{idx + 1}/{len(promoted_ids)}] idea ERROR: {type(e).__name__}: {e}")
            per_idea.append(
                {
                    "idea_id": idea_id,
                    "error": type(e).__name__,
                    "detail": str(e)[:240],
                }
            )

    # --- Phase 7: diversity-aware top-3 --------------------------------
    # Re-read runs + verdicts, pick top-3 among those that actually produced
    # valid metrics. Prefer promising, then inconclusive, then rejected;
    # within a verdict band prefer lower variance / more claims.
    _VERDICT_RANK = {"promising": 3, "inconclusive": 2, "rejected": 1}

    def _sort_key(entry: dict) -> tuple:
        rank = _VERDICT_RANK.get((entry.get("verdict") or "").lower(), 0)
        return (rank, entry.get("claim_count", 0), -len(entry.get("error", "")))

    successful = [e for e in per_idea if e.get("run_status") == "succeeded"]
    successful.sort(key=_sort_key, reverse=True)
    final_3 = successful[:n_final]
    log(
        f"final {len(final_3)} selected: "
        + ", ".join(f"{e['idea_id'][:12]}({e.get('verdict')})" for e in final_3)
    )

    # Promote the final-3 from S1 to S2 for auditing.
    with SessionLocal() as db:
        for entry in final_3:
            try:
                FunnelService(db).apply_decision(
                    entry["idea_id"],
                    IdeaDecisionIn(
                        decision="promote",
                        rationale="selected for final write-up",
                        promote_to_stage=FunnelStage.S2.value,
                    ),
                )
            except Exception as e:  # noqa: BLE001
                log(f"S2 promote skipped for {entry['idea_id']}: {e}")

    # --- Phase 8: draft -------------------------------------------------
    draft_id = None
    try:
        guard.precheck("draft_generation")
        with SessionLocal() as db:
            draft = await DraftService(db).generate(
                project_id,
                DraftGenerateIn(
                    manuscript_title=(
                        "Small-scale exploration: training stability tricks "
                        "on a toy classifier"
                    ),
                    target_venue="Internal technical report",
                    include_run_ids=[e["run_id"] for e in final_3 if e.get("run_id")],
                ),
            )
            draft_id = draft.id
        log(f"draft v{draft.version} generated id={draft_id}")
    except BudgetExceeded as e:
        log(f"BUDGET HALTED before draft: {e}")
    except Exception as e:  # noqa: BLE001
        log(f"draft step ERROR: {e}")

    # --- Phase 8b: reviewers ----------------------------------------------
    review_count = 0
    review_summary: dict[str, Any] | None = None
    try:
        guard.precheck("manuscript_review")
        with SessionLocal() as db:
            issues = await ReviewService(db).run_reviewers(project_id, ReviewRunIn())
            review_count = len(issues)
            from app.services.review_summary_service import ReviewSummaryService

            review_summary = ReviewSummaryService(db).summary(project_id)
        log(f"review issues: {review_count}")
    except BudgetExceeded as e:
        log(f"reviewer phase halted by budget: {e}")
    except Exception as e:  # noqa: BLE001
        log(f"reviewer phase ERROR: {e}")

    # --- Phase 9: package ----------------------------------------------
    pkg_zip = None
    pkg_id = None
    package_status = "skipped"
    package_error = None
    try:
        summary = review_summary
        if summary is None:
            with SessionLocal() as db:
                from app.services.review_summary_service import ReviewSummaryService

                summary = ReviewSummaryService(db).summary(project_id)
        if (summary or {}).get("blocking_open") or (summary or {}).get("major_open"):
            package_status = "blocked_by_review"
            package_error = (
                "package freeze blocked by open P0/P1 review issues; "
                "resolve or explicitly waive them before external packaging"
            )
            log(f"package blocked: {package_error}")
        else:
            with SessionLocal() as db:
                pkg = await PackageService(db).build(
                    project_id,
                    PackageCreateIn(
                        allow_with_waived_p2=True,
                        include_mock=False,
                        notes=(
                            "First real orchestrated run. Strict $30 cap. "
                            "20 -> 8 -> 3 funnel, real experiments only."
                        ),
                    ),
                    require_approval=False,
                )
                pkg_zip = pkg.zip_path
                pkg_id = pkg.id
            package_status = "built"
            log(f"package id={pkg_id} at {pkg_zip}")
    except Exception as e:  # noqa: BLE001
        package_status = "error"
        package_error = str(e)
        log(f"package step ERROR: {e}")

    # --- Build report --------------------------------------------------
    with SessionLocal() as db:
        total_claims = (
            db.query(Claim).filter(Claim.project_id == project_id).count()
        )
        runs_in_db = (
            db.query(ExperimentRun)
            .filter(ExperimentRun.project_id == project_id)
            .all()
        )
        sample_claim = (
            db.query(Claim)
            .filter(Claim.project_id == project_id)
            .order_by(Claim.created_at.asc())
            .first()
        )
        sample_claim_out = None
        if sample_claim:
            sample_claim_out = {
                "id": sample_claim.id,
                "run_id": sample_claim.run_id,
                "text": sample_claim.text[:240],
                "value": sample_claim.value,
                "quantitative": sample_claim.quantitative,
            }

    report = {
        "project_id": project_id,
        "probe": {
            "openai": probe["openai"],
            "anthropic": probe["anthropic"],
        },
        "funnel": {
            "ideas": len(idea_ids),
            "promoted_to_S1": len(promoted_ids),
            "final_3": [e["idea_id"] for e in final_3],
        },
        "per_idea": per_idea,
        "claims_total": total_claims,
        "sample_claim": sample_claim_out,
        "draft_id": draft_id,
        "package_id": pkg_id,
        "package_zip": pkg_zip,
        "package_status": package_status,
        "package_error": package_error,
        "review_summary": review_summary,
        "total_runs_db": len(runs_in_db),
        "verdicts": {
            "promising": sum(1 for e in per_idea if e.get("verdict") == "promising"),
            "inconclusive": sum(
                1 for e in per_idea if e.get("verdict") == "inconclusive"
            ),
            "rejected": sum(1 for e in per_idea if e.get("verdict") == "rejected"),
            "failed_runs": sum(
                1 for e in per_idea if e.get("run_status") not in (None, "succeeded")
            ),
            "errors": sum(1 for e in per_idea if e.get("error")),
        },
        "cost": guard.summary(),
    }
    return report


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


async def amain() -> int:
    # Load settings + bootstrap credentials once.
    from app.core import models  # noqa: F401 register ORM
    from app.db.base import Base
    from app.db.session import SessionLocal, engine
    from app.services.credential_bootstrap_service import run_bootstrap

    Base.metadata.create_all(bind=engine)
    try:
        from app.db.migrate import run_upgrade

        run_upgrade()
    except Exception as e:  # noqa: BLE001
        log(f"alembic upgrade skipped: {e}")

    with SessionLocal() as db:
        report = run_bootstrap(db)
    log(
        f"bootstrap: openai={report.openai} anthropic={report.anthropic} "
        f"sources={report.sources_consulted}"
    )

    _clean_conflicting_credentials()

    # Probe.
    probe = await probe_available_models()
    log(
        f"probe: openai={probe['openai']} anthropic={probe['anthropic']}"
    )
    if not probe["openai"] or not probe["anthropic"]:
        log("probe trace openai: " + json.dumps(probe["openai_trace"]))
        log("probe trace anthropic: " + json.dumps(probe["anthropic_trace"]))
        raise SystemExit("probe failed — no usable model found for at least one provider")

    apply_runtime_policy_overrides(probe["openai"], probe["anthropic"])

    # Install budget guard. Default 8192 max_tokens per-call because
    # gpt-5-series reasoning models count reasoning tokens against the
    # output budget — a 4096 ceiling can starve the actual JSON output
    # when reasoning_effort=medium/high is set. 8192 is still squarely
    # inside the operator-approved band (2048-8192).
    guard = BudgetGuard(
        cap_usd=float(_os.environ.get("RESEARCHOS_REAL_RUN_BUDGET_USD", "30")),
        max_tokens=int(_os.environ.get("RESEARCHOS_REAL_RUN_MAX_TOKENS", "8192")),
    )
    install_budget_and_token_caps(guard)
    log(f"budget cap = ${guard.cap_usd:.2f}; per-call max_tokens = {guard.max_tokens}")

    # Run pipeline.
    n_ideas = int(_os.environ.get("RESEARCHOS_REAL_RUN_IDEAS", "20"))
    n_exp = int(_os.environ.get("RESEARCHOS_REAL_RUN_TOP_EXP", "8"))
    n_final = int(_os.environ.get("RESEARCHOS_REAL_RUN_TOP_FINAL", "3"))

    try:
        report = await run_pipeline(
            guard=guard,
            n_ideas=n_ideas,
            n_exp=n_exp,
            n_final=n_final,
            probe=probe,
        )
    except BudgetExceeded as e:
        log(f"BUDGET EXCEEDED: {e}")
        report = {"status": "budget_exceeded", "detail": str(e), "cost": guard.summary()}
    except Exception:  # noqa: BLE001
        log("pipeline ERROR:\n" + traceback.format_exc())
        report = {"status": "error", "cost": guard.summary()}

    report["probe_trace"] = {
        "openai": probe["openai_trace"],
        "anthropic": probe["anthropic_trace"],
    }

    log("=== FINAL REPORT ===")
    log(json.dumps(report, indent=2, default=str))
    # Also write to disk for easy grep later.
    out_path = (
        Path(__file__).resolve().parent
        / "var"
        / "real_run_reports"
        / f"{PROJECT_ID}.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    log(f"report written to {out_path}")
    if report.get("status") in {"error", "budget_exceeded"}:
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(amain()))
    except KeyboardInterrupt:
        raise SystemExit(130)
