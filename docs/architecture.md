# ResearchOS (local) — architecture

ResearchOS is an **operator console + headless runtime** for a research
decision and execution system. It runs on a single developer machine. It is
not a paper generator, not an auto-submission pipeline, and not a
multi-tenant SaaS — every draft is explicitly decision-support output that
requires human validation before it leaves the console.

> **Internal-naming debt.** A few ORM models still use legacy labels
> (`StudentProject`, `student_name`, `mentor_name`, `MentorshipSession`) from
> an earlier mentorship-service framing. They are no longer exposed in
> operator-facing copy, and a schema rename is out of scope for this pass
> because renaming a primary-keyed table across the whole pipeline carries
> real migration risk. Treat the fields as owner/reviewer metadata.

## Shape of the system

```
+--------------+          +-------------------------------+
|  frontend    |          |           backend             |
|  (Vite/React)| <------> |  FastAPI  +  SQLAlchemy SQLite |
|   5173       |  /api    |               8000              |
+--------------+          +---------+---------------------+
                                    |
                 +------------------+------------------+
                 |                  |                  |
            providers/         services/             workers/
          (OpenAI, Anthropic,  (13 domain           (claude_code,
           MockAdapter)         services +           codex, job
                                AuditService)        runner)
                 |                  |                  |
                 +------------------+------------------+
                                    |
                     var/       (secrets + artifacts + workspaces + packages)
```

### Dual-phase clarification

This matters: **Claude Code and Codex are the tools we are using to BUILD
ResearchOS.** At runtime the system talks to the *Claude API* / *OpenAI
Responses API* through our own headless adapters. There is no dependency on
Claude Code, the Claude Code Agent SDK, any IDE, or any interactive terminal
session at runtime. Once the local backend is running, no human has to open
a coding tool for the pipeline to make progress.

The two worker classes, `ClaudeCodeWorker` (builder) and `CodexWorker`
(independent reviewer), are internal names for these two headless roles.
They are implemented as plain Python classes that call provider adapters.
Swapping in a different backing model or a different adapter is a local edit
in `app/workers/` + `app/providers/`; the rest of the pipeline does not
change.

### Default collaboration: builder + reviewer (two-step)

The intended default runtime collaboration model is:

1. **Builder pass** (`ClaudeCodeWorker`) uses the builder prompt
   (`prompts/code_generation.md`) to produce an initial file tree from an
   `ExperimentSpec`.
2. **Reviewer pass** (`CodexWorker`) uses a separate reviewer prompt
   (`prompts/code_review.md`). It is explicitly instructed to act as an
   independent reviewer, not as a second builder. It may return full-file
   patches and new tests.
3. The code-worker service merges the reviewer's patches over the builder's
   output and materialises the result into the run workspace.

`RunStartIn.worker` defaults to `"two_step"` at the schema level and the
frontend run form preselects **Builder + Reviewer (recommended)**. Operators
can still choose single-worker modes (`claude_code` builder-only, `codex`
reviewer-only) when that matches the task.

## Pipeline stages

```
brief → ideas → funnel (S0..S4) → specs → code worker → run
                                                     |
                                                     v
                        result analysis → draft → review → package (ZIP)
```

Every stage writes to SQLite + the audit log + (where applicable) the artifact
store. The pipeline is idempotent where it reasonably can be: ideas and specs
are versioned, drafts are append-only, packages are versioned and supersede
each other.

## Module map

### Backend

| Path                                         | Responsibility                                            |
| -------------------------------------------- | --------------------------------------------------------- |
| `app/main.py`                                | FastAPI app, CORS, schema bootstrap                       |
| `app/config/settings.py`                     | Typed settings loaded from `.env` / process env           |
| `app/core/enums`                             | String enums shared by models, schemas, services          |
| `app/core/models`                            | SQLAlchemy ORM models (one file per domain)               |
| `app/core/schemas`                           | Pydantic request/response schemas                         |
| `app/db`                                     | Base + session + init/seed script                         |
| `app/storage/secret_store.py`                | Filesystem-backed encrypted secret store (Fernet/PBKDF2)  |
| `app/storage/artifact_store.py`              | Local artifact filesystem layout with sha256              |
| `app/storage/workspace_manager.py`           | Per-run workspace on disk                                 |
| `app/providers/{openai,anthropic,mock}_adapter.py` | Headless HTTP clients + router                       |
| `app/workers/claude_code_worker.py`          | Primary code-generation worker                            |
| `app/workers/codex_worker.py`                | Reviewer / patch / test augmentation worker               |
| `app/workers/job_runner.py`                  | Async subprocess runner with concurrency cap + timeout    |
| `app/services/*`                             | Domain orchestration: brief, ideas, funnel, spec, code, run, analysis, draft, review, package, sessions, budget, audit, secret |
| `app/api/routes/*`                           | FastAPI routers, one per domain                           |
| `app/prompts/*.md`                           | Prompt templates loaded at runtime                        |

### Frontend

| Path                                       | Responsibility                                      |
| ------------------------------------------ | --------------------------------------------------- |
| `src/main.tsx`                             | React root + Query + Router                         |
| `src/app/App.tsx`                          | Sidebar shell + routes                              |
| `src/app/pages/*`                          | Dashboard, Project create, Project detail, Settings, Audit |
| `src/features/*`                           | Per-tab components (ideas, funnel, specs, runs, drafts, reviews, sessions, packages, audit) |
| `src/lib/api.ts`                           | Thin typed client around the backend API            |
| `src/lib/queryClient.ts`                   | TanStack Query client                               |

## Secret handling

