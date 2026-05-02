"""Model alias + per-call cost estimate.

Two responsibilities, kept tiny and surgical:

1. **Alias layer** — the production policy names the models we *want* to use.
   If an id is not yet live on the provider endpoints, we can translate each
   requested model to a currently-available alias at the last possible
   moment, inside the router. The requested id stays on the audit trail so
   a future maintainer sees both what was *asked for* and what was *sent*.

2. **Cost awareness** — a tiny per-1k-token map + a helper. We never enforce
   a spend cap here; we just attach an ``estimated_cost`` to each
   ``CompletionResult`` so manifests and logs can reason about cost. Real
   budget enforcement, if/when we need it, can live outside this module.

This file is intentionally standalone: it does not import settings, ORM
models, or the router. Anything that needs an alias just calls
``resolve_model_alias(requested)``; anything that needs a cost calls
``estimate_call_cost(tokens, model)``.
"""

from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# Alias table
# ---------------------------------------------------------------------------
# Map of "model the policy asked for" -> "model we actually send on the wire
# today". Remove entries as the real models ship.
MODEL_ALIAS: dict[str, str] = {
    # Claude Opus 4.7 is future-dated; Claude Opus 4.5 is the current
    # strongest Claude on the user's keys and supports adaptive thinking
    # (its id starts with ``claude-opus-4-`` so the adapter keeps
    # thinking + stable-temperature behaviour).
    "claude-opus-4-7": "claude-opus-4-5",
}


# Lifecycle metadata for each alias. ``status`` is one of:
#   ``temporary``  — the requested id is future-dated / not yet shipping.
#                    Remove the entry once the upstream model is live.
#   ``permanent``  — we intentionally map one id onto another (e.g. a
#                    pricing-tier redirect). Do NOT remove without review.
#   ``deprecated`` — the requested id was retired; alias is the successor
#                    kept for back-compat. Schedule removal.
# Keeping this explicit makes alias lifecycle auditable instead of tribal
# knowledge — every manifest records the status so a future operator can
# decide when to prune entries.
MODEL_ALIAS_METADATA: dict[str, dict[str, str]] = {
    "claude-opus-4-7": {"target": "claude-opus-4-5", "status": "temporary"},
}


