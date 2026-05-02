"""Canonical model policy for ResearchOS.

Single source of truth for "which provider + which model + what reasoning
effort" for every pipeline phase, stratified by run mode
(production / smoke / mock). Services MUST NOT hardcode model ids or
reasoning choices - they call ``resolve_model_policy(phase)`` and use the
returned ``ModelConfig``.

Production intent (matches ``docs/model-policy.md``):

    | phase               | provider   | model             | mode/effort |
    |---------------------|------------|-------------------|-------------|
    | idea_generation     | openai     | gpt-5.4-pro       | xhigh (reasoning)   |
    | idea_ranking        | openai     | gpt-5.4-pro       | xhigh (reasoning)   |
    | spec_generation     | openai     | gpt-5.4-pro       | xhigh (reasoning)   |
    | code_generation     | openai     | gpt-5.4-pro       | low (reasoning)     |
    | code_review         | openai     | gpt-5.4-pro       | xhigh (reasoning)   |
    | result_analysis     | openai     | gpt-5.4-pro       | xhigh (reasoning)   |
    | draft_generation    | openai     | gpt-5.4-pro       | xhigh (reasoning)   |
    | draft_polish        | openai     | gpt-5.4-pro       | xhigh (reasoning)   |
    | manuscript_review   | openai     | gpt-5.4-pro       | xhigh (reasoning)   |

Smoke policy uses the cheap models configured in settings and drops
reasoning / thinking effort. Mock policy routes every phase through the
deterministic mock adapter so no network call fires.

Notes on providers:

* The production path is OpenAI-only because Anthropic has repeatedly
  fallen back during real draft runs. If an operator wants a future
  Anthropic split, use the per-phase env overrides below.
* OpenAI GPT-5.x / GPT-5.x-pro supports a ``reasoning.effort`` field on
  the Responses API. Our "xhigh" internal tier maps to the API's "high"
  string while the label stays "xhigh" for reporting / auditing.

Overrides:

Each phase honours ``RESEARCHOS_MODEL_<PHASE>``,
``RESEARCHOS_REASONING_<PHASE>``, and ``RESEARCHOS_TEMP_<PHASE>`` env
vars. Example: ``RESEARCHOS_MODEL_CODE_GENERATION=gpt-5.4-turbo``. A
blanket ``RESEARCHOS_RUN_MODE=production|smoke|mock`` short-circuits
which table we read from.

``RESEARCHOS_OPENAI_PRO_MODEL`` controls the default pro model used for
critical production phases. It currently defaults to ``gpt-5.4-pro`` because
the configured API credentials expose that model; set it to ``gpt-5.5-pro``
once the deployed API key actually lists that id.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any

from app.config.settings import get_settings


class RunMode(str, Enum):
    production = "production"
    smoke = "smoke"
    mock = "mock"


class Phase(str, Enum):
    idea_generation = "idea_generation"
    idea_ranking = "idea_ranking"
    spec_generation = "spec_generation"
    code_generation = "code_generation"
    code_review = "code_review"
    result_analysis = "result_analysis"
    draft_generation = "draft_generation"
    draft_polish = "draft_polish"
    manuscript_review = "manuscript_review"


class ReasoningEffort(str, Enum):
    minimal = "minimal"
    low = "low"
    medium = "medium"
    high = "high"
    # Internal-only tier for "as hard as the provider allows". At the API
    # level we send "high" - this is about expressing policy intent.
    xhigh = "xhigh"


# OpenAI Responses API only understands minimal|low|medium|high today. We
# map our internal xhigh intent to "high" on the wire and keep the
# internal label intact for audit records.
_OPENAI_EFFORT_WIRE = {
    "minimal": "minimal",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "high",
}


@dataclass
class ModelConfig:
    """Resolved policy for a single call site."""

    phase: str
    provider: str                          # "anthropic" | "openai" | "mock"
    model: str
    reasoning_effort: str | None = None    # Phase-local label (incl. "xhigh")
    thinking_mode: str | None = None       # "adaptive" | None
    temperature: float = 0.2
    max_output_tokens: int = 2000
    timeout: float = 60.0
    policy_label: str = "production"       # production | smoke | mock
    extra: dict[str, Any] = field(default_factory=dict)

    # --- wire-level helpers -----------------------------------------

    def openai_effort_wire(self) -> str | None:
        """What to send to OpenAI's ``reasoning.effort`` field."""
        if not self.reasoning_effort:
            return None
        return _OPENAI_EFFORT_WIRE.get(self.reasoning_effort, "high")

    def anthropic_thinking_enabled(self) -> bool:
        return self.thinking_mode == "adaptive"

    def as_metadata(self) -> dict[str, Any]:
        """Compact dict for audit logs / manifests / run metadata."""
        return {
            "phase": self.phase,
            "provider": self.provider,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "thinking_mode": self.thinking_mode,
            "policy": self.policy_label,
        }


