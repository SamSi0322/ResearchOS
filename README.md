<p align="center">
  <img src="docs/assets/logo.png" alt="ResearchOS" width="360">
</p>

# ResearchOS

> A local-first research decision and execution system. Turn a brief into runnable experiments, evidence-checked claims, and a packaged manuscript draft — without anything leaving your laptop.

[![CI](https://github.com/SamSi0322/ResearchOS/actions/workflows/ci.yml/badge.svg)](https://github.com/SamSi0322/ResearchOS/actions/workflows/ci.yml)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-3776AB.svg?logo=python&logoColor=white)](https://www.python.org/downloads/)
[![Node 20+](https://img.shields.io/badge/node-20+-5FA04E.svg?logo=node.js&logoColor=white)](https://nodejs.org/)
[![Status: open-source MVP](https://img.shields.io/badge/status-open--source%20MVP-orange.svg)](#roadmap--known-limitations)

ResearchOS takes a research brief, generates and ranks candidate ideas, screens them through a structured funnel, produces executable experiment plans, runs them as real Python subprocesses, extracts evidence-backed claims from the measured outputs, drafts a structured report grounded in those claims, and freezes the whole audit trail into a signed ZIP.

It runs on `localhost`. Provider API keys never leave the host. Every artefact is reproducible from a recorded `provider_routing` blob, a `code_hash`, a seed, and the manifest the package freeze writes.

---

## What ResearchOS is not

> **Not a paper generator.** Every draft is decision support that requires human validation before it leaves the system. Mock-mode artefacts are tagged `MOCK` end-to-end, and the freeze step refuses to ship a package if any P0 / P1 reviewer issue is open.

> **Not an auto-submission pipeline.** Optional Human-in-the-Loop (HITL) approval gates pause the pipeline at three named checkpoints; the operator must explicitly approve before each run continues.

> **Not a hosted service.** AGPL-3.0, self-hosted on `localhost`, single-tenant. There is no managed cloud version and no public API.

---

## Highlights

- **Provider-agnostic, headless adapters** for OpenAI Responses and Anthropic Messages, plus a deterministic mock that exercises the entire pipeline offline.
- **Two-step code worker** by default — an independent reviewer pass merges patches and tests on top of the builder's output, with worker-supplied paths sanitised before they touch disk.
- **One canonical model policy** with stratified production / smoke / mock run modes, per-phase env overrides, a model-alias layer for future-dated ids, and capability gates that drop policy fields the wire model would reject.
- **Encrypted secret store** with a per-installation random salt; raw keys never appear in DB rows, logs, API responses, ZIP manifests, or browser storage.
- **Evidence-first drafting** with a deterministic alignment pass — drafts only carry claim ids that already exist on disk; nothing is invented.
- **HITL approval gates** with SMTP or `var/outbox/*.eml` fallback, so the workflow stays testable without a relay.
- **Reproducible runs** — every run records `code_hash`, seed, `provider_routing`, worker config, and the requested-vs-aliased model id pair.
- **Frozen package** — versioned ZIP with `manifest.json` (file index, sha256 per file, model-policy block), draft markdown, claim data, and per-run artefacts.

---

## Architecture at a glance

```
brief ─► ideas ─► funnel (S0..S4) ─► specs ─► code worker ─► run
                                                              │
                                                              ▼
                       result analysis ─► draft ─► review ─► package (ZIP)
```

Three layers don't collapse into each other:

1. **`app/providers/`** — headless HTTP clients behind a single `ProviderRouter`. Services never instantiate an adapter directly.
2. **`app/workers/`** — `ClaudeCodeWorker` (builder) and `CodexWorker` (reviewer). **Internal role names**, not wrappers around the Claude Code or Codex CLIs; the runtime calls provider APIs directly. A subprocess guard explicitly refuses to spawn an interactive coding-agent binary.
3. **`app/services/`** — one file per pipeline domain; orchestrate state, business rules, and audit events. Routes are thin Pydantic-in / service-out shells.

See [`docs/architecture.md`](docs/architecture.md) for the full design, data model, and known limitations.

---

## Quickstart

Mock mode, no API keys, full pipeline in five commands:

```bash
git clone <repository-url> researchos-local
cd researchos-local
cp .env.example .env

# Backend (one terminal)
cd backend
python -m venv .venv
source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m app.db.init_db --demo
uvicorn app.main:app --reload --port 8000

# Frontend (a second terminal, from the repo root)
cd frontend
npm install
npm run dev
```

Open `http://localhost:5173`, go to **Settings → Providers**, add a `mock` credential (any string works), then **Dashboard → New Project** and walk the wizard. Each pipeline action is also exposed as a REST endpoint — Swagger UI is at `http://localhost:8000/docs`.

To use real providers, replace the `mock` credential with an `openai` or `anthropic` API key in the same modal. The key is encrypted at rest, never echoed back to the frontend, and scrubbed from every log line. See [`docs/local-run.md`](docs/local-run.md) for the full setup walk-through.

---

## Run modes

| Mode | When to use | Models | Cost |
|------|-------------|--------|------|
| **`production`** | Real work | OpenAI `gpt-5.4-pro` for every phase, `xhigh` reasoning except `code_generation` (`low`) | High |
| **`smoke`** | Validate the whole chain against real APIs cheaply | `gpt-4.1-mini` / `claude-haiku-4-5`, reasoning dropped, `max_tokens` clamped | Pennies per run |
| **`mock`** | Develop or demo without network | Deterministic mock adapter | $0 |

Set `RESEARCHOS_RUN_MODE=production|smoke|mock` to pick. `RESEARCHOS_SMOKE_MODE=true` forces smoke regardless. Per-phase overrides — `RESEARCHOS_MODEL_<PHASE>`, `RESEARCHOS_REASONING_<PHASE>`, `RESEARCHOS_TEMP_<PHASE>` — apply on top of the production table; smoke derives from the post-override table. See [`docs/model-policy.md`](docs/model-policy.md) for the alias layer, capability gates, and pro-tier timeouts.

---

## Provider validation

Three distinct endpoints, three distinct questions:

| Endpoint | Question | Model used |
|----------|----------|------------|
| `POST /api/providers/test` | Does this stored credential work? | The credential-test model (default `gpt-4.1-mini` / `claude-sonnet-4-6`). **Independent of the policy table** so a future-dated policy id can't poison validation. |
| `POST /api/smoke/ping` | Does the runtime policy path work? | The exact policy model after the alias layer. |
| `POST /api/smoke/run` | Does the full tiny pipeline work end-to-end? | All phases in smoke mode. |

All three return a canonical `ProviderValidationResult` with a `category` enum: `ok` / `auth_error` / `model_error` / `network_error` / `config_error` / `provider_error`. The Settings page maps each to an explicit headline, so the old "200 but error" confusion can't recur. Raw upstream response bodies are never attached.

---

## Security model

- **Encrypted at rest.** API keys are posted to the backend over `localhost` and immediately encrypted with a Fernet key derived from `APP_MASTER_KEY` plus a per-installation random 32-byte salt (PBKDF2-HMAC-SHA256, 240k iterations). Ciphertexts live at `var/secrets/<ref>.enc` (mode `0o600` where supported); the DB only holds non-sensitive metadata and a masked preview.
- **Never logged.** A handler-level redaction filter scrubs OpenAI / Anthropic key shapes and Bearer tokens out of every log line. Raw keys never appear in API responses, DB rows, ZIP manifests, or browser storage. The frontend clears keys from memory on success and never persists them in `localStorage` / `sessionStorage`.
- **Headless by audit.** The `JobRunner` explicitly refuses to spawn an interactive coding-agent binary (`codex`, `claude`, `claude-code`). Every run, package, and validation response carries the literal `execution_mode: "headless_api"` so the audit trail is unambiguous.

---

## HITL approval gates

Per-project switch. When enabled the pipeline pauses at three named gates — `post_shortlist`, `post_pilot_evidence`, `pre_package_freeze` — creates an `ApprovalRequest`, and emails the configured approver (or drops an `.eml` into `var/outbox/` if SMTP is not set). Decisions: `approve` resumes the pipeline; `reject` and `request_changes` keep it blocked. Starting a batch run or freezing a package while an approval is pending returns HTTP 409.

The console surfaces pending approvals on the **Approvals** tab; full REST surface in [`docs/api.md`](docs/api.md).

---

## Background context bundles

At project intake an operator can upload a single ZIP (≤ 512 MB) of background material — notes, prior drafts, datasets. Text-like files are indexed and short snippets are inlined into the idea-generation prompt so generated ideas are grounded in the real context; non-text files are preserved on disk for later retrieval. Extraction is zip-slip safe.

---

## Documentation

| Doc | Purpose |
|-----|---------|
| [`docs/architecture.md`](docs/architecture.md) | Full design, three-layer separation, data model, frank list of known limitations |
| [`docs/local-run.md`](docs/local-run.md) | End-to-end setup, console walk-through, REST quickstart |
| [`docs/api.md`](docs/api.md) | REST surface |
| [`docs/model-policy.md`](docs/model-policy.md) | Phase × provider × model × effort, alias layer, capability gates |
| [`docs/smoke-mode.md`](docs/smoke-mode.md) | Cheap real-provider validation; the `python -m app.cli.smoke` CLI |

---

## Development

```bash
# Backend tests
cd backend && pytest -q

# Single test file
pytest tests/test_e2e_smoke.py -q

# Single test by name
pytest -k "evidence_alignment" -q

# Frontend typecheck + build
cd frontend && npm run typecheck && npm run build

# Full-chain smoke against real providers (cheap)
export RESEARCHOS_SMOKE_MODE=true
cd backend && python -m app.cli.smoke --ideas 2 --worker two_step

# Deterministic mock smoke (no network)
cd backend && python -m app.cli.smoke --mock --ideas 2
```

`pytest.ini` sets `asyncio_mode = auto`, so `async def` tests are picked up without the `@pytest.mark.asyncio` decorator.

---

## Roadmap & known limitations

- **Single-tenant by design.** No multi-user, no SSO, no shared filesystem. One operator on `localhost`.
- **Schema migrations are minimal.** The startup hook idempotently runs `Base.metadata.create_all` followed by `alembic upgrade head`, but only one Alembic revision exists today. Add a new revision when you change the schema; do not amend the existing one.
- **Internal-naming debt.** A few DB tables still carry legacy mentorship-era labels (`StudentProject`, `student_name`, `mentor_name`, `MentorshipSession`); they are owner / reviewer / session metadata in the current product. A primary-keyed table rename is deferred to a future migration cycle.
- **Production policy is OpenAI-only.** Anthropic was repeatedly fallback-ing during real draft runs, so the production table routes every phase to OpenAI. Per-phase overrides let you re-introduce a Claude split for a specific phase if you want to.

A frank, more complete list lives at the bottom of [`docs/architecture.md`](docs/architecture.md).

---

## Contributing

Pull requests welcome. By submitting a PR you agree that your contribution will be released under the same AGPL-3.0 terms as the rest of the project.

Quick orientation for contributors:

- Code lives under `backend/app/` (Python) and `frontend/src/` (TypeScript / React); tests under `backend/tests/`.
- The provider router is the only place to call an adapter; services never instantiate one directly.
- The model policy in `backend/app/config/model_policy.py` is the single source of truth for `phase × provider × model × effort` — never hard-code a model id in a service.
- Every service that invokes a provider writes a compact `policy` dict to its row's `meta` / `rubric` / `provider_routing` field. Preserve it when adding new provider-invoking services; that dict is what makes runs auditable per phase.

---

## Acknowledgements

ResearchOS was built with [Claude Code](https://www.anthropic.com/claude-code) (builder) and Codex (reviewer) collaborating on the same codebase. The two worker classes (`ClaudeCodeWorker`, `CodexWorker`) take their internal names from those tools but are **not** wrappers around them — at runtime ResearchOS calls the provider APIs through its own headless adapters and never spawns an interactive CLI.

---

## License

[GNU Affero General Public License v3.0](LICENSE) (AGPL-3.0). Strong copyleft: if you run a modified copy as a network-accessible service, you must offer the corresponding source to its users. See `LICENSE` for the full text.