def _alias_disabled() -> bool:
    """``RESEARCHOS_DISABLE_ALIAS=true`` turns the alias layer off.

    Operators use this to force the router to send the requested id verbatim
    — useful once upstream ships the real id but before
    ``MODEL_ALIAS`` has been pruned in source. Accepts the usual truthy
    strings so the flag is forgiving of shell quoting.
    """
    return os.environ.get("RESEARCHOS_DISABLE_ALIAS", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def resolve_model_alias(model: str) -> str:
    """Return the currently-usable model id for ``model``.

    * If ``RESEARCHOS_DISABLE_ALIAS=true`` is set, return ``model`` unchanged.
    * If ``model`` is in the alias table, return the alias target.
    * Otherwise return ``model`` unchanged (pass-through).
    """
    if not model:
        return model
    if _alias_disabled():
        return model
    return MODEL_ALIAS.get(model, model)


def alias_was_applied(model: str) -> bool:
    if not model or _alias_disabled():
        return False
    return model in MODEL_ALIAS


def alias_status(model: str) -> str | None:
    """Return the lifecycle status string for ``model`` if an alias is active.

    * ``None`` when no alias fired (pass-through or kill-switch on).
    * Otherwise one of ``temporary`` / ``permanent`` / ``deprecated``.
    """
    if not alias_was_applied(model):
        return None
    meta = MODEL_ALIAS_METADATA.get(model)
    if not meta:
        # Alias present without metadata is a soft inconsistency — default
        # to temporary so operators still see "this is a translation layer".
        return "temporary"
    return meta.get("status")


def alias_info(model: str) -> dict[str, object]:
    """One-call summary used by manifests / audit records.

    Shape:
        {
          "requested": str,
          "actual": str,
          "alias_applied": bool,
          "alias_status": "temporary" | "permanent" | "deprecated" | None,
          "alias_disabled": bool,
        }
    """
    disabled = _alias_disabled()
    actual = model if (disabled or not model) else MODEL_ALIAS.get(model, model)
    applied = bool(model) and not disabled and model in MODEL_ALIAS
    return {
        "requested": model,
        "actual": actual,
        "alias_applied": applied,
        "alias_status": (
            (MODEL_ALIAS_METADATA.get(model, {}) or {}).get("status", "temporary")
            if applied
            else None
        ),
        "alias_disabled": disabled,
    }


# ---------------------------------------------------------------------------
# Which Anthropic models support the "thinking" parameter
# ---------------------------------------------------------------------------
# Extended thinking / adaptive thinking landed with the Claude 3.7 Sonnet and
# Claude 4 families. Older models (Claude 3 Opus, Claude 3.5 Sonnet, Haiku 4.5)
# do NOT accept ``thinking`` on the wire and will 400 if we send it. When the
# alias layer maps a thinking-intended phase to one of those, the adapter must
# skip the ``thinking`` block silently.
_THINKING_CAPABLE_PREFIXES: tuple[str, ...] = (
    "claude-opus-4-",
    "claude-sonnet-4-",
    "claude-4-",
    "claude-3-7-",
)


def supports_thinking(model: str) -> bool:
    if not model:
        return False
    return any(model.startswith(p) for p in _THINKING_CAPABLE_PREFIXES)


# OpenAI: reasoning.effort on the Responses API is a gpt-5 / o-series feature.
# gpt-4.x and older reject it. When the alias layer maps a policy id to a
# non-reasoning model, the adapter must skip the ``reasoning`` block.
_REASONING_EFFORT_CAPABLE_PREFIXES: tuple[str, ...] = (
    "gpt-5",
    "o1",
    "o3",
    "o4",
    "o5",
)


def supports_reasoning_effort(model: str) -> bool:
    if not model:
        return False
    return any(model.startswith(p) for p in _REASONING_EFFORT_CAPABLE_PREFIXES)


# Reasoning-tier OpenAI models (gpt-5, o-series) also REJECT ``temperature``.
# gpt-4.x accepts it. When the adapter targets one of these, the temperature
# field must be omitted from the body.
def supports_temperature(model: str) -> bool:
    if not model:
        return True
    # Reasoning models reject temperature. Everything else accepts it.
    return not any(model.startswith(p) for p in _REASONING_EFFORT_CAPABLE_PREFIXES)


# ---------------------------------------------------------------------------
# Cost map
# ---------------------------------------------------------------------------
# USD per 1K tokens. Input and output prices are collapsed into one number
# deliberately; this is an estimate, not an invoice. Numbers reflect
# published list prices at patch time - update as needed.
MODEL_COST_PER_1K: dict[str, float] = {
    # OpenAI - alias targets and common ids
    "gpt-4.1": 0.0025,
    "gpt-4.1-mini": 0.00015,
    "gpt-4.1-nano": 0.00010,
    "gpt-4o": 0.0075,
    "gpt-4o-mini": 0.00015,
    "gpt-5": 0.005,
    "gpt-5-mini": 0.00125,
    "gpt-5-nano": 0.0005,
    "gpt-5.1": 0.005,
    "gpt-5.2": 0.005,
    "gpt-5.3": 0.005,
    "gpt-5.4": 0.005,
    "gpt-5.4-pro": 0.020,
    "gpt-5.5": 0.005,
    "gpt-5.5-pro": 0.020,
    "o3": 0.012,
    "o3-mini": 0.0035,
    "o4-mini": 0.003,
    "o1": 0.015,
    # Anthropic
    "claude-opus-4-5": 0.015,
    "claude-opus-4-1": 0.015,
    "claude-opus-4-1-20250805": 0.015,
    "claude-opus-4-0": 0.015,
    "claude-opus-4-20250514": 0.015,
    "claude-sonnet-4-5": 0.003,
    "claude-sonnet-4-5-20250929": 0.003,
    "claude-sonnet-4-0": 0.003,
    "claude-sonnet-4-20250514": 0.003,
    "claude-haiku-4-5": 0.00025,
    "claude-haiku-4-5-20251001": 0.00025,
    "claude-opus-4-7": 0.015,
    "claude-sonnet-4-6": 0.003,
    "claude-3-opus-20240229": 0.015,
    "claude-3-5-sonnet-20240620": 0.003,
    # Mock
    "mock-1": 0.0,
}


def estimate_call_cost(tokens: int, model: str) -> float:
    """Return an estimated USD cost for a call that consumed ``tokens`` total.

    * ``tokens`` should be the best approximation the caller has, normally
      ``usage.total_tokens`` from the provider. When unknown, 0 is fine.
    * Unrecognised model ids fall back to ``0.0`` rather than raising; a
      zero estimate is still accurate enough to be safe in manifests.
    """
    if not tokens or tokens <= 0:
        return 0.0
    price = MODEL_COST_PER_1K.get(model)
    if price is None:
        return 0.0
    return round((tokens / 1000.0) * price, 6)


# ---------------------------------------------------------------------------
# Split (input/output) cost table
# ---------------------------------------------------------------------------
# USD per 1K tokens, split by direction. Used for the lightweight per-run
# cost estimate. Unknown models fall back to ``MODEL_COST_PER_1K`` (blended).
# Prices reflect list prices at patch time — update as needed.
MODEL_COST_TABLE: dict[str, dict[str, float]] = {
    # OpenAI
    "gpt-4.1":      {"input": 0.0025, "output": 0.01},
    "gpt-4.1-mini": {"input": 0.00015, "output": 0.0006},
    "gpt-4.1-nano": {"input": 0.00010, "output": 0.0004},
    "gpt-4o":       {"input": 0.0025, "output": 0.01},
    "gpt-4o-mini":  {"input": 0.00015, "output": 0.0006},
    "gpt-5":        {"input": 0.005, "output": 0.015},
    "gpt-5-mini":   {"input": 0.00125, "output": 0.005},
    "gpt-5-nano":   {"input": 0.0005, "output": 0.002},
    "gpt-5.1":      {"input": 0.005, "output": 0.015},
    "gpt-5.2":      {"input": 0.005, "output": 0.015},
    "gpt-5.3":      {"input": 0.005, "output": 0.015},
    "gpt-5.4":      {"input": 0.005, "output": 0.015},
    "gpt-5.4-pro":  {"input": 0.020, "output": 0.080},
    "gpt-5.5":      {"input": 0.005, "output": 0.015},
    "gpt-5.5-pro":  {"input": 0.020, "output": 0.080},
    "o3":           {"input": 0.012, "output": 0.048},
    "o3-mini":      {"input": 0.0035, "output": 0.014},
    "o4-mini":      {"input": 0.003, "output": 0.012},
    "o1":           {"input": 0.015, "output": 0.060},
    # Anthropic
    "claude-opus-4-5":     {"input": 0.015, "output": 0.075},
    "claude-opus-4-1":     {"input": 0.015, "output": 0.075},
    "claude-opus-4-0":     {"input": 0.015, "output": 0.075},
    "claude-sonnet-4-5":   {"input": 0.003, "output": 0.015},
    "claude-sonnet-4-0":   {"input": 0.003, "output": 0.015},
    "claude-haiku-4-5":    {"input": 0.00025, "output": 0.00125},
    "claude-haiku-4-5-20251001": {"input": 0.00025, "output": 0.00125},
    "claude-3-opus-20240229":   {"input": 0.015, "output": 0.075},
    # Mock
    "mock-1":       {"input": 0.0, "output": 0.0},
}


def estimate_split_cost(input_tokens: int, output_tokens: int, model: str) -> float:
    """Return a USD estimate that honours the input vs. output price split.

    Falls back to the blended ``MODEL_COST_PER_1K`` rate (applied to the
    combined token count) when the model is not in ``MODEL_COST_TABLE`` —
    this keeps the contract that unknown models never raise and never lie
    about their cost. Zero/negative inputs => 0.0.
    """
    input_tokens = max(0, int(input_tokens or 0))
    output_tokens = max(0, int(output_tokens or 0))
    total = input_tokens + output_tokens
    if total == 0:
        return 0.0
    split = MODEL_COST_TABLE.get(model)
    if split is None:
        return estimate_call_cost(total, model)
    cost = (input_tokens / 1000.0) * float(split.get("input", 0.0)) + (
        output_tokens / 1000.0
    ) * float(split.get("output", 0.0))
    return round(cost, 6)