# ---------------------------------------------------------------------------
# Production policy (source of truth)
# ---------------------------------------------------------------------------


def _production_table() -> dict[Phase, ModelConfig]:
    """Built fresh each call so env overrides are picked up at runtime."""
    pro_model = os.environ.get("RESEARCHOS_OPENAI_PRO_MODEL", "gpt-5.4-pro").strip()
    pro_timeout = _env_float("RESEARCHOS_PRO_REQUEST_TIMEOUT", 7200.0)
    builder_timeout = _env_float("RESEARCHOS_BUILDER_REQUEST_TIMEOUT", 3600.0)
    draft_max_tokens = int(_env_float("RESEARCHOS_PRO_DRAFT_MAX_OUTPUT_TOKENS", 64000.0))
    polish_max_tokens = int(_env_float("RESEARCHOS_PRO_POLISH_MAX_OUTPUT_TOKENS", 32000.0))
    base: dict[Phase, ModelConfig] = {
        Phase.idea_generation: ModelConfig(
            phase=Phase.idea_generation.value,
            provider="openai",
            model=pro_model,
            reasoning_effort="xhigh",
            temperature=0.9,
            max_output_tokens=4000,
            timeout=pro_timeout,
        ),
        Phase.idea_ranking: ModelConfig(
            phase=Phase.idea_ranking.value,
            provider="openai",
            model=pro_model,
            reasoning_effort="xhigh",
            temperature=0.2,
            max_output_tokens=3000,
            timeout=pro_timeout,
        ),
        Phase.spec_generation: ModelConfig(
            phase=Phase.spec_generation.value,
            provider="openai",
            model=pro_model,
            reasoning_effort="xhigh",
            temperature=0.3,
            max_output_tokens=2500,
            timeout=pro_timeout,
        ),
        # Keep builder reasoning intentionally low. In real runs the OpenAI
        # code builder was occasionally spending all output tokens on
        # reasoning and returning 200 OK with zero final text. The reviewer
        # phase remains the deep/xhigh pass; builder priority is reliable
        # runnable output.
        Phase.code_generation: ModelConfig(
            phase=Phase.code_generation.value,
            provider="openai",
            model=pro_model,
            reasoning_effort="low",
            temperature=0.2,
            max_output_tokens=8000,
            timeout=builder_timeout,
        ),
        Phase.code_review: ModelConfig(
            phase=Phase.code_review.value,
            provider="openai",
            model=pro_model,
            reasoning_effort="xhigh",
            temperature=0.2,
            max_output_tokens=8000,
            timeout=pro_timeout,
        ),
        Phase.result_analysis: ModelConfig(
            phase=Phase.result_analysis.value,
            provider="openai",
            model=pro_model,
            reasoning_effort="xhigh",
            temperature=0.2,
            max_output_tokens=1500,
            timeout=pro_timeout,
        ),
        Phase.draft_generation: ModelConfig(
            phase=Phase.draft_generation.value,
            provider="openai",
            model=pro_model,
            reasoning_effort="xhigh",
            temperature=0.3,
            max_output_tokens=draft_max_tokens,
            timeout=pro_timeout,
        ),
        Phase.draft_polish: ModelConfig(
            phase=Phase.draft_polish.value,
            provider="openai",
            model=pro_model,
            reasoning_effort="xhigh",
            temperature=0.2,
            max_output_tokens=polish_max_tokens,
            timeout=pro_timeout,
        ),
        Phase.manuscript_review: ModelConfig(
            phase=Phase.manuscript_review.value,
            provider="openai",
            model=pro_model,
            reasoning_effort="xhigh",
            temperature=0.2,
            max_output_tokens=3500,
            timeout=pro_timeout,
        ),
    }

    # Per-phase env overrides.
    for phase, cfg in base.items():
        model_env = f"RESEARCHOS_MODEL_{phase.value.upper()}"
        effort_env = f"RESEARCHOS_REASONING_{phase.value.upper()}"
        temp_env = f"RESEARCHOS_TEMP_{phase.value.upper()}"
        if os.environ.get(model_env):
            base[phase] = replace(cfg, model=os.environ[model_env].strip())
            cfg = base[phase]
        if os.environ.get(effort_env):
            base[phase] = replace(cfg, reasoning_effort=os.environ[effort_env].strip().lower())
            cfg = base[phase]
        if os.environ.get(temp_env):
            try:
                base[phase] = replace(cfg, temperature=float(os.environ[temp_env]))
            except ValueError:
                pass
    return base


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _smoke_table() -> dict[Phase, ModelConfig]:
    """Smoke policy: cheap models, short outputs, low / no reasoning effort."""
    settings = get_settings()
    openai_model = settings.openai_smoke_model
    anthropic_model = settings.anthropic_smoke_model
    smoke_max = int(settings.smoke_max_tokens)
    smoke_timeout = float(settings.smoke_request_timeout)
    base: dict[Phase, ModelConfig] = {}
    prod = _production_table()
    for phase, cfg in prod.items():
        provider = cfg.provider
        if provider == "openai":
            model = openai_model
        elif provider == "anthropic":
            model = anthropic_model
        else:
            model = cfg.model
        base[phase] = ModelConfig(
            phase=cfg.phase,
            provider=provider,
            model=model,
            # Smoke drops reasoning effort entirely. The cheap smoke models
            # (gpt-4.1-mini, Claude Haiku) reject ``reasoning.effort`` on
            # the wire because it is a gpt-5 / o-series feature; we leave it
            # off here rather than trying to downgrade.
            reasoning_effort=None,
            thinking_mode=None,
            temperature=cfg.temperature,
            max_output_tokens=min(cfg.max_output_tokens, smoke_max),
            timeout=smoke_timeout,
            policy_label="smoke",
        )
    return base


