# Model policy

ResearchOS picks which provider + model + reasoning effort to use for each
pipeline phase through a single canonical policy layer at
`backend/app/config/model_policy.py`. Services never hard-code model IDs;
they call `resolve_model_policy(phase)` and get back a typed
`ModelConfig`.

Three run modes are supported and strictly separated:

* **production** — strong models, the real product behaviour.
* **smoke** — cheap models, tiny outputs, reasoning/thinking dropped.
* **mock** — deterministic mock adapter for every phase; never touches
  the network.

The active run mode comes from `RESEARCHOS_RUN_MODE` (or `smoke_mode`
/ `default_provider=mock` as fall-through). Tests can pass
`resolve_model_policy(phase, mode=RunMode.production)` explicitly.

## Production policy (source of truth)

| Phase               | Provider   | Model            | Reasoning / thinking      |
| ------------------- | ---------- | ---------------- | ------------------------- |
| idea_generation     | OpenAI     | gpt-5.4-pro      | xhigh reasoning            |
| idea_ranking        | OpenAI     | gpt-5.4-pro      | xhigh reasoning            |
| spec_generation     | OpenAI     | gpt-5.4-pro      | xhigh reasoning            |
| code_generation     | OpenAI     | gpt-5.4-pro      | low reasoning              |
| code_review         | OpenAI     | gpt-5.4-pro      | xhigh reasoning            |
| result_analysis     | OpenAI     | gpt-5.4-pro      | xhigh reasoning            |
| draft_generation    | OpenAI     | gpt-5.4-pro      | xhigh reasoning            |
| draft_polish        | OpenAI     | gpt-5.4-pro      | xhigh reasoning            |
| manuscript_review   | OpenAI     | gpt-5.4-pro      | xhigh reasoning            |

* **OpenAI-only production path.** Real draft runs repeatedly fell back
  from Anthropic to OpenAI, so production now routes every phase to OpenAI.
* **gpt-5.4-pro** is the current default Pro model exposed by the configured
  API keys. Set `RESEARCHOS_OPENAI_PRO_MODEL=gpt-5.5-pro` once the deployed
  key actually exposes that id.
* **xhigh for decision/review/draft phases.** The code builder keeps low
  reasoning to reduce empty reasoning-only code responses; the code review
  pass remains Pro + xhigh.
* **Long Pro timeouts.** `RESEARCHOS_PRO_REQUEST_TIMEOUT` defaults to 7200s
  because Pro/xhigh calls can legitimately reason for an hour or more.
  `RESEARCHOS_BUILDER_REQUEST_TIMEOUT` defaults to 3600s for code generation.
  Draft generation defaults to `RESEARCHOS_PRO_DRAFT_MAX_OUTPUT_TOKENS=64000`
  and polish defaults to `RESEARCHOS_PRO_POLISH_MAX_OUTPUT_TOKENS=32000` so
  hidden reasoning does not consume the entire output budget.
* **Sectional draft generation.** `RESEARCHOS_DRAFT_SECTIONAL_FIRST=true`
  avoids a single huge Pro request by generating manuscript sections one at a
  time, still through OpenAI Pro/xhigh/background mode.

The Anthropic adapter sends `thinking: {"type": "enabled"}` and
`temperature: 1` whenever `thinking_mode == "adaptive"`. The OpenAI
adapter sends `reasoning: {"effort": "<wire>"}` whenever the policy
carries a `reasoning_effort`; our internal `xhigh` tier maps to the
API's `high` on the wire while the label stays `xhigh` for audit
records.

## Smoke policy

Smoke keeps the pipeline exerciseable without spending real money:

* providers are preserved (Anthropic stays Anthropic, OpenAI stays
  OpenAI) so the wire path is still real,
* models become `RESEARCHOS_OPENAI_SMOKE_MODEL` (default
  `gpt-4.1-mini`) and `RESEARCHOS_ANTHROPIC_SMOKE_MODEL` (default
  `claude-haiku-4-5-20251001`),
* reasoning effort is dropped to `low` (or removed) and thinking mode is
  turned off,
* max output tokens is clamped to `smoke_max_tokens`,
* request timeouts shrink to `smoke_request_timeout`.

## Mock policy

Every phase routes through `provider="mock"` / `model="mock-1"`. The
router short-circuits to the mock adapter without consulting credentials
so there is no way for a mocked test to accidentally fire a real API
call.

## Model alias layer

The policy table sometimes names a model that is not yet live on the
wire (a future-dated id). Hitting that id today would 404.
`backend/app/config/model_alias.py` translates each requested id to a
currently-available model at the last possible moment, inside
`router.resolve_with_policy`:

