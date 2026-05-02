from __future__ import annotations

import json
import os
from pathlib import Path
import importlib

import pytest


def test_relative_sqlite_url_resolves_under_repo_root(monkeypatch):
    from app.config import get_settings, reset_settings_cache

    monkeypatch.setenv("RESEARCHOS_DB_URL", "sqlite:///var/data/researchos.db")
    reset_settings_cache()
    settings = get_settings()
    expected = settings.resolve_path(Path("var/data/researchos.db")).as_posix()
    assert settings.db_url == f"sqlite:///{expected}"
    reset_settings_cache()


def test_sqlite_engine_uses_busy_timeout():
    from app.db.session import engine

    if not str(engine.url).startswith("sqlite"):
        pytest.skip("busy_timeout is SQLite-specific")

    conn = engine.raw_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("PRAGMA busy_timeout")
        timeout_ms = cursor.fetchone()[0]
    finally:
        cursor.close()
        conn.close()

    assert timeout_ms >= 30000


@pytest.mark.asyncio
async def test_job_runner_returns_when_returncode_is_seen_but_pipes_hang(
    monkeypatch, tmp_path
):
    import asyncio

    from app.workers.job_runner import JobRunner

    class _FakeProc:
        returncode = 0

        async def communicate(self):
            await asyncio.sleep(60)
            return b"", b""

    async def _fake_spawn(*args, **kwargs):
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_spawn)

    outcome = await JobRunner(max_concurrent=1, default_timeout=5)._run_exec(
        ["python"],
        cwd=tmp_path,
        timeout=5,
    )

    assert outcome.exit_code == 0
    assert outcome.timed_out is False
    assert "output pipes did not close" in outcome.stderr


@pytest.mark.asyncio
async def test_idea_generation_recovers_truncated_json_and_clamps_to_requested(
    fresh_db, monkeypatch
):
    idea_mod = importlib.import_module("app.services.idea_generation_service")
    from app.core.schemas import IdeaGenerateIn, ProjectCreateIn
    from app.db.session import SessionLocal
    from app.providers.base import CompletionResult
    from app.services import ResearchBriefService

    class _FakeAdapter:
        async def complete(self, req):
            return CompletionResult(
                provider="anthropic",
                model="fake-haiku",
                text=(
                    '```json\n{"ideas": ['
                    '{"title": "Idea A", "summary": "One", "hypothesis": "H1", '
                    '"novelty_claim": "N1", "target_metric": "M1", "cluster_tag": "eval"}, '
                    '{"title": "Idea B", "summary": "Two", "hypothesis": "H2", '
                    '"novelty_claim": "N2", "target_metric": "M2", "cluster_tag": "data"}'
                ),
                raw={"stop_reason": "max_tokens"},
            )

    class _FakeResolved:
        adapter = _FakeAdapter()

    class _FakeRouter:
        def resolve_with_policy(self, policy):
            return _FakeResolved()

    monkeypatch.setattr(idea_mod, "get_provider_router", lambda db: _FakeRouter())

    with SessionLocal() as db:
        project = ResearchBriefService(db).create_project(
            ProjectCreateIn(
                title="Validation fix project",
                student_name="Owner",
                mentor_name="Reviewer",
                research_direction="Test idea recovery.",
                target_venues=["Internal"],
                constraints="cheap",
                exploration_strategy="focused",
                budget_usd=5.0,
            )
        )

    with SessionLocal() as db:
        ideas = await idea_mod.IdeaGenerationService(db).generate(
            project.id, IdeaGenerateIn(count=1)
        )

    assert len(ideas) == 1
    assert ideas[0].title == "Idea A"


@pytest.mark.asyncio
async def test_code_worker_service_adds_requirements_and_environment_doc(
    fresh_db, monkeypatch
):
    from app.core.models import ExperimentSpec, Idea
    from app.core.schemas import ProjectCreateIn
    from app.db.session import SessionLocal
    from app.services import ResearchBriefService
    from app.services.code_worker_service import CodeWorkerService
    from app.workers.base import CodeWorkerResult

    async def _fake_builder(self, req):
        return CodeWorkerResult(
            files=[
                {"path": "requirements.txt", "content": "numpy==1.26.4\n"},
                {
                    "path": "train.py",
                    "content": (
                        "import json\n"
                        "import numpy as np\n"
                        "from pathlib import Path\n"
                        "OUT = Path(__file__).resolve().parent.parent / 'outputs'\n"
                        "OUT.mkdir(parents=True, exist_ok=True)\n"
                        "arr = np.array([1.0, 2.0, 3.0])\n"
                        "(OUT / 'metrics.json').write_text(json.dumps({"
                        "'baseline': {'mean': 1.5}, 'variant': {'mean': float(arr.mean())}, "
                        "'delta': {'mean': float(arr.mean()) - 1.5}}))\n"
                    ),
                }
            ],
            summary="builder returned numpy experiment",
            provider="openai",
            model="fake",
            mock=False,
        )

    monkeypatch.setattr(
        "app.services.code_worker_service.ClaudeCodeWorker.run", _fake_builder
    )

    with SessionLocal() as db:
        project = ResearchBriefService(db).create_project(
            ProjectCreateIn(
                title="Deps project",
                student_name="Owner",
                mentor_name="Reviewer",
                research_direction="Validate dependency manifest generation.",
                target_venues=["Internal"],
            )
        )
        idea = Idea(
            id="idea_dep_case",
            project_id=project.id,
            title="Dependency case",
            summary="Needs numpy.",
        )
        spec = ExperimentSpec(
            id="spec_dep_case",
            project_id=project.id,
            idea_id=idea.id,
            version=1,
            hypothesis="h",
            problem_framing="p",
            target_metrics=["mean"],
            dataset_assumptions="d",
            baseline="b",
            experiment_plan="e",
            constraints="c",
            success_criteria=[],
            stop_criteria=[],
            budget_estimate_usd=0.1,
        )
        db.add(idea)
        db.add(spec)
        db.commit()

    with SessionLocal() as db:
        out = await CodeWorkerService(db).generate_code(
            spec=db.query(ExperimentSpec).filter(ExperimentSpec.id == "spec_dep_case").first(),
            idea=db.query(Idea).filter(Idea.id == "idea_dep_case").first(),
            run_id="run_dep_case",
            worker="claude_code",
            seed=0,
            extra_instructions=None,
            provider_credential_id=None,
        )

    assert "numpy" in out["dependencies"]
    code_root = Path(out["workspace_path"]) / "code"
    assert (code_root / "requirements.txt").exists()
    reqs = (code_root / "requirements.txt").read_text(encoding="utf-8")
    # The exact pin (e.g. "numpy>=2.1" vs "numpy==1.26.4") depends on the
    # numpy version installed in the runtime venv at code-worker time, so
    # only assert that requirements.txt carries a numpy entry of some shape.
    assert any(
        line.startswith("numpy") for line in reqs.splitlines()
    ), f"numpy entry missing from requirements.txt: {reqs!r}"
    assert (code_root / "ENVIRONMENT.md").exists()


