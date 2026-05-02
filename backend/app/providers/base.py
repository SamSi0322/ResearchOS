"""Provider adapter base types.

Every adapter implements a single async ``complete`` method that receives a
``CompletionRequest`` and returns a ``CompletionResult``. Keep the interface
small — task-specific behaviour (idea gen vs code gen vs review) is handled
above this layer in the services package.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any


class ProviderError(RuntimeError):
    """Raised by adapters on failure. Service callers catch this."""

    def __init__(self, provider: str, message: str, status: int | None = None) -> None:
        super().__init__(f"[{provider}] {message}")
        self.provider = provider
        self.status = status
        self.message = message


@dataclass
class CompletionRequest:
    system: str | None = None
    prompt: str = ""
    model: str | None = None
    temperature: float = 0.3
    max_tokens: int = 1500
    json_mode: bool = False
    task_kind: str = "generic"
    # --- model policy fields (populated by services that went through
    # ``resolve_model_policy``; adapters honour these on the wire) -----
    phase: str | None = None              # Phase enum value
    reasoning_effort: str | None = None   # internal tier label (incl. "xhigh")
    thinking_mode: str | None = None      # "adaptive" | None
    policy_label: str = "production"      # production | smoke | mock
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class CompletionResult:
    provider: str
    model: str
    text: str
    usage: dict[str, Any] = field(default_factory=dict)
    latency_ms: int = 0
    mock: bool = False
    # Echo the policy tiers the caller asked for so services can record
    # them without re-reading the policy table.
    reasoning_effort: str | None = None
    thinking_mode: str | None = None
    policy_label: str = "production"
    # --- alias / cost transparency ---
    # ``model`` above is the id actually sent on the wire (post-alias).
    # ``requested_model`` is what the policy asked for; ``actual_model``
    # mirrors ``model`` and is the sent id so callers can read either
    # without ambiguity. Reasoning effort tracks the same idea: our
    # internal ``xhigh`` tier maps to wire ``high`` so callers can see
    # both sides.
    requested_model: str | None = None
    actual_model: str | None = None
    requested_reasoning_effort: str | None = None
    actual_reasoning_effort: str | None = None
    estimated_cost: float = 0.0
    # Lifecycle of the alias that fired (if any). ``None`` when no alias
    # applied (pass-through) or when the alias layer is disabled via
    # ``RESEARCHOS_DISABLE_ALIAS``.
    alias_status: str | None = None
    # Split-cost estimate populated when we know input vs output tokens.
    # Equivalent to ``estimated_cost`` when split pricing is unknown — kept
    # as a separate field so manifests can distinguish "blended estimate"
    # from "input/output estimate".
    estimated_cost_usd: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)


class BaseProvider:
    name: str = "base"

    async def complete(self, req: CompletionRequest) -> CompletionResult:  # pragma: no cover
        raise NotImplementedError

    async def ping(self) -> CompletionResult:
        """Smoke test — used by the Test Connection button in the UI."""
        return await self.complete(
            CompletionRequest(prompt="Respond with the single word OK.", max_tokens=8)
        )

    async def aclose(self) -> None:  # pragma: no cover
        return None


def apply_policy(req: CompletionRequest, policy) -> CompletionRequest:
    """Overlay a ``ModelConfig`` onto a ``CompletionRequest``.

    Lets services build their prompt with hand-tuned max_tokens / temperature
    while the policy still decides provider, model, reasoning effort and
    thinking mode. Returns a shallow copy - the input is not mutated.

    ``req.model`` is set to the **alias-resolved** id so the wire-level call
    uses a currently-available model, while ``req.extra["requested_model"]``
    preserves the original policy id for auditing.
    """
    if policy is None:
        return req
    from app.config.model_alias import alias_info, resolve_model_alias

    actual_model = resolve_model_alias(policy.model)
    info = alias_info(policy.model)
    extra = dict(req.extra or {})
    extra.setdefault("requested_model", policy.model)
    extra.setdefault("actual_model", actual_model)
    extra.setdefault("alias_status", info.get("alias_status"))
    return replace(
        req,
        model=actual_model,
        phase=policy.phase,
        reasoning_effort=policy.reasoning_effort,
        thinking_mode=policy.thinking_mode,
        policy_label=policy.policy_label,
        max_tokens=min(req.max_tokens, policy.max_output_tokens)
        if req.max_tokens
        else policy.max_output_tokens,
        temperature=policy.temperature if req.temperature == 0.3 else req.temperature,
        extra=extra,
    )


def apply_smoke_limits(req: CompletionRequest, settings) -> CompletionRequest:
    """Clamp a ``CompletionRequest`` so a smoke run stays cheap.

    * ``max_tokens`` is lowered to ``settings.smoke_max_tokens`` if it was
      higher - we never RAISE the caller's limit.
    * The prompt (and system prompt) are truncated to
      ``settings.smoke_prompt_budget_chars`` so we do not pay for ten pages of
      context we will not use at this stage.
    * Temperature is left alone; smoke mode is about volume, not style.
    * The returned request is a shallow copy; the caller's instance is not
      mutated.

    If ``settings.smoke_mode`` is false, the request is returned unchanged.
    """
    if not getattr(settings, "smoke_mode", False):
        return req
    budget = max(800, int(getattr(settings, "smoke_prompt_budget_chars", 6000)))
    smoke_max_tokens = int(getattr(settings, "smoke_max_tokens", 400))
    return replace(
        req,
        prompt=req.prompt[:budget] if req.prompt else req.prompt,
        system=(req.system[: budget // 2] if isinstance(req.system, str) else req.system),
        max_tokens=min(req.max_tokens, smoke_max_tokens),
    )