def _mock_table() -> dict[Phase, ModelConfig]:
    prod = _production_table()
    return {
        phase: ModelConfig(
            phase=cfg.phase,
            provider="mock",
            model="mock-1",
            reasoning_effort=None,
            thinking_mode=None,
            temperature=cfg.temperature,
            max_output_tokens=min(cfg.max_output_tokens, 1200),
            timeout=cfg.timeout,
            policy_label="mock",
        )
        for phase, cfg in prod.items()
    }


# ---------------------------------------------------------------------------
# resolve
# ---------------------------------------------------------------------------


def _normalize_mode(mode: RunMode | str | None) -> RunMode:
    """Resolve the active run mode.

    Precedence (highest wins):
      1. explicit ``mode`` argument (tests use this);
      2. ``RESEARCHOS_RUN_MODE`` set to production / smoke / mock;
      3. ``RESEARCHOS_SMOKE_MODE=true`` implies smoke;
      4. default: production.

    Note that ``RESEARCHOS_DEFAULT_PROVIDER=mock`` is a *router* default
    for credential fallback, not a policy mode. It does not downgrade the
    whole pipeline to mock - a pure-mock run must set
    ``RESEARCHOS_RUN_MODE=mock``.
    """
    if isinstance(mode, RunMode):
        return mode
    if mode:
        try:
            return RunMode(mode)
        except ValueError:
            pass
    settings = get_settings()
    # ``smoke_mode=true`` is a deliberate "make this cheap" flag and wins over
    # the settings-default RUN_MODE. An operator who genuinely wants
    # production while smoke_mode is on can set RESEARCHOS_RUN_MODE=mock or
    # disable smoke_mode.
    if getattr(settings, "smoke_mode", False):
        return RunMode.smoke
    configured = (getattr(settings, "run_mode", None) or "").strip().lower()
    if configured in {"mock", "smoke", "production"}:
        return RunMode(configured)
    return RunMode.production


def active_run_mode() -> RunMode:
    return _normalize_mode(None)


def resolve_model_policy(
    phase: Phase | str, *, mode: RunMode | str | None = None
) -> ModelConfig:
    """Return the ``ModelConfig`` a caller should use for ``phase``.

    ``mode`` may be passed explicitly (useful for tests); otherwise the
    current settings + env decide.
    """
    if not isinstance(phase, Phase):
        phase = Phase(phase)
    effective = _normalize_mode(mode)
    if effective is RunMode.mock:
        table = _mock_table()
    elif effective is RunMode.smoke:
        table = _smoke_table()
    else:
        table = _production_table()
    return table[phase]


def production_policy_snapshot() -> dict[str, dict[str, Any]]:
    """Serialize the current production table for manifest embedding."""
    return {p.value: cfg.as_metadata() for p, cfg in _production_table().items()}


def current_policy_snapshot() -> dict[str, dict[str, Any]]:
    """Serialize whichever policy the current run_mode resolves to."""
    mode = active_run_mode()
    if mode is RunMode.mock:
        table = _mock_table()
    elif mode is RunMode.smoke:
        table = _smoke_table()
    else:
        table = _production_table()
    out = {p.value: cfg.as_metadata() for p, cfg in table.items()}
    out["__run_mode__"] = {"mode": mode.value}
    return out
