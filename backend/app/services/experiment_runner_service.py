"""End-to-end experiment run orchestration.

Flow:

1. check HITL approval gate (unless explicitly bypassed by the batch caller)
2. check project budget ceiling
3. generate code via CodeWorkerService
4. run the code via JobRunner
5. classify the result (succeeded_valid / succeeded_invalid / failed_* / canceled)
6. persist metrics.json as an artifact
7. record the run's estimated cost in the budget ledger
8. audit the run_started + run_finished events

We keep the function `start_and_run` async so FastAPI routes can `await` it
directly for the local MVP. If you later want true background execution,
schedule this coroutine on a dedicated task queue.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.enums import AuditKind, GateKey, RunResultClass, RunStatus
from app.core.models import (
    Artifact,
    ExperimentRun,
    ExperimentSpec,
    Idea,
    StudentProject,
)
from app.core.schemas import RunStartIn
from app.services.approval_service import ApprovalService, GateDecision
from app.services.audit_service import AuditService
from app.services.budget_service import BudgetService
from app.services.code_worker_service import CodeWorkerService
from app.storage import get_artifact_store
from app.utils import get_logger, new_id
from app.workers import get_job_runner

logger = get_logger(__name__)


def pick_run_gate(db: Session, project_id: str) -> str:
    """Gate A vs Gate B based on whether at least one run has already succeeded.

    Shared between ``BatchOrchestratorService`` and single-run via
    ``ExperimentRunnerService`` so the two paths apply the same rule: the
    first batch OR first single run on a project triggers ``post_shortlist``;
    later runs trigger ``post_pilot_evidence`` instead.
    """
    # Gate B requires *valid* pilot evidence, not just exit-code-0.
    # A run with ``status=succeeded`` but ``result_class=succeeded_invalid``
    # means the script ran to completion without producing a metrics.json —
    # there is no pilot evidence, so the project stays at Gate A.
    has_valid_pilot = (
        db.query(ExperimentRun)
        .filter(
            ExperimentRun.project_id == project_id,
            ExperimentRun.status == RunStatus.succeeded.value,
            ExperimentRun.result_class == RunResultClass.succeeded_valid.value,
        )
        .first()
        is not None
    )
    return (
        GateKey.post_pilot_evidence.value
        if has_valid_pilot
        else GateKey.post_shortlist.value
    )


class RunBlockedError(Exception):
    """Raised when HITL is enabled and the run cannot proceed yet.

    The HTTP layer translates this into a 409 with a structured body so
    the UI can point at the pending approval id.
    """

    def __init__(
        self,
        *,
        stage_key: str,
        reason: str,
        approval_id: str | None,
        status: str,
    ) -> None:
        super().__init__(f"run blocked at {stage_key}: {reason}")
        self.stage_key = stage_key
        self.reason = reason
        self.approval_id = approval_id
        self.status = status  # "paused" | "blocked"


class BudgetExceededError(Exception):
    """Raised when the project's budget ceiling has already been spent.

    The HTTP layer translates this into a 402 (Payment Required) with a
    structured body describing ceiling + spent so the operator can raise
    the ceiling or wind the project down.
    """

    def __init__(
        self,
        *,
        project_id: str,
        spent_usd: float,
        ceiling_usd: float,
    ) -> None:
        super().__init__(
            f"budget ceiling reached for {project_id}: "
            f"${spent_usd:.4f} >= ${ceiling_usd:.4f}"
        )
        self.project_id = project_id
        self.spent_usd = float(spent_usd)
        self.ceiling_usd = float(ceiling_usd)


class ExperimentRunnerService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.audit = AuditService(db)

    async def start_and_run(
        self,
        project_id: str,
        payload: RunStartIn,
        *,
        provider_credential_id: str | None = None,
        require_approval: bool = True,
    ) -> ExperimentRun:
        spec = self.db.query(ExperimentSpec).filter(ExperimentSpec.id == payload.spec_id).first()
        if spec is None:
            raise LookupError(f"spec not found: {payload.spec_id}")
        if spec.project_id != project_id:
            raise ValueError("spec does not belong to this project")
        idea = self.db.query(Idea).filter(Idea.id == spec.idea_id).first()
        if idea is None:
            raise LookupError(f"idea not found for spec: {spec.id}")

        # --- HITL approval gate (single-run) ---------------------------
        # The batch orchestrator gates its own top-level path and then calls
        # start_and_run(require_approval=False), so we do not double-gate
        # within a batch. Direct /runs/start calls go through this branch so
        # there is no trivial approval bypass (starting a single run IS the
        # same pipeline work as starting a batch of one).
        if require_approval:
            project = (
                self.db.query(StudentProject)
                .filter(StudentProject.id == project_id)
                .first()
            )
            if project is not None and project.human_in_loop_enabled:
                gate_key = pick_run_gate(self.db, project_id)
                gate = await ApprovalService(self.db).ensure_gate(
                    project_id=project_id,
                    stage_key=gate_key,
                    context_snapshot={
                        "reason": "single_run_start",
                        "spec_id": payload.spec_id,
                        "worker": payload.worker,
                        "gate_key": gate_key,
                    },
                )
                if gate.decision is GateDecision.paused:
                    raise RunBlockedError(
                        stage_key=gate_key,
                        reason=gate.reason or "awaiting_approval",
                        approval_id=gate.approval.id if gate.approval else None,
                        status="paused",
                    )
                if gate.decision is GateDecision.blocked:
                    raise RunBlockedError(
                        stage_key=gate_key,
                        reason=gate.reason or "blocked",
                        approval_id=gate.approval.id if gate.approval else None,
                        status="blocked",
                    )

        # --- Budget ceiling precheck ----------------------------------
        # Only blocks when a BudgetPolicy with a positive ceiling exists and
        # the project has already spent at or beyond it. Projects without a
        # ceiling (ceiling_usd == 0) are unrestricted — useful for tests and
        # mock mode.
        budget = BudgetService(self.db)
        summary = budget.summary(project_id)
        ceiling = float(summary.get("ceiling_usd") or 0.0)
        spent = float(summary.get("spent_usd") or 0.0)
        # Predictive precheck: if the spec carries a positive
        # ``budget_estimate_usd``, refuse to start when ``spent + estimate``
        # would cross the ceiling. When the estimate is zero or missing we
        # fall back to the retrospective rule (block only when spent has
        # already reached the ceiling) — that preserves behavior for specs
        # the planner did not estimate, while still catching the overspend
        # case for specs that did.
        expected_increment = float(getattr(spec, "budget_estimate_usd", 0.0) or 0.0)
        if ceiling > 0:
            projected = spent + max(0.0, expected_increment)
            if expected_increment > 0 and projected > ceiling:
                raise BudgetExceededError(
                    project_id=project_id,
                    spent_usd=spent,
                    ceiling_usd=ceiling,
                )
            if spent >= ceiling:
                raise BudgetExceededError(
                    project_id=project_id,
                    spent_usd=spent,
                    ceiling_usd=ceiling,
                )

        settings = get_settings()
        run_id = new_id("run")
        seed = payload.seed if payload.seed is not None else 0

        run = ExperimentRun(
            id=run_id,
            project_id=project_id,
            spec_id=spec.id,
            idea_id=idea.id,
            workspace_path="",
            status=RunStatus.queued.value,
            seed=seed,
            provider_routing={},
            config={
                "worker": payload.worker,
                "extra_instructions": payload.extra_instructions,
                "spec_version": spec.version,
            },
            started_at=datetime.utcnow(),
        )
        self.db.add(run)
        self.db.flush()

        self.audit.log(
            project_id=project_id,
            kind=AuditKind.run_started,
            message=f"Run queued for {idea.title[:40]} (worker={payload.worker})",
            subject_kind="run",
            subject_id=run_id,
            payload={"seed": seed, "worker": payload.worker},
        )
        self.db.commit()

        # 1. Generate code
        try:
            code_info = await CodeWorkerService(self.db).generate_code(
                spec=spec,
                idea=idea,
                run_id=run_id,
                worker=payload.worker,
                seed=seed,
                extra_instructions=payload.extra_instructions,
                provider_credential_id=provider_credential_id,
            )
        except Exception as e:
            self._finalize_failed(run, reason=f"code generation failed: {e!s}")
            return run

        from app.config import active_run_mode

        run.workspace_path = code_info["workspace_path"]
        run.code_hash = code_info["code_hash"]
        run.total_estimated_cost = float(code_info.get("estimated_cost_usd", 0.0) or 0.0)
        run.provider_routing = {
            "worker": code_info["worker"],
            "provider": code_info["provider"],
            "model": code_info["model"],
            "run_mode": active_run_mode().value,
            # Pinned literal. Every code-generation call in ResearchOS flows
            # through an HTTP provider adapter; the runtime never shells out
            # to an interactive Claude Code / Codex session. This field lets
            # any future reader tell that at a glance.
            "execution_mode": "headless_api",
        }
        config_updates: dict[str, Any] = {
            "dependencies": list(code_info.get("dependencies") or []),
            "used_fallback_code": bool(code_info.get("used_fallback")),
        }
        if code_info.get("diagnostics"):
            config_updates["code_worker"] = {"diagnostics": code_info["diagnostics"]}
        self._merge_run_config(run, **config_updates)
        run.mock = bool(code_info.get("mock"))
        run.status = RunStatus.running.value
        self.db.commit()

        # 2. Execute
        job = get_job_runner()
        setup = await job.prepare_python_env(
            cwd=Path(code_info["workspace_path"]) / "code"
        )
        self._merge_run_config(
            run,
            environment_setup={
                "ok": bool(setup.ok),
                "skipped": bool(setup.skipped),
                "installed": list(setup.installed),
                "python_executable": setup.python_executable,
            },
        )
        self._persist_setup_logs(run, setup)
        if not setup.ok:
            run.status = RunStatus.failed.value
            run.result_class = RunResultClass.failed_retriable.value
            run.exit_code = 127
            run.ended_at = datetime.utcnow()
            run.summary = "environment setup failed"
            self.audit.log(
                project_id=project_id,
                kind=AuditKind.run_finished,
                message="Run failed during environment setup",
                subject_kind="run",
                subject_id=run_id,
                payload={"dependencies": list(setup.installed)},
            )
            self.db.commit()
            return run
        outcome = await job.run_python(
            cwd=Path(code_info["workspace_path"]) / "code",
            script="train.py",
            timeout=settings.run_timeout,
            python_executable=setup.python_executable,
        )

        # 3. Persist logs + metrics
        self._persist_logs(run, outcome)
        metrics = self._load_metrics(code_info["workspace_path"])
        run.metrics = metrics or {}

        # 4. Classify
        timeout_boundary_with_metrics = (
            bool(outcome.timed_out)
            and outcome.exit_code == 0
            and self._metrics_are_usable(metrics)
        )
        if timeout_boundary_with_metrics:
            run.status = RunStatus.succeeded.value
            run.result_class = RunResultClass.succeeded_valid.value
            self._merge_run_config(
                run,
                timeout_boundary={
                    "timed_out": True,
                    "exit_code": outcome.exit_code,
                    "metrics_accepted": True,
                    "reason": (
                        "process reached timeout boundary but exited 0 and "
                        "produced usable metrics"
                    ),
                },
            )
        elif outcome.timed_out:
            run.status = RunStatus.timed_out.value
            run.result_class = RunResultClass.failed_retriable.value
        elif self._metrics_are_usable(metrics):
            run.status = RunStatus.succeeded.value
            run.result_class = RunResultClass.succeeded_valid.value
        elif outcome.exit_code == 0:
            run.status = RunStatus.succeeded.value
            run.result_class = (
                RunResultClass.succeeded_valid.value
                if metrics
                else RunResultClass.succeeded_invalid.value
            )
        else:
            run.status = RunStatus.failed.value
            run.result_class = (
                RunResultClass.failed_terminal.value
                if outcome.exit_code < 0
                else RunResultClass.failed_retriable.value
            )
        run.exit_code = outcome.exit_code
        run.ended_at = datetime.utcnow()
        run.summary = f"exit={outcome.exit_code} duration={outcome.duration_s:.2f}s"
        if timeout_boundary_with_metrics:
            run.summary += " (timeout boundary; metrics accepted)"

        self.audit.log(
            project_id=project_id,
            kind=AuditKind.run_finished,
            message=f"Run finished status={run.status} result={run.result_class}",
            subject_kind="run",
            subject_id=run_id,
            payload={
                "exit_code": outcome.exit_code,
                "duration_s": outcome.duration_s,
                "has_metrics": bool(metrics),
                "timed_out": bool(outcome.timed_out),
                "timeout_boundary_with_metrics": timeout_boundary_with_metrics,
                "mock": run.mock,
            },
        )
        self.db.commit()

        # --- Budget ledger run marker ---------------------------------
        # Provider calls are now recorded at call-site granularity with
        # kind="provider_call". Keep a zero-dollar run marker for traceability
        # without double-counting the same code/review spend in budget totals.
        # Best-effort: a ledger write failure must not fail a run that has
        # already completed.
        try:
            cost = float(run.total_estimated_cost or 0.0)
            if cost > 0:
                budget.record(
                    project_id=project_id,
                    amount_usd=0.0,
                    kind="run",
                    reference=run_id,
                    meta={
                        "aggregate_estimated_cost_usd": round(cost, 6),
                        "status": run.status,
                        "result_class": run.result_class,
                        "provider_routing": run.provider_routing,
                        "mock": bool(run.mock),
                    },
                )
        except Exception as e:  # noqa: BLE001
            logger.warning("ledger entry skipped for run %s: %s", run_id, e)
        return run

    # --- helpers -----------------------------------------------------------

    def _persist_logs(self, run: ExperimentRun, outcome) -> None:
        ws = Path(run.workspace_path)
        logs_dir = ws / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        (logs_dir / "stdout.log").write_text(outcome.stdout, encoding="utf-8")
        (logs_dir / "stderr.log").write_text(outcome.stderr, encoding="utf-8")
        # truncate in DB for quick UI display
        run.stdout_log = (outcome.stdout or "")[-20_000:]
        run.stderr_log = (outcome.stderr or "")[-20_000:]

        artifact_store = get_artifact_store()
        for name in ("stdout.log", "stderr.log"):
            src = logs_dir / name
            if src.exists():
                stored = artifact_store.copy_in(
                    run.project_id, f"runs/{run.id}/logs/{name}", src
                )
                self.db.add(
                    Artifact(
                        id=new_id("art"),
                        project_id=run.project_id,
                        run_id=run.id,
                        kind="log",
                        name=name,
                        path=str(stored.path),
                        sha256=stored.sha256,
                        size_bytes=stored.size_bytes,
                        mock=run.mock,
                    )
                )

    def _persist_setup_logs(self, run: ExperimentRun, setup) -> None:
        if setup.skipped and not setup.stdout and not setup.stderr:
            return
        ws = Path(run.workspace_path)
        logs_dir = ws / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        entries = {
            "setup.stdout.log": setup.stdout or "",
            "setup.stderr.log": setup.stderr or "",
        }
        artifact_store = get_artifact_store()
        for name, text in entries.items():
            target = logs_dir / name
            target.write_text(text, encoding="utf-8")
            stored = artifact_store.copy_in(
                run.project_id, f"runs/{run.id}/logs/{name}", target
            )
            self.db.add(
                Artifact(
                    id=new_id("art"),
                    project_id=run.project_id,
                    run_id=run.id,
                    kind="log",
                    name=name,
                    path=str(stored.path),
                    sha256=stored.sha256,
                    size_bytes=stored.size_bytes,
                    mock=run.mock,
                )
            )

    def _load_metrics(self, workspace_path: str) -> dict | None:
        metrics_path = self._find_output_file(workspace_path, "metrics.json")
        if not metrics_path.exists():
            return None
        try:
            data = json.loads(metrics_path.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            logger.warning("failed to parse metrics.json: %s", e)
            return None
        if isinstance(data, dict):
            sanitized = self._sanitize_metrics(data, workspace_path)
            if sanitized != data:
                data = sanitized
                metrics_path.write_text(
                    json.dumps(data, indent=2, default=str) + "\n",
                    encoding="utf-8",
                )

        artifact_store = get_artifact_store()
        stored = artifact_store.copy_in(
            # project_id inferred from workspace path format .../<project>/<run>
            Path(workspace_path).parent.name,
            f"runs/{Path(workspace_path).name}/outputs/metrics.json",
            metrics_path,
        )
        # Associate as an artifact
        self.db.add(
            Artifact(
                id=new_id("art"),
                project_id=Path(workspace_path).parent.name,
                run_id=Path(workspace_path).name,
                kind="metrics",
                name="metrics.json",
                path=str(stored.path),
                sha256=stored.sha256,
                size_bytes=stored.size_bytes,
                mock=bool(data.get("mock")) if isinstance(data, dict) else False,
            )
        )
        # Also copy predictions if present.
        preds = self._find_output_file(workspace_path, "predictions.json")
        if preds.exists():
            stored_p = artifact_store.copy_in(
                Path(workspace_path).parent.name,
                f"runs/{Path(workspace_path).name}/outputs/predictions.json",
                preds,
            )
            self.db.add(
                Artifact(
                    id=new_id("art"),
                    project_id=Path(workspace_path).parent.name,
                    run_id=Path(workspace_path).name,
                    kind="artifact",
                    name="predictions.json",
                    path=str(stored_p.path),
                    sha256=stored_p.sha256,
                    size_bytes=stored_p.size_bytes,
                    mock=bool(data.get("mock")) if isinstance(data, dict) else False,
                )
            )
        return data if isinstance(data, dict) else None

    def _find_output_file(self, workspace_path: str, name: str) -> Path:
        roots = [
            Path(workspace_path) / "outputs",
            Path(workspace_path) / "code" / "outputs",
        ]
        for root in roots:
            candidate = root / name
            if candidate.exists():
                return candidate
        return roots[0] / name

    def _metrics_are_usable(self, metrics: dict[str, Any] | None) -> bool:
        if not isinstance(metrics, dict) or not metrics:
            return False
        if any(k in metrics for k in ("baseline", "variant", "delta", "overlap_results", "optimal_lambdas")):
            return True
        numeric_leaf_count = 0
        stack: list[Any] = [metrics]
        ignored = {"seed", "status", "variant_name", "error", "error_type", "error_message"}
        while stack:
            current = stack.pop()
            if isinstance(current, dict):
                for key, value in current.items():
                    if key in ignored:
                        continue
                    stack.append(value)
                continue
            if isinstance(current, list):
                stack.extend(current)
                continue
            if isinstance(current, (int, float)) and not isinstance(current, bool):
                numeric_leaf_count += 1
                if numeric_leaf_count >= 2:
                    return True
        return False

    def _merge_run_config(self, run: ExperimentRun, **updates: Any) -> None:
        merged = dict(run.config or {})
        merged.update(updates)
        run.config = merged

    def _sanitize_metrics(self, metrics: dict[str, Any], workspace_path: str) -> dict[str, Any]:
        workspace = Path(workspace_path).resolve()
        roots: list[tuple[Path, str]] = [
            ((workspace / "outputs").resolve(), "outputs"),
            ((workspace / "code" / "outputs").resolve(), "outputs"),
            (workspace, "workspace"),
        ]

        def scrub(value: Any) -> Any:
            if isinstance(value, dict):
                return {k: scrub(v) for k, v in value.items()}
            if isinstance(value, list):
                return [scrub(v) for v in value]
            if isinstance(value, str):
                return self._sanitize_metrics_string(value, roots)
            return value

        return scrub(metrics)

    def _sanitize_metrics_string(
        self, value: str, roots: list[tuple[Path, str]]
    ) -> str:
        normalized = value.replace("\\", "/")
        for root, label in roots:
            prefix = root.as_posix()
            if normalized == prefix:
                return label
            if normalized.startswith(prefix + "/"):
                rel = normalized[len(prefix) + 1 :]
                return f"{label}/{rel}"
        return value

    def _finalize_failed(self, run: ExperimentRun, *, reason: str) -> None:
        run.status = RunStatus.failed.value
        run.result_class = RunResultClass.failed_terminal.value
        run.summary = reason
        run.ended_at = datetime.utcnow()
        self.audit.log(
            project_id=run.project_id,
            kind=AuditKind.run_finished,
            message=f"Run failed early: {reason}",
            subject_kind="run",
            subject_id=run.id,
            payload={"reason": reason},
        )
        self.db.commit()

    # --- queries -----------------------------------------------------------

    def list(self, project_id: str) -> list[ExperimentRun]:
        return (
            self.db.query(ExperimentRun)
            .filter(ExperimentRun.project_id == project_id)
            .order_by(ExperimentRun.created_at.desc())
            .all()
        )

    def get(self, run_id: str) -> ExperimentRun:
        run = self.db.query(ExperimentRun).filter(ExperimentRun.id == run_id).first()
        if run is None:
            raise LookupError(f"run not found: {run_id}")
        return run