@pytest.mark.asyncio
async def test_real_builder_empty_output_fails_instead_of_fallback(
    fresh_db, monkeypatch
):
    from app.core.models import ExperimentSpec, Idea
    from app.core.schemas import ProjectCreateIn
    from app.db.session import SessionLocal
    from app.services import ResearchBriefService
    from app.services.code_worker_service import CodeWorkerService
    from app.workers.base import CodeWorkerResult

    async def _empty_builder(self, req):
        return CodeWorkerResult(
            files=[],
            summary="empty",
            provider="anthropic",
            model="real-model",
            mock=False,
        )

    monkeypatch.setattr(
        "app.services.code_worker_service.ClaudeCodeWorker.run", _empty_builder
    )

    with SessionLocal() as db:
        project = ResearchBriefService(db).create_project(
            ProjectCreateIn(
                title="No fallback",
                student_name="Owner",
                mentor_name="Reviewer",
                research_direction="Do not mask empty real output.",
                target_venues=["Internal"],
            )
        )
        idea = Idea(
            id="idea_no_fallback",
            project_id=project.id,
            title="No fallback",
            summary="Empty output should fail.",
        )
        spec = ExperimentSpec(
            id="spec_no_fallback",
            project_id=project.id,
            idea_id=idea.id,
            version=1,
            hypothesis="h",
            problem_framing="p",
            target_metrics=["x"],
            dataset_assumptions="d",
            baseline="b",
            experiment_plan="e",
            constraints="c",
            success_criteria=[],
            stop_criteria=[],
            budget_estimate_usd=0.1,
        )
        db.add(idea)
        db.add(spec)
        db.commit()

    with SessionLocal() as db:
        with pytest.raises(RuntimeError, match="builder returned no files"):
            await CodeWorkerService(db).generate_code(
                spec=db.query(ExperimentSpec).filter(ExperimentSpec.id == "spec_no_fallback").first(),
                idea=db.query(Idea).filter(Idea.id == "idea_no_fallback").first(),
                run_id="run_no_fallback",
                worker="claude_code",
                seed=0,
                extra_instructions=None,
                provider_credential_id=None,
            )


@pytest.mark.asyncio
async def test_claude_code_worker_retries_empty_response(fresh_db, monkeypatch):
    from app.db.session import SessionLocal
    from app.config.model_policy import ModelConfig
    from app.providers.base import CompletionResult
    from app.workers.base import CodeWorkerRequest
    from app.workers.claude_code_worker import ClaudeCodeWorker

    calls = {"n": 0}

    class _FakeAdapter:
        async def complete(self, req):
            calls["n"] += 1
            if calls["n"] == 1:
                return CompletionResult(
                    provider="openai",
                    model="fake",
                    text='{"summary":"empty","files":[]}',
                )
            return CompletionResult(
                provider="openai",
                model="fake",
                text=(
                    '{"files":[{"path":"train.py","content":"print(\\"ok\\")"}],'
                    '"summary":"repaired"}'
                ),
            )

    class _FakeResolved:
        adapter = _FakeAdapter()

    class _FakeRouter:
        def resolve_with_policy(self, policy, credential_id=None):
            return _FakeResolved()

    monkeypatch.setattr(
        "app.workers.claude_code_worker.get_provider_router", lambda db: _FakeRouter()
    )
    monkeypatch.setattr(
        "app.workers.claude_code_worker.resolve_model_policy",
        lambda phase: ModelConfig(
            phase="code_generation",
            provider="openai",
            model="gpt-5.4",
            reasoning_effort="low",
            temperature=0.2,
            max_output_tokens=8000,
            timeout=180.0,
            policy_label="production",
        ),
    )

    with SessionLocal() as db:
        result = await ClaudeCodeWorker(db).run(
            CodeWorkerRequest(
                spec_id="spec_x",
                project_id="proj_x",
                idea_id="idea_x",
                hypothesis="h",
                experiment_plan="p",
                target_metrics=["acc"],
                baseline="b",
                constraints="c",
                dataset_assumptions="d",
                success_criteria=[],
                stop_criteria=[],
            )
        )

    assert calls["n"] == 2
    assert any(f["path"] == "train.py" for f in result.files)
    assert "repaired" in result.summary


