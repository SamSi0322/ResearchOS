# Contributing to ResearchOS

Thanks for your interest. ResearchOS is a single-maintainer open-source
project under AGPL-3.0. Pull requests are welcome — please skim the
constraints below before sinking time into a large change.

## Quick start

The README's [Quickstart](README.md#quickstart) gets you a fully running
local install in five commands, in mock mode, with no API keys. Use that
— the demo seeder lets you exercise every pipeline phase from a clean
slate without touching the network.

## Reporting bugs

Open a [GitHub Issue](https://github.com/SamSi0322/ResearchOS/issues)
with:

- Run mode (`mock` / `smoke` / `production`) and the relevant `RESEARCHOS_*`
  env vars at the time.
- The commit hash you reproduced against (`git rev-parse HEAD`).
- Minimal repro — a `curl` against `http://localhost:8000/api/...` or a
  console click path is plenty.
- Expected vs actual behaviour. The relevant section of the audit
  timeline (`GET /api/audit?...`) or the `manifest.json` from a frozen
  package often makes triage faster.

For security vulnerabilities use [SECURITY.md](SECURITY.md), not a public
issue.

## Proposing changes

1. **For non-trivial changes, open an issue first** describing the problem
   and the approach. Drive-by refactors of the provider router, model
   policy, or secret store are particularly likely to be rejected without
   prior discussion.
2. Fork the repo and branch off `main`. Branch name is up to you;
   descriptive is nice (`fix/alias-disabled-passthrough`,
   `feat/openai-batch`).
3. Make the change. Add or update tests under `backend/tests/`. Frontend
   does not have a test runner today — at minimum, `npm run typecheck`
   and `npm run build` must pass.
4. Run `pytest -q` from `backend/` and confirm green locally before
   pushing.
5. Open a PR against `main`. CI runs `pytest -q` plus the frontend
   typecheck + build. Wait for CI to finish before pinging.

## Code constraints

These are load-bearing — review will push back hard on PRs that break
them.

- **Provider calls**: services never instantiate an adapter. Always go
  through `ProviderRouter` (`backend/app/providers/router.py`) with a
  `TaskKind`. The router handles credential selection, alias resolution,
  capability gating, and audit metadata.
- **Model policy**: never hard-code a model id, reasoning effort, or
  temperature in a service. Call `resolve_model_policy(phase)` and use
  the returned `ModelConfig`. The single source of truth lives in
  `backend/app/config/model_policy.py`.
- **Audit metadata**: every service that invokes a provider writes a
  compact `policy` dict (`{phase, provider, model, reasoning_effort,
  thinking_mode, policy_label}`) to its row's `meta` / `rubric` /
  `provider_routing` field. Do not drop this when adding a new
  provider-invoking service — it is what makes runs auditable per phase.
- **Run reproducibility**: do not drop `code_hash`, `seed`,
  `provider_routing`, or `config.worker` from `Run` rows. Losing one of
  them silently breaks the package-freeze audit trail.
- **Headless runtime**: nothing under `backend/app/` may shell out to
  `codex`, `claude`, or `claude-code`. The `JobRunner` already enforces
  this via `assert_not_interactive_agent`; do not disable it.
- **Tests use mock mode**: backend tests must run on the deterministic
  mock adapter, with no real API calls. The `conftest.py` autouse fixture
  enforces this — it isolates env vars, scrubs `OPENAI_API_KEY` and
  `ANTHROPIC_API_KEY`, and points the API_KEYS file at an empty fixture.
  Don't fight it.

## Commit and PR style

- No required convention, but a one-line subject plus a short body is
  nice. Squash-merge on PR is fine.
- AGPL-3.0 means: by submitting a PR you agree your contribution is
  released under AGPL-3.0.

## What is out of scope

These are deliberate non-goals. PRs that bolt them on without prior
discussion are likely to be closed:

- Multi-tenancy / multi-user / SSO. ResearchOS is single-operator on
  `localhost` by design.
- A hosted / cloud version. AGPL-3.0 already says what we want here.
- Auto-submission to journals or preprint servers. The system is
  decision support; the human submits.
- Replacing the package-freeze ZIP with a different distribution format.
- Schema renames of `StudentProject` / `student_name` / `mentor_name` /
  `MentorshipSession`. These are real internal-naming debt; a rename
  needs a real migration plan, which is its own design discussion.

## Code of conduct

Participation in this project is governed by the
[Contributor Covenant Code of Conduct](CODE_OF_CONDUCT.md). By
contributing, you agree to abide by its terms.