```python
MODEL_ALIAS = {
    "claude-opus-4-7": "claude-opus-4-5",
}
```

At present only `claude-opus-4-7` is aliased — `gpt-5.4-pro` is live on
the deployed OpenAI key and goes through unchanged. Each entry has
companion lifecycle metadata in `MODEL_ALIAS_METADATA`
(`temporary` / `permanent` / `deprecated`), and operators can force the
requested id through verbatim with `RESEARCHOS_DISABLE_ALIAS=true`.

The alias is applied only when the adapter is built. The policy object
is never mutated; both ids are tracked so a later reader can see what
was *asked for* vs what was *sent*:

* `CompletionRequest.model` carries the alias target (what goes on the
  wire); the original id is kept on
  `CompletionRequest.extra["requested_model"]`.
* `CompletionResult` exposes `requested_model` and `actual_model` as
  first-class fields, plus `requested_reasoning_effort` /
  `actual_reasoning_effort` (our internal `xhigh` maps to wire `high`).
* Package `manifest.json` carries `model_policy.alias_applied: bool`,
  per-phase `model` + `actual_model`, and a flat `model_resolution:
  {phase: {requested, actual}}` summary.

### Removing aliases

When the upstream model ships (e.g. Claude Opus 4.7 goes live), delete
the corresponding key from `MODEL_ALIAS` and the router falls through to
the real id automatically. No other code changes are required.

### Cost awareness

`model_alias.py` also ships `MODEL_COST_PER_1K` (USD per 1k tokens) and
`estimate_call_cost(tokens, model)`. Every `CompletionResult` gets an
`estimated_cost` field computed from `usage.total_tokens` when the
provider returns it (falling back to `max_tokens`). We deliberately do
**not** enforce spend caps here — this is a hook, not a gate.

## Credential-test models (independent of policy)

`/api/providers/test` validates a stored credential against a dedicated,
cheap, always-available model per provider:

* OpenAI: `gpt-4.1-mini` (override with `RESEARCHOS_OPENAI_CREDENTIAL_TEST_MODEL`)
* Anthropic: `claude-sonnet-4-6` (override with `RESEARCHOS_ANTHROPIC_CREDENTIAL_TEST_MODEL`)
* Mock: `mock-1` (not configurable)

This is deliberately decoupled from the policy and alias layers. If the
production table asks for a future-dated id and the alias
happens to point at something that 404s on the operator's account, we do
NOT want "test credential" to fail as if the key were bad. The credential
test answers *does this key work?*; for *does the current runtime route
work?* use `/api/smoke/ping`, which goes through `resolve_model_policy` +
`router.resolve_with_policy` and exercises the real wire path.

## Anthropic temperature with adaptive thinking

When `thinking_mode == "adaptive"`, the Anthropic adapter sends
`thinking: {type: "enabled", budget_tokens: ...}` and pins
`temperature: 0.3` rather than the legacy `1.0`. The stable temperature
keeps the research pipeline deterministic while the thinking pass still
produces the extended-reasoning benefit. Override per-phase via
`RESEARCHOS_TEMP_<PHASE>` if needed.

## Overrides

Every field is overridable per phase via env vars:

```bash
# Pin an explicit model for one phase (any phase, any mode).
RESEARCHOS_MODEL_CODE_GENERATION=gpt-5.4-turbo
RESEARCHOS_MODEL_IDEA_RANKING=o3-pro

# Pin the reasoning effort. Recognised tiers:
# minimal | low | medium | high | xhigh (xhigh becomes "high" on the wire).
RESEARCHOS_REASONING_CODE_REVIEW=xhigh

# Pin the temperature (applies in production / smoke, ignored when
# adaptive thinking is enabled because Anthropic requires temperature=1
# in that case).
RESEARCHOS_TEMP_IDEA_GENERATION=0.9
```

Overrides apply to the *production* table specifically; smoke derives
from the post-override production table.

## Auditability

Every invocation records the policy it used:

* `Idea.meta`, `Scorecard.rubric`, `ExperimentSpec.meta`, `Draft.meta`,
  `ReviewIssue.meta` each carry a compact `policy` dict (phase, provider,
  model, reasoning_effort, thinking_mode, policy_label).
* `ExperimentRun.provider_routing` adds `run_mode` alongside the worker's
  chosen provider + model.
* `DeliveryPackage` manifests embed `model_policy: {run_mode, phases:
  {phase: {provider, model, reasoning_effort, thinking_mode}}}`.
* Audit events attach the same metadata on generation / analysis /
  review events.

A later reader can answer, per phase:
* which provider was used,
* which model ID was used,
* which reasoning / thinking tier was active,
* whether the run was production / smoke / mock.
