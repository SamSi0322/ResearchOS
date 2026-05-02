"""Tests for the canonical model policy.

These verify:

* the production table matches the documented intent (OpenAI-only,
  gpt-5.4-pro for critical phases, xhigh except the code builder)
* smoke policy swaps in the cheap smoke models and drops reasoning / thinking
* mock policy routes every phase through the mock adapter
* per-phase env overrides win
* Anthropic adapter emits ``thinking={"type": "enabled"}`` + temperature=1
* OpenAI adapter emits ``reasoning.effort`` with xhigh mapped to "high" on wire
* router ``resolve_with_policy`` picks a credential for the policy's provider
  and falls back to mock when none exists
* package manifest carries the policy snapshot
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


# ---- production table ----------------------------------------------------


def test_production_policy_matches_documented_intent(fresh_db):
    from app.config import Phase, RunMode, resolve_model_policy

    pro = "gpt-5.4-pro"

    assertions = [
        (Phase.idea_generation, "openai", pro, "xhigh", None),
        (Phase.idea_ranking, "openai", pro, "xhigh", None),
        (Phase.spec_generation, "openai", pro, "xhigh", None),
        (Phase.code_generation, "openai", pro, "low", None),
        (Phase.code_review, "openai", pro, "xhigh", None),
        (Phase.result_analysis, "openai", pro, "xhigh", None),
        (Phase.draft_generation, "openai", pro, "xhigh", None),
        (Phase.draft_polish, "openai", pro, "xhigh", None),
        (Phase.manuscript_review, "openai", pro, "xhigh", None),
    ]
    for phase, provider, model, effort, thinking in assertions:
        cfg = resolve_model_policy(phase, mode=RunMode.production)
        assert cfg.provider == provider, f"{phase.value}: provider"
        assert cfg.model == model, f"{phase.value}: model"
        assert cfg.reasoning_effort == effort, f"{phase.value}: effort"
        assert cfg.thinking_mode == thinking, f"{phase.value}: thinking"
        assert cfg.policy_label == "production"


def test_gpt_5_4_pro_is_used_for_critical_production_phases(fresh_db):
    from app.config import Phase, RunMode, resolve_model_policy

    pro_phases = []
    for p in Phase:
        cfg = resolve_model_policy(p, mode=RunMode.production)
        if cfg.model == "gpt-5.4-pro":
            pro_phases.append(p.value)
    assert pro_phases == [p.value for p in Phase]


def test_code_generation_timeout_is_extended_for_real_builder_calls(fresh_db):
    from app.config import Phase, RunMode, resolve_model_policy

    cfg = resolve_model_policy(Phase.code_generation, mode=RunMode.production)
    assert cfg.timeout == pytest.approx(3600.0)


# ---- smoke table ---------------------------------------------------------


def test_smoke_policy_uses_cheap_models_and_drops_reasoning(fresh_db, monkeypatch):
    from app.config import Phase, RunMode, resolve_model_policy

    monkeypatch.setenv("RESEARCHOS_OPENAI_SMOKE_MODEL", "gpt-4.1-mini")
    monkeypatch.setenv("RESEARCHOS_ANTHROPIC_SMOKE_MODEL", "claude-haiku-4-5-20251001")
    from app.config import reset_settings_cache

    reset_settings_cache()

    for phase in Phase:
        cfg = resolve_model_policy(phase, mode=RunMode.smoke)
        assert cfg.policy_label == "smoke"
        assert cfg.thinking_mode is None, f"{phase}: thinking must be off in smoke"
        # Smoke models do not support reasoning.effort on the wire, so the
        # policy drops it for every phase.
        assert cfg.reasoning_effort is None
        if cfg.provider == "openai":
            assert cfg.model == "gpt-4.1-mini"
        elif cfg.provider == "anthropic":
            assert cfg.model == "claude-haiku-4-5-20251001"


# ---- mock table ----------------------------------------------------------


def test_mock_policy_uses_mock_adapter_for_every_phase(fresh_db):
    from app.config import Phase, RunMode, resolve_model_policy

    for phase in Phase:
        cfg = resolve_model_policy(phase, mode=RunMode.mock)
        assert cfg.provider == "mock"
        assert cfg.model == "mock-1"
        assert cfg.policy_label == "mock"
        assert cfg.thinking_mode is None
        assert cfg.reasoning_effort is None


# ---- env overrides -------------------------------------------------------


def test_env_override_pins_model_per_phase(fresh_db, monkeypatch):
    from app.config import Phase, RunMode, resolve_model_policy, reset_settings_cache

    monkeypatch.setenv("RESEARCHOS_MODEL_CODE_GENERATION", "gpt-5.4-turbo")
    monkeypatch.setenv("RESEARCHOS_REASONING_CODE_GENERATION", "medium")
    monkeypatch.setenv("RESEARCHOS_TEMP_CODE_GENERATION", "0.05")
    reset_settings_cache()

    cfg = resolve_model_policy(Phase.code_generation, mode=RunMode.production)
    assert cfg.model == "gpt-5.4-turbo"
    assert cfg.reasoning_effort == "medium"
    assert cfg.temperature == pytest.approx(0.05)


# ---- adapter wire-level behaviour ---------------------------------------


def test_openai_effort_wire_maps_xhigh_to_high(fresh_db):
    from app.config.model_policy import ModelConfig

    cfg = ModelConfig(phase="code_review", provider="openai", model="gpt-5.4", reasoning_effort="xhigh")
    assert cfg.openai_effort_wire() == "high"
    cfg2 = ModelConfig(phase="idea_ranking", provider="openai", model="gpt-5.4-pro", reasoning_effort="high")
    assert cfg2.openai_effort_wire() == "high"
    cfg3 = ModelConfig(phase="idea_ranking", provider="openai", model="gpt-5.4-pro", reasoning_effort=None)
    assert cfg3.openai_effort_wire() is None


@pytest.mark.asyncio
async def test_openai_adapter_sends_reasoning_effort():
    """The OpenAI adapter must include ``reasoning.effort`` when asked."""
    from app.providers.base import CompletionRequest
    from app.providers.openai_adapter import OpenAIProvider

    captured = {}

    class _FakeResp:
        status_code = 200
        text = "{}"

        def json(self):
            return {
                "id": "resp_test",
                "output": [
                    {"type": "message", "content": [{"type": "output_text", "text": "OK"}]}
                ],
                "usage": {},
                "status": "completed",
            }

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, headers=None, json=None):
            captured["url"] = url
            captured["body"] = json
            return _FakeResp()

    adapter = OpenAIProvider(api_key="sk-proj-test", model="gpt-5.4")
    with patch("app.providers.openai_adapter.httpx.AsyncClient", return_value=_FakeClient()):
        await adapter.complete(
            CompletionRequest(
                prompt="hi",
                reasoning_effort="xhigh",
                phase="code_review",
                policy_label="production",
            )
        )
    assert captured["body"]["reasoning"] == {"effort": "high"}
    assert captured["body"]["model"] == "gpt-5.4"


@pytest.mark.asyncio
async def test_anthropic_adapter_emits_adaptive_thinking():
    """Adaptive thinking must send ``thinking.enabled`` and temperature=1."""
    from app.providers.anthropic_adapter import AnthropicProvider
    from app.providers.base import CompletionRequest

    captured = {}

    class _FakeResp:
        status_code = 200
        text = "{}"

        def json(self):
            return {
                "id": "msg_test",
                "content": [{"type": "text", "text": "OK"}],
                "usage": {},
                "stop_reason": "end_turn",
            }

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, headers=None, json=None):
            captured["body"] = json
            return _FakeResp()

    adapter = AnthropicProvider(api_key="sk-ant-api03-test", model="claude-opus-4-7")
    with patch("app.providers.anthropic_adapter.httpx.AsyncClient", return_value=_FakeClient()):
        await adapter.complete(
            CompletionRequest(
                prompt="hi",
                temperature=0.3,  # should be overridden to 1.0
                thinking_mode="adaptive",
                phase="draft_generation",
                policy_label="production",
            )
        )
    body = captured["body"]
    # type must be "enabled" and budget_tokens must be < max_tokens.
    assert body["thinking"]["type"] == "enabled"
    assert isinstance(body["thinking"].get("budget_tokens"), int)
    assert body["thinking"]["budget_tokens"] < body["max_tokens"]
    # Anthropic enforces temperature=1 when thinking is enabled; lower
    # values 400 on the live API.
    assert body["temperature"] == pytest.approx(1.0)
    assert body["model"] == "claude-opus-4-7"


# ---- router policy resolution -------------------------------------------


def test_router_resolve_with_policy_uses_mock_for_mock_policy(fresh_db):
    from app.config import Phase, RunMode, resolve_model_policy
    from app.db.session import SessionLocal
    from app.providers.router import get_provider_router

    policy = resolve_model_policy(Phase.idea_generation, mode=RunMode.mock)
    with SessionLocal() as db:
        resolved = get_provider_router(db).resolve_with_policy(policy)
    assert resolved.provider == "mock"
    assert resolved.model == "mock-1"


def test_router_resolve_with_policy_falls_back_when_no_credential(fresh_db):
    """If no OpenAI credential is stored, the router uses mock instead of
    exploding - the operator sees a clear log instead of a 500."""
    from app.config import Phase, RunMode, resolve_model_policy
    from app.db.session import SessionLocal
    from app.providers.router import get_provider_router

    policy = resolve_model_policy(Phase.code_generation, mode=RunMode.production)
    with SessionLocal() as db:
        resolved = get_provider_router(db).resolve_with_policy(policy)
    assert resolved.provider == "mock"


def test_router_resolve_with_policy_picks_openai_credential(fresh_db):
    from app.config import Phase, RunMode, resolve_model_policy
    from app.core.enums import ProviderName
    from app.core.schemas import ProviderCredentialIn
    from app.db.session import SessionLocal
    from app.providers.router import get_provider_router
    from app.services import ProviderSecretService

    with SessionLocal() as db:
        ProviderSecretService(db).add(
            ProviderCredentialIn(
                provider=ProviderName.openai.value,
                label="code",
                api_key="sk-proj-fake-for-policy-test",
                default_model="gpt-4.1-mini",
            )
        )

    policy = resolve_model_policy(Phase.code_generation, mode=RunMode.production)
    with SessionLocal() as db:
        resolved = get_provider_router(db).resolve_with_policy(policy)
    assert resolved.provider == "openai"
    # Adapter.model is overridden with the policy's current Pro model. The
    # alias layer is still available for future provider renames, but should
    # not hide OpenAI Pro usage in the production path.
    from app.config.model_alias import resolve_model_alias

    assert resolved.model == resolve_model_alias("gpt-5.4-pro")
    assert resolved.model == "gpt-5.4-pro"


# ---- manifest carries the policy ---------------------------------------


@pytest.mark.asyncio
async def test_package_manifest_records_model_policy(fresh_db):
    """After building a package in mock mode, the manifest's ``model_policy``
    block reports the current run_mode and the per-phase model map."""
    import json
    import zipfile

    from app.core.schemas import (
        DraftGenerateIn,
        IdeaGenerateIn,
        PackageCreateIn,
        ProjectCreateIn,
        ProviderCredentialIn,
        RunStartIn,
        SpecGenerateIn,
        ReviewRunIn,
    )
    from app.db.session import SessionLocal
    from app.services import (
        DraftService,
        ExperimentRunnerService,
        IdeaGenerationService,
        PackageService,
        ProviderSecretService,
        ResearchBriefService,
        ResultAnalysisService,
        ReviewService,
        SpecService,
    )

    with SessionLocal() as db:
        ProviderSecretService(db).add(
            ProviderCredentialIn(
                provider="mock", label="t", api_key="mock-policy", is_default=True
            )
        )
        project = ResearchBriefService(db).create_project(
            ProjectCreateIn(
                title="policy-in-manifest",
                student_name="x",
                mentor_name="x",
                research_direction="test",
            )
        )
    with SessionLocal() as db:
        ideas = await IdeaGenerationService(db).generate(
            project.id, IdeaGenerateIn(count=1)
        )
    with SessionLocal() as db:
        spec = await SpecService(db).generate(
            project.id, SpecGenerateIn(idea_id=ideas[0].id)
        )
    with SessionLocal() as db:
        run = await ExperimentRunnerService(db).start_and_run(
            project.id, RunStartIn(spec_id=spec.id, worker="claude_code", seed=0)
        )
    with SessionLocal() as db:
        await ResultAnalysisService(db).analyze(run.id)
    with SessionLocal() as db:
        await DraftService(db).generate(
            project.id,
            DraftGenerateIn(extra_instructions="__skip_polish__"),
        )
    with SessionLocal() as db:
        await ReviewService(db).run_reviewers(project.id, ReviewRunIn())
    with SessionLocal() as db:
        pkg = await PackageService(db).build(
            project.id, PackageCreateIn(allow_with_waived_p2=True, include_mock=True)
        )

    with zipfile.ZipFile(pkg.zip_path) as zf:
        manifest = json.loads(zf.read("manifest.json"))
    mp = manifest.get("model_policy") or {}
    # In the test env run_mode comes from RESEARCHOS_DEFAULT_PROVIDER=mock.
    assert mp.get("run_mode") in {"mock", "production", "smoke"}
    phases = mp.get("phases") or {}
    # The 9 policy phases must all appear in the manifest.
    for needed in (
        "idea_generation",
        "idea_ranking",
        "spec_generation",
        "code_generation",
        "code_review",
        "result_analysis",
        "draft_generation",
        "draft_polish",
        "manuscript_review",
    ):
        assert needed in phases, f"missing phase {needed} in manifest"