@pytest.mark.asyncio
async def test_claude_code_worker_retries_reasoning_only_empty_with_reasoning_disabled(
    fresh_db, monkeypatch
):
    from app.db.session import SessionLocal
    from app.config.model_policy import ModelConfig
    from app.providers.base import CompletionResult
    from app.workers.base import CodeWorkerRequest
    from app.workers.claude_code_worker import ClaudeCodeWorker

    seen_efforts = []

    class _FakeAdapter:
        async def complete(self, req):
            seen_efforts.append(req.reasoning_effort)
            if len(seen_efforts) == 1:
                return CompletionResult(
                    provider="openai",
                    model="gpt-5",
                    text="",
                    usage={
                        "output_tokens": 400,
                        "output_tokens_details": {"reasoning_tokens": 400},
                    },
                    raw={"status": "completed", "output_item_types": ["reasoning"]},
                )
            return CompletionResult(
                provider="openai",
                model="gpt-5",
                text=(
                    '{"files":[{"path":"train.py","content":"print(\\"ok\\")"}],'
                    '"summary":"repaired"}'
                ),
                usage={
                    "output_tokens": 100,
                    "output_tokens_details": {"reasoning_tokens": 0},
                },
                raw={"status": "completed", "output_item_types": ["message"]},
            )

    class _FakeResolved:
        adapter = _FakeAdapter()

    class _FakeRouter:
        def resolve_with_policy(self, policy, credential_id=None):
            return _FakeResolved()

    monkeypatch.setattr(
        "app.workers.claude_code_worker.get_provider_router", lambda db: _FakeRouter()
    )
    monkeypatch.setattr(
        "app.workers.claude_code_worker.resolve_model_policy",
        lambda phase: ModelConfig(
            phase="code_generation",
            provider="openai",
            model="gpt-5.4",
            reasoning_effort="low",
            temperature=0.2,
            max_output_tokens=8000,
            timeout=180.0,
            policy_label="production",
        ),
    )

    with SessionLocal() as db:
        result = await ClaudeCodeWorker(db).run(
            CodeWorkerRequest(
                spec_id="spec_reasoning_retry",
                project_id="proj_reasoning_retry",
                idea_id="idea_reasoning_retry",
                hypothesis="h",
                experiment_plan="p",
                target_metrics=["acc"],
                baseline="b",
                constraints="c",
                dataset_assumptions="d",
                success_criteria=[],
                stop_criteria=[],
            )
        )

    assert seen_efforts[0] == "low"
    assert seen_efforts[1] is None
    assert any(f["path"] == "train.py" for f in result.files)
    assert result.diagnostics["initial_attempt"]["reasoning_only_empty"] is True


@pytest.mark.asyncio
async def test_nonzero_exit_with_metrics_counts_as_succeeded_valid(
    fresh_db, monkeypatch, tmp_path
):
    from app.core.models import ExperimentSpec, Idea
    from app.core.schemas import ProjectCreateIn, RunStartIn
    from app.db.session import SessionLocal
    from app.services import ExperimentRunnerService, ResearchBriefService

    workspace = tmp_path / "run_case"
    code_dir = workspace / "code"
    code_dir.mkdir(parents=True, exist_ok=True)
    (code_dir / "train.py").write_text("print('ok')\n", encoding="utf-8")
    metrics_dir = code_dir / "outputs"
    metrics_dir.mkdir(parents=True, exist_ok=True)
    (metrics_dir / "metrics.json").write_text(
        '{"overlap_results":{"0.5":{"0":0.9}},"threshold_met":false}\n',
        encoding="utf-8",
    )

    async def _fake_generate(self, **kwargs):
        return {
            "workspace_path": str(workspace),
            "code_hash": "abc",
            "provider": "openai",
            "model": "fake",
            "mock": False,
            "worker": "claude_code",
            "files": ["train.py"],
            "summary": "generated",
            "warnings": [],
            "assumptions": [],
            "dependencies": [],
            "used_fallback": False,
            "estimated_cost_usd": 0.1,
        }

    class _FakeSetup:
        ok = True
        skipped = True
        installed = []
        python_executable = "python"
        stdout = ""
        stderr = ""
        duration_s = 0.0

    class _FakeOutcome:
        exit_code = 3
        stdout = ""
        stderr = "criteria failed"
        timed_out = False
        duration_s = 0.1

    class _FakeRunner:
        async def prepare_python_env(self, **kwargs):
            return _FakeSetup()

        async def run_python(self, **kwargs):
            return _FakeOutcome()

    monkeypatch.setattr(
        "app.services.experiment_runner_service.CodeWorkerService.generate_code",
        _fake_generate,
    )
    monkeypatch.setattr(
        "app.services.experiment_runner_service.get_job_runner",
        lambda: _FakeRunner(),
    )

    with SessionLocal() as db:
        project = ResearchBriefService(db).create_project(
            ProjectCreateIn(
                title="Metrics-success project",
                student_name="Owner",
                mentor_name="Reviewer",
                research_direction="Use metrics even on negative hypothesis.",
                target_venues=["Internal"],
            )
        )
        idea = Idea(
            id="idea_metrics_success",
            project_id=project.id,
            title="Metrics success",
            summary="Negative result still valid.",
        )
        spec = ExperimentSpec(
            id="spec_metrics_success",
            project_id=project.id,
            idea_id=idea.id,
            version=1,
            hypothesis="h",
            problem_framing="p",
            target_metrics=["acc"],
            dataset_assumptions="d",
            baseline="b",
            experiment_plan="e",
            constraints="c",
            success_criteria=[],
            stop_criteria=[],
            budget_estimate_usd=0.1,
        )
        db.add(idea)
        db.add(spec)
        db.commit()

    with SessionLocal() as db:
        run = await ExperimentRunnerService(db).start_and_run(
            project.id, RunStartIn(spec_id="spec_metrics_success", worker="claude_code")
        )

    assert run.status == "succeeded"
    assert run.result_class == "succeeded_valid"
    assert run.metrics.get("overlap_results")


