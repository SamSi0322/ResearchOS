<!--
Thanks for the contribution. Please skim CONTRIBUTING.md (especially the
"Code constraints" section) before submitting if you haven't already.
-->

## Summary

What does this PR change and why?

## Test plan

- [ ] `pytest -q` passes locally (from `backend/`)
- [ ] `npm run typecheck && npm run build` passes locally (from `frontend/`, only if the frontend changed)
- [ ] CI is green
- [ ] Manually verified the change in mock mode (and smoke mode if it touches a provider adapter)

## Code-constraints checklist

If this PR touches the provider / model / worker / runtime layers, confirm:

- [ ] Provider calls go through `ProviderRouter`; no service instantiates an adapter directly.
- [ ] No hard-coded model id / reasoning effort / temperature in any service — all via `resolve_model_policy(phase)`.
- [ ] New provider-invoking services write the `policy` dict (`{phase, provider, model, reasoning_effort, thinking_mode, policy_label}`) to the row's `meta` / `rubric` / `provider_routing` field.
- [ ] `Run` rows still record `code_hash`, `seed`, `provider_routing`, and `config.worker`.
- [ ] Nothing under `backend/app/` shells out to `codex` / `claude` / `claude-code` (the `JobRunner` guard is intact).
- [ ] Backend tests use the mock adapter (no real API calls); the `conftest.py` autouse fixture is not bypassed.

## Related issues

Closes #