1. Operator opens Settings, clicks **Add Provider Credential**.
2. Modal collects `{provider, label, api_key, default_model?, ...}` and POSTs
   it directly to `/api/providers`.
3. `ProviderSecretService` asks `SecretStore` to encrypt the key with a Fernet
   key derived from `APP_MASTER_KEY` + a **per-installation random 32-byte
   salt** stored at `var/secrets/.salt`. The salt is generated on first boot.
   KDF = PBKDF2-HMAC-SHA256, 240k iterations.
4. The ciphertext lives in `var/secrets/<ref>.enc` (0o600 where the OS
   supports it). Metadata (`provider`, `masked_preview`) lives in
   `var/secrets/<ref>.meta.json` plus a `ProviderCredential` DB row that
   holds only non-sensitive fields.
5. Runtime LLM calls go through the `ProviderRouter`, which picks a
   credential based on: (a) per-task `default_for` mapping, (b) the
   `is_default` flag, (c) auto-selecting the **only** configured credential
   if exactly one exists, (d) the process-wide `RESEARCHOS_DEFAULT_PROVIDER`,
   in that order. It reads the ciphertext through the secret store, decrypts
   in-memory, and hands the plaintext to the appropriate adapter (OpenAI
   Responses, Anthropic Messages, or Mock).
6. Raw keys are never written to logs — the logging filter redacts OpenAI /
   Anthropic key shapes and Bearer tokens at the handler layer. They never
   appear in API responses; only masked previews do.

### Salt file lifecycle

| Event                     | Behavior                                              |
| ------------------------- | ----------------------------------------------------- |
| First boot                | `var/secrets/.salt` generated (32 random bytes)       |
| Subsequent boots          | Salt read as-is                                       |
| Salt file deleted         | New salt created; all stored ciphertexts become unreadable. Delete `var/secrets/*.enc` + `provider_credentials` rows and re-enter keys. |
| Upgrading from pre-salt   | See the migration note in `README.md`                |

## Headless code workers

The build spec was explicit: **no interactive Claude Code or Codex sessions
at runtime.** The two workers (`ClaudeCodeWorker`, `CodexWorker`) are
headless wrappers that:

1. Accept a `CodeWorkerRequest` built from an `ExperimentSpec`.
2. Ask the router for a provider for the corresponding `TaskKind`
   (`code_generation` for the builder, `code_review` for the reviewer).
3. Call the adapter with a JSON-strict, role-specific prompt:
   * builder loads `prompts/code_generation.md`;
   * reviewer loads `prompts/code_review.md` **only** (it does not load the
     builder prompt - that would nudge the model toward producing new code
     instead of reviewing what the builder already produced).
4. Parse the returned file list + summary + warnings + (for the reviewer)
   patches.
5. Return files for the runner to materialise into a workspace. The
   `CodeWorkerService` sanitises every worker-provided path (rejecting
   absolute paths, drive letters, and `..` traversal) before writing.

This preserves the interface even when the backing model or product
catalogue changes — you only adjust the adapter or prompt, not the rest of
the pipeline.

## Experiment runner

* `ExperimentRunnerService.start_and_run` generates code, runs `python train.py`
  via `asyncio.create_subprocess_exec` with a concurrency semaphore, captures
  stdout/stderr, loads `outputs/metrics.json`, classifies the result
  (`succeeded_valid` / `succeeded_invalid` / `failed_retriable` /
  `failed_terminal` / `timed_out` / `canceled`), and audits the start/finish.
* Run state includes a `code_hash` (sha256 over concatenated generated
  files), `seed`, `provider_routing` blob, and `config.worker` so the run is
  reproducible.

## Evidence-first drafting

`DraftService.generate` collects all `Claim` rows + selected runs and sends
them to the provider with a prompt that refuses to invent numbers. Sections
are written to `DraftSection` rows with `claim_refs` that cite specific claim
ids. If a section cannot be evidence-backed, the fallback produces a
`[PLACEHOLDER]` paragraph instead. Drafts tagged `mock=True` cannot be
mistaken for real results — they are flagged in the package manifest and in
the rendered markdown.

## Review / package freeze

Reviewers produce `ReviewIssue` rows scoped by severity (`P0..P3`).
`PackageService.build` refuses to freeze if any `P0` or `P1` issues are
open. `P2` requires explicit `allow_with_waived_p2=True`. `P3` is informational.
On success we write a ZIP to `var/packages/<project_id>/package_vN.zip`
containing:

```
manifest.json   (file index + checksums + mock flags)
README.md       (human summary)
data/*.json     (project / ideas / funnel / specs / runs / claims / drafts / issues / sessions / audit)
artifacts/...   (real files - code, logs, metrics, predictions)
manuscript/draft_vN.md
```

Checksums: every real file has a sha256 in the manifest; the package row also
records its own sha256.

## What's *not* implemented in this local MVP (and why)

* **Alembic migrations**: we use `Base.metadata.create_all` at startup. The
  `alembic` dependency is installed and a stub `migrations/README.md` walks
  through enabling autogenerate when you need multi-dev schema drift.
* **Real-time token streaming from providers**: the adapters return full
  completions. Streaming would only help UX; the orchestration logic works
  the same.
* **Cost tracking per-call**: the `BudgetService` records manual ledger
  entries. Automatic per-call cost accounting would need model-specific
  tokenizers and price tables and was out of scope for the day.
* **Authentication**: this is a localhost-only internal tool; there is no
  login. Add a reverse proxy + basic auth if you expose it.
* **Vector search / clustering for idea diversity**: the idea generator
  produces a `cluster_tag` but we don't run embeddings. The funnel can be
  extended later.