@pytest.mark.asyncio
async def test_timeout_boundary_with_valid_metrics_counts_as_succeeded_valid(
    fresh_db, monkeypatch, tmp_path
):
    from app.core.models import ExperimentSpec, Idea
    from app.core.schemas import ProjectCreateIn, RunStartIn
    from app.db.session import SessionLocal
    from app.services import ExperimentRunnerService, ResearchBriefService

    workspace = tmp_path / "timeout_boundary_case"
    code_dir = workspace / "code"
    code_dir.mkdir(parents=True, exist_ok=True)
    (code_dir / "train.py").write_text("print('ok')\n", encoding="utf-8")
    metrics_dir = code_dir / "outputs"
    metrics_dir.mkdir(parents=True, exist_ok=True)
    (metrics_dir / "metrics.json").write_text(
        json.dumps(
            {
                "baseline": {"accuracy": 0.75},
                "variant": {"accuracy": 0.78},
                "delta": {"accuracy": 0.03},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    async def _fake_generate(self, **kwargs):
        return {
            "workspace_path": str(workspace),
            "code_hash": "timeout-hash",
            "provider": "openai",
            "model": "fake",
            "mock": False,
            "worker": "claude_code",
            "files": ["train.py"],
            "summary": "generated",
            "warnings": [],
            "assumptions": [],
            "dependencies": [],
            "used_fallback": False,
            "estimated_cost_usd": 0.1,
        }

    class _FakeSetup:
        ok = True
        skipped = True
        installed = []
        python_executable = "python"
        stdout = ""
        stderr = ""
        duration_s = 0.0

    class _FakeOutcome:
        exit_code = 0
        stdout = '{"status":"finished at timeout boundary"}'
        stderr = ""
        timed_out = True
        duration_s = 60.01

    class _FakeRunner:
        async def prepare_python_env(self, **kwargs):
            return _FakeSetup()

        async def run_python(self, **kwargs):
            return _FakeOutcome()

    monkeypatch.setattr(
        "app.services.experiment_runner_service.CodeWorkerService.generate_code",
        _fake_generate,
    )
    monkeypatch.setattr(
        "app.services.experiment_runner_service.get_job_runner",
        lambda: _FakeRunner(),
    )

    with SessionLocal() as db:
        project = ResearchBriefService(db).create_project(
            ProjectCreateIn(
                title="Timeout boundary project",
                student_name="Owner",
                mentor_name="Reviewer",
                research_direction="Accept completed metrics at timeout boundary.",
                target_venues=["Internal"],
            )
        )
        idea = Idea(
            id="idea_timeout_boundary",
            project_id=project.id,
            title="Timeout boundary",
            summary="Metrics are available even though timeout flag is true.",
        )
        spec = ExperimentSpec(
            id="spec_timeout_boundary",
            project_id=project.id,
            idea_id=idea.id,
            version=1,
            hypothesis="h",
            problem_framing="p",
            target_metrics=["accuracy"],
            dataset_assumptions="d",
            baseline="b",
            experiment_plan="e",
            constraints="c",
            success_criteria=[],
            stop_criteria=[],
            budget_estimate_usd=0.1,
        )
        db.add(idea)
        db.add(spec)
        db.commit()

    with SessionLocal() as db:
        run = await ExperimentRunnerService(db).start_and_run(
            project.id,
            RunStartIn(spec_id="spec_timeout_boundary", worker="claude_code"),
        )

    assert run.status == "succeeded"
    assert run.result_class == "succeeded_valid"
    assert run.config["timeout_boundary"]["metrics_accepted"] is True
    assert "timeout boundary" in run.summary


@pytest.mark.asyncio
async def test_run_persists_config_updates_and_scrubbed_metrics(
    fresh_db, monkeypatch, tmp_path
):
    from app.core.models import ExperimentRun, ExperimentSpec, Idea
    from app.core.schemas import ProjectCreateIn, RunStartIn
    from app.db.session import SessionLocal
    from app.services import ExperimentRunnerService, ResearchBriefService

    workspace = tmp_path / "persist_case"
    code_dir = workspace / "code"
    code_dir.mkdir(parents=True, exist_ok=True)
    (code_dir / "train.py").write_text("print('ok')\n", encoding="utf-8")
    outputs_dir = code_dir / "outputs"
    outputs_dir.mkdir(parents=True, exist_ok=True)
    data_dir = outputs_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    metrics_path = outputs_dir / "metrics.json"
    metrics_path.write_text(
        json.dumps(
            {
                "baseline": {"score": 0.10},
                "variant": {"score": 0.25},
                "delta": {"score": 0.15},
                "artifacts": {
                    "data_dir": str(data_dir),
                    "metrics_path": str(metrics_path),
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    async def _fake_generate(self, **kwargs):
        return {
            "workspace_path": str(workspace),
            "code_hash": "persist-hash",
            "provider": "openai",
            "model": "fake",
            "mock": False,
            "worker": "claude_code",
            "files": ["train.py"],
            "summary": "generated",
            "warnings": [],
            "assumptions": [],
            "dependencies": ["numpy>=2.1"],
            "used_fallback": False,
            "estimated_cost_usd": 0.25,
            "diagnostics": {
                "initial_attempt": {
                    "reasoning_only_empty": True,
                    "output_item_types": ["reasoning"],
                }
            },
        }

    class _FakeSetup:
        ok = True
        skipped = False
        installed = ["numpy>=2.1"]
        python_executable = "python"
        stdout = "installed numpy"
        stderr = ""
        duration_s = 0.1

    class _FakeOutcome:
        exit_code = 0
        stdout = "done"
        stderr = ""
        timed_out = False
        duration_s = 0.2

    class _FakeRunner:
        async def prepare_python_env(self, **kwargs):
            return _FakeSetup()

        async def run_python(self, **kwargs):
            return _FakeOutcome()

    monkeypatch.setattr(
        "app.services.experiment_runner_service.CodeWorkerService.generate_code",
        _fake_generate,
    )
    monkeypatch.setattr(
        "app.services.experiment_runner_service.get_job_runner",
        lambda: _FakeRunner(),
    )

    with SessionLocal() as db:
        project = ResearchBriefService(db).create_project(
            ProjectCreateIn(
                title="Persist config project",
                student_name="Owner",
                mentor_name="Reviewer",
                research_direction="Persist runner metadata.",
                target_venues=["Internal"],
            )
        )
        idea = Idea(
            id="idea_persist_config",
            project_id=project.id,
            title="Persist config",
            summary="Persist config and scrub paths.",
        )
        spec = ExperimentSpec(
            id="spec_persist_config",
            project_id=project.id,
            idea_id=idea.id,
            version=1,
            hypothesis="h",
            problem_framing="p",
            target_metrics=["score"],
            dataset_assumptions="d",
            baseline="b",
            experiment_plan="e",
            constraints="c",
            success_criteria=[],
            stop_criteria=[],
            budget_estimate_usd=0.25,
        )
        db.add(idea)
        db.add(spec)
        db.commit()

    with SessionLocal() as db:
        run = await ExperimentRunnerService(db).start_and_run(
            project.id, RunStartIn(spec_id="spec_persist_config", worker="claude_code")
        )
        run_id = run.id

    with SessionLocal() as db:
        stored = db.query(ExperimentRun).filter(ExperimentRun.id == run_id).one()

    assert stored.config["dependencies"] == ["numpy>=2.1"]
    assert stored.config["environment_setup"]["installed"] == ["numpy>=2.1"]
    assert stored.config["code_worker"]["diagnostics"]["initial_attempt"][
        "reasoning_only_empty"
    ] is True
    assert stored.metrics["artifacts"]["data_dir"] == "outputs/data"
    assert stored.metrics["artifacts"]["metrics_path"] == "outputs/metrics.json"
    assert json.loads(metrics_path.read_text(encoding="utf-8"))["artifacts"] == {
        "data_dir": "outputs/data",
        "metrics_path": "outputs/metrics.json",
    }


@pytest.mark.asyncio
async def test_package_exports_owner_reviewer_and_scrubbed_paths(fresh_db):
    import zipfile

    from app.config import get_settings
    from app.core.enums import AuditKind, ReviewSeverity, ReviewState, ReviewerClass
    from app.core.models import AuditEvent, ExperimentRun, ReviewIssue
    from app.core.schemas import PackageCreateIn, ProjectCreateIn
    from app.db.session import SessionLocal
    from app.services import PackageService, ResearchBriefService
    from app.utils import new_id

    settings = get_settings()

    with SessionLocal() as db:
        project = ResearchBriefService(db).create_project(
            ProjectCreateIn(
                title="Portable package",
                student_name="Owner Name",
                mentor_name="Reviewer Name",
                research_direction="Export sanitized package data.",
                target_venues=["Internal"],
            )
        )
        workspace = settings.resolve_path(settings.workspaces_dir) / project.id / "run_export_case"
        outputs = workspace / "outputs"
        outputs.mkdir(parents=True, exist_ok=True)
        metrics_path = outputs / "metrics.json"
        metrics_path.write_text(
            json.dumps({"delta": {"score": 0.12}}) + "\n", encoding="utf-8"
        )

        db.add(
            ExperimentRun(
                id="run_export_case",
                project_id=project.id,
                spec_id="spec_export_case",
                idea_id="idea_export_case",
                workspace_path=str(workspace),
                status="succeeded",
                result_class="succeeded_valid",
                exit_code=0,
                seed=0,
                code_hash="abc",
                provider_routing={"provider": "openai"},
                config={"workspace": str(workspace / "code")},
                metrics={
                    "artifacts": {
                        "metrics_path": str(metrics_path),
                        "data_dir": str(outputs / "data"),
                    }
                },
                summary="ok",
                mock=False,
            )
        )
        db.add(
            ReviewIssue(
                id=new_id("rev"),
                project_id=project.id,
                draft_id=None,
                subject_kind="run",
                subject_id="run_export_case",
                reviewer_class=ReviewerClass.package.value,
                severity=ReviewSeverity.P1.value,
                state=ReviewState.waived.value,
                description=f"Absolute path leaked via {workspace}",
                evidence=f"path={metrics_path}",
                suggested_remediation=f"Use {workspace / 'outputs'} relatively.",
                resolution_note="waived for test",
                meta={"workspace_path": str(workspace)},
            )
        )
        db.add(
            AuditEvent(
                id=new_id("audit"),
                project_id=project.id,
                kind=AuditKind.run_finished.value,
                actor="system",
                subject_kind="run",
                subject_id="run_export_case",
                message=f"stored artifacts under {workspace}",
                payload={"workspace_path": str(workspace)},
            )
        )
        db.commit()

    with SessionLocal() as db:
        pkg = await PackageService(db).build(
            project.id,
            PackageCreateIn(allow_with_waived_p2=True, include_mock=True),
            require_approval=False,
        )

    with zipfile.ZipFile(pkg.zip_path) as zf:
        manifest = json.loads(zf.read("manifest.json"))
        project_doc = json.loads(zf.read("data/project.json"))
        runs_doc = json.loads(zf.read("data/runs.json"))
        issues_doc = json.loads(zf.read("data/review_issues.json"))
        audit_doc = json.loads(zf.read("data/audit.json"))
        readme = zf.read("README.md").decode("utf-8")
        payloads = [
            zf.read(name).decode("utf-8", "replace")
            for name in zf.namelist()
            if name.endswith(".json") or name == "README.md"
        ]

    assert manifest["project"]["owner_name"] == "Owner Name"
    assert manifest["project"]["reviewer_name"] == "Reviewer Name"
    assert "student_name" not in manifest["project"]
    assert "mentor_name" not in manifest["project"]
    assert project_doc["owner_name"] == "Owner Name"
    assert project_doc["reviewer_name"] == "Reviewer Name"
    assert "student_name" not in project_doc
    assert "mentor_name" not in project_doc
    assert runs_doc[0]["workspace_path"].startswith("workspaces/")
    assert runs_doc[0]["metrics"]["artifacts"]["metrics_path"].startswith("workspaces/")
    assert issues_doc[0]["description"].startswith("Absolute path leaked via workspaces/")
    assert "workspaces/" in issues_doc[0]["evidence"]
    workspace_audit = next(
        entry for entry in audit_doc if "workspace_path" in (entry.get("payload") or {})
    )
    assert workspace_audit["payload"]["workspace_path"].startswith("workspaces/")
    assert "Owner: Owner Name" in readme
    assert "Reviewer: Reviewer Name" in readme
    assert "Student:" not in readme
    assert "Mentor:" not in readme
    combined = "\n".join(payloads)
    assert "/mnt/c/" not in combined
    assert "\\Users\\" not in combined


def test_real_run_policy_overrides_use_low_code_generation_reasoning(monkeypatch):
    pipeline = importlib.import_module("run_real_pipeline")

    monkeypatch.delenv("RESEARCHOS_REASONING_CODE_GENERATION", raising=False)
    pipeline.apply_runtime_policy_overrides("gpt-test", "claude-test")

    assert os.environ["RESEARCHOS_REASONING_CODE_GENERATION"] == "low"


@pytest.mark.asyncio
async def test_draft_generation_replaces_placeholder_sections_with_full_prose(
    fresh_db, monkeypatch
):
    draft_mod = importlib.import_module("app.services.draft_service")
    from app.core.models import Claim, DraftSection, ExperimentRun
    from app.core.schemas import DraftGenerateIn, ProjectCreateIn
    from app.db.session import SessionLocal
    from app.providers.base import CompletionResult
    from app.services import DraftService, ResearchBriefService

    class _FakeAdapter:
        async def complete(self, req):
            return CompletionResult(
                provider="anthropic",
                model="fake-opus",
                text=json.dumps(
                    {
                        "sections": [
                            {
                                "key": "abstract",
                                "title": "Abstract",
                                "content": "This draft summarizes preliminary evidence.",
                                "claim_refs": ["claim_full_draft"],
                            },
                            {
                                "key": "discussion",
                                "title": "Discussion",
                                "content": "[PLACEHOLDER] Discussion awaiting review.",
                                "claim_refs": [],
                            },
                            {
                                "key": "limitations",
                                "title": "Limitations",
                                "content": "Limitations to be discussed by mentor team.",
                                "claim_refs": [],
                            },
                            {
                                "key": "conclusion",
                                "title": "Conclusion",
                                "content": "Conclusion awaiting mentor review.",
                                "claim_refs": [],
                            },
                        ]
                    }
                ),
            )

    class _FakeResolved:
        adapter = _FakeAdapter()

    class _FakeRouter:
        def resolve_with_policy(self, policy):
            return _FakeResolved()

    monkeypatch.setattr(draft_mod, "get_provider_router", lambda db: _FakeRouter())

    with SessionLocal() as db:
        project = ResearchBriefService(db).create_project(
            ProjectCreateIn(
                title="Full prose draft",
                student_name="Owner",
                mentor_name="Reviewer",
                research_direction="Study robust experiment drafting without placeholders.",
                target_venues=["Internal"],
            )
        )
        db.add(
            ExperimentRun(
                id="run_full_draft",
                project_id=project.id,
                spec_id="spec_full_draft",
                idea_id="idea_full_draft",
                workspace_path="workspaces/proj/run_full_draft",
                status="succeeded",
                result_class="succeeded_valid",
                exit_code=0,
                seed=0,
                code_hash="hash",
                provider_routing={"provider": "openai"},
                config={},
                metrics={"delta": {"score": 0.12}},
                summary="ok",
                mock=False,
            )
        )
        db.add(
            Claim(
                id="claim_full_draft",
                project_id=project.id,
                idea_id="idea_full_draft",
                run_id="run_full_draft",
                text="The exploratory run completed and produced usable evidence-backed outputs.",
                kind="qualitative",
                evidence_refs=[{"type": "run", "id": "run_full_draft"}],
                quantitative=False,
                value="",
                mock=False,
            )
        )
        db.commit()

    with SessionLocal() as db:
        draft = await DraftService(db).generate(
            project.id,
            DraftGenerateIn(extra_instructions="__skip_polish__"),
        )
        sections = (
            db.query(DraftSection)
            .filter(DraftSection.draft_id == draft.id)
            .all()
        )

    by_key = {section.key: section.content for section in sections}
    for key in ("discussion", "limitations", "conclusion"):
        lowered = by_key[key].lower()
        assert "[placeholder]" not in lowered
        assert "awaiting review" not in lowered
        assert "mentor review" not in lowered
        assert len(by_key[key].strip()) > 40


@pytest.mark.asyncio
async def test_draft_generation_retries_after_transient_provider_error(
    fresh_db, monkeypatch
):
    draft_mod = importlib.import_module("app.services.draft_service")
    from app.core.models import Draft
    from app.core.schemas import DraftGenerateIn, ProjectCreateIn
    from app.db.session import SessionLocal
    from app.providers.base import CompletionResult
    from app.services import DraftService, ResearchBriefService

    calls = {"n": 0}

    class _FlakyAdapter:
        async def complete(self, req):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("temporary network error")
            return CompletionResult(
                provider="anthropic",
                model="fake-opus",
                text=json.dumps(
                    {
                        "sections": [
                            {
                                "key": "abstract",
                                "title": "Abstract",
                                "content": "A complete retry-generated abstract.",
                                "claim_refs": [],
                            },
                            {
                                "key": "discussion",
                                "title": "Discussion",
                                "content": "The retry path returned a full section instead of placeholder text.",
                                "claim_refs": [],
                            },
                        ]
                    }
                ),
            )

    class _FakeResolved:
        adapter = _FlakyAdapter()

    class _FakeRouter:
        def resolve_with_policy(self, policy):
            return _FakeResolved()

    monkeypatch.setattr(draft_mod, "get_provider_router", lambda db: _FakeRouter())

    with SessionLocal() as db:
        project = ResearchBriefService(db).create_project(
            ProjectCreateIn(
                title="Draft retry project",
                student_name="Owner",
                mentor_name="Reviewer",
                research_direction="Retry transient draft-generation failures.",
                target_venues=["Internal"],
            )
        )

    with SessionLocal() as db:
        draft = await DraftService(db).generate(
            project.id,
            DraftGenerateIn(extra_instructions="__skip_polish__"),
        )
        stored = db.query(Draft).filter(Draft.id == draft.id).one()

    # The draft service generates per-section, so the total provider call
    # count depends on the section count. The invariant under test is that
    # a transient failure on the first attempt is followed by a retry —
    # i.e. at least 2 calls are observed and a draft is still produced.
    assert (
        calls["n"] >= 2
    ), f"expected at least one retry to fire after the transient error; got {calls['n']} calls"
    assert stored.meta["provider"] == "anthropic"
    assert stored.meta["model"] == "fake-opus"


@pytest.mark.asyncio
async def test_draft_fallback_uses_verdict_summary_and_salient_claims(
    fresh_db, monkeypatch
):
    draft_mod = importlib.import_module("app.services.draft_service")
    from app.core.enums import AuditKind
    from app.core.models import AuditEvent, Claim, DraftSection, ExperimentRun, Idea
    from app.core.schemas import DraftGenerateIn, ProjectCreateIn
    from app.db.session import SessionLocal
    from app.providers.base import CompletionResult
    from app.services import DraftService, ResearchBriefService
    from app.utils import new_id

    class _FakeAdapter:
        async def complete(self, req):
            return CompletionResult(
                provider="anthropic",
                model="fake-opus",
                text=json.dumps(
                    {
                        "sections": [
                            {
                                "key": "abstract",
                                "title": "Abstract",
                                "content": "[PLACEHOLDER] abstract awaiting review.",
                                "claim_refs": [],
                            },
                            {
                                "key": "discussion",
                                "title": "Discussion",
                                "content": "[PLACEHOLDER] discussion awaiting review.",
                                "claim_refs": [],
                            },
                        ]
                    }
                ),
            )

    class _FakeResolved:
        adapter = _FakeAdapter()

    class _FakeRouter:
        def resolve_with_policy(self, policy):
            return _FakeResolved()

    monkeypatch.setattr(draft_mod, "get_provider_router", lambda db: _FakeRouter())

    with SessionLocal() as db:
        project = ResearchBriefService(db).create_project(
            ProjectCreateIn(
                title="Salient draft project",
                student_name="Owner",
                mentor_name="Reviewer",
                research_direction="Summarize mixed experimental evidence clearly.",
                target_venues=["Internal"],
            )
        )
        db.add(
            Idea(
                id="idea_salient_draft",
                project_id=project.id,
                title="Loss-weighted resampling",
                summary="Resample examples in proportion to recent loss.",
            )
        )
        db.add(
            ExperimentRun(
                id="run_salient_draft",
                project_id=project.id,
                spec_id="spec_salient_draft",
                idea_id="idea_salient_draft",
                workspace_path="workspaces/proj/run_salient_draft",
                status="succeeded",
                result_class="succeeded_valid",
                exit_code=0,
                seed=0,
                code_hash="hash",
                provider_routing={"provider": "openai"},
                config={},
                metrics={"delta": {"accuracy": 0.02}},
                summary="ok",
                mock=False,
            )
        )
        db.add_all(
            [
                Claim(
                    id="claim_completion",
                    project_id=project.id,
                    idea_id="idea_salient_draft",
                    run_id="run_salient_draft",
                    text="Run completed successfully (status: succeeded; exit_code: 0).",
                    kind="qualitative",
                    evidence_refs=[{"type": "run", "id": "run_salient_draft"}],
                    quantitative=False,
                    value="",
                    mock=False,
                ),
                Claim(
                    id="claim_accuracy",
                    project_id=project.id,
                    idea_id="idea_salient_draft",
                    run_id="run_salient_draft",
                    text="Variant accuracy mean is higher than baseline (0.78 vs 0.75).",
                    kind="quantitative",
                    evidence_refs=[{"type": "run", "id": "run_salient_draft"}],
                    quantitative=True,
                    value="0.78",
                    mock=False,
                ),
                Claim(
                    id="claim_calibration",
                    project_id=project.id,
                    idea_id="idea_salient_draft",
                    run_id="run_salient_draft",
                    text="Calibration worsened despite the accuracy gain, so the run remained inconclusive.",
                    kind="qualitative",
                    evidence_refs=[{"type": "run", "id": "run_salient_draft"}],
                    quantitative=False,
                    value="",
                    mock=False,
                ),
            ]
        )
        db.add(
            AuditEvent(
                id=new_id("audit"),
                project_id=project.id,
                kind=AuditKind.result_validated.value,
                actor="system",
                subject_kind="run",
                subject_id="run_salient_draft",
                message="Analyzed run",
                payload={
                    "verdict": "inconclusive",
                    "delta": {"accuracy": 0.02},
                    "claim_ids": ["claim_completion", "claim_accuracy", "claim_calibration"],
                },
            )
        )
        db.commit()

    with SessionLocal() as db:
        draft = await DraftService(db).generate(
            project.id,
            DraftGenerateIn(extra_instructions="__skip_polish__"),
        )
        sections = (
            db.query(DraftSection)
            .filter(DraftSection.draft_id == draft.id)
            .all()
        )

    by_key = {section.key: section.content for section in sections}
    assert "inconclusive" in by_key["abstract"].lower()
    assert "internal draft" not in by_key["abstract"].lower()
    assert "decision document" not in by_key["abstract"].lower()
    assert "run completed successfully" not in by_key["abstract"].lower()
    assert "accuracy mean is higher than baseline" in by_key["abstract"].lower()
    assert "this draft frames" not in by_key["introduction"].lower()
    assert "this version of the draft" not in by_key["method"].lower()
    assert "this draft is limited" not in by_key["limitations"].lower()
    assert "no run in the current batch crossed the threshold" in by_key["discussion"].lower()
    assert "calibration" in by_key["discussion"].lower()
    assert "accuracy and stability" in by_key["results"].lower()
    assert "representative evidence-backed findings" not in by_key["results"].lower()
    accuracy_paragraph = by_key["results"].split("\n\n", 1)[0].lower()
    assert "calibration worsened" not in accuracy_paragraph
    assert "loss-weighted resampling" in by_key["results"].lower()
    assert "run_salient_draft" not in by_key["results"].lower()
    assert "pilot evidence does not yet support" in by_key["conclusion"].lower()


@pytest.mark.asyncio
async def test_draft_generation_uses_openai_primary_provider(
    fresh_db, monkeypatch
):
    draft_mod = importlib.import_module("app.services.draft_service")
    from app.config import reset_settings_cache
    from app.core.models import Draft
    from app.core.schemas import DraftGenerateIn, ProjectCreateIn
    from app.db.session import SessionLocal
    from app.providers.base import CompletionResult
    from app.services import DraftService, ResearchBriefService

    monkeypatch.setenv("RESEARCHOS_RUN_MODE", "production")
    reset_settings_cache()

    class _OpenAIAdapter:
        async def complete(self, req):
            return CompletionResult(
                provider="openai",
                model="gpt-5.4-pro",
                text=json.dumps(
                    {
                        "sections": [
                            {
                                "key": "abstract",
                                "title": "Abstract",
                                "content": "OpenAI produced the backup draft.",
                                "claim_refs": [],
                            },
                            {
                                "key": "discussion",
                                "title": "Discussion",
                                "content": "The secondary provider fallback returned full prose.",
                                "claim_refs": [],
                            },
                        ]
                    }
                ),
            )

    class _FakeResolved:
        def __init__(self, adapter):
            self.adapter = adapter

    class _FakeRouter:
        def resolve_with_policy(self, policy):
            if policy.provider == "openai":
                return _FakeResolved(_OpenAIAdapter())
            raise AssertionError(f"unexpected provider {policy.provider}")

    monkeypatch.setattr(draft_mod, "get_provider_router", lambda db: _FakeRouter())

    with SessionLocal() as db:
        project = ResearchBriefService(db).create_project(
            ProjectCreateIn(
                title="Draft fallback project",
                student_name="Owner",
                mentor_name="Reviewer",
                research_direction="Fallback to a secondary LLM provider.",
                target_venues=["Internal"],
            )
        )

    with SessionLocal() as db:
        draft = await DraftService(db).generate(
            project.id,
            DraftGenerateIn(extra_instructions="__skip_polish__"),
        )
        stored = db.query(Draft).filter(Draft.id == draft.id).one()

    assert stored.meta["provider"] == "openai"
    assert stored.meta["model"] == "gpt-5.4-pro"
    assert stored.meta["provider_fallback"] is None
    reset_settings_cache()
