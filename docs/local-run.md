# Local run instructions

## Prerequisites

* Python 3.11+ (tested on 3.13)
* Node 18+ (tested on 24)
* ~200 MB disk for dependencies

## One-time setup

```bash
# Clone the repo, then:
cd researchos-local

cp .env.example .env                # edit APP_MASTER_KEY if you want real security
cd backend
python -m venv .venv
source .venv/bin/activate             # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m app.db.init_db --demo       # create schema + seed demo project

cd ../frontend
npm install
```

## Run

Open two terminals.

**Terminal 1 — backend:**

```bash
cd researchos-local/backend
source .venv/bin/activate
uvicorn app.main:app --reload --port 8000
```

Backend will listen on `http://localhost:8000`. Swagger UI at
`http://localhost:8000/docs`.

**Terminal 2 — frontend:**

```bash
cd researchos-local/frontend
npm run dev
```

Open `http://localhost:5173`.

## Runtime is headless

Nothing in the runtime invokes `codex` or `claude` binaries. Provider calls go
through HTTP adapters under `backend/app/providers/`, and every run, package
manifest, and `/providers/test` / `/smoke/ping` response carries the literal
`execution_mode: "headless_api"`. The worker labels in the Runs tab
(*Builder only (headless code worker)*, *Reviewer only (headless review
worker)*) are logical roles — picking one never opens a terminal UI. The
job runner explicitly refuses to spawn any executable whose basename is
`codex` / `codex.exe` / `codex.cmd` / `claude` / `claude.exe` / `claude-code`
as defence-in-depth.

If you want to automate the Claude Code or Codex CLIs from *development*
tooling (outside the runtime), use their non-interactive modes:

* Codex: `codex exec …` (not the interactive TUI)
* Claude Code: `claude -p "…"` / `claude --print …`

These are not runtime dependencies — ResearchOS does not require either.

## Provider validation vs. smoke ping

Three separate flows, each answers a different question:

| Flow                                  | Question                                       | Model used                                  |
| ------------------------------------- | ---------------------------------------------- | ------------------------------------------- |
| **Settings → Test** (`/providers/test`) | Does this stored key work?                     | Per-provider credential-test model (default `gpt-4.1-mini` / `claude-sonnet-4-6`). Not the policy model. |
| **Settings → Ping** (`/smoke/ping`)     | Does the current runtime route work?           | The exact policy model (after alias layer) for the phase that provider drives. |
| **Settings → Run 2-idea smoke** (`/smoke/run`) | Does the tiny end-to-end pipeline work? | All phases, smoke mode.                     |

The UI prints an explicit classification for every validation call:

* **Credential valid** — the key works.
* **Credential invalid** — 401/403 from the provider; rotate the key.
* **Configured but selected model unavailable** — 404 / "model does not
  exist"; the key is fine, fix the model id. This is the failure mode
  operators used to misread as "key broken".
* **Network / provider unreachable** — transport / DNS / timeout.
* **Configuration incomplete** — missing credential for the requested
  provider/phase.

Override the credential-test models without touching the policy table:

```bash
RESEARCHOS_OPENAI_CREDENTIAL_TEST_MODEL=gpt-4.1-mini
RESEARCHOS_ANTHROPIC_CREDENTIAL_TEST_MODEL=claude-sonnet-4-6
```

## First walkthrough

1. **Settings / Providers** → *Add Provider Credential* → choose `mock` and
   enter any non-empty string. Mark it as default. (Real keys: pick `openai`
   or `anthropic` and paste your key. The key goes straight to the backend
   and is encrypted at rest.)
2. **Dashboard** → click the seeded demo project, or **+ New project** to
   create one.
3. In the project page, use the tabs in order:
   * **Ideas** → Generate ideas → (optional) Score → tick decisions.
   * **Funnel** → Advance S0 → S1 with a target count.
   * **Specs** → Generate spec for a promoted idea.
   * **Runs** → Start run. The default worker is **Builder + Reviewer
     (recommended)** — the builder produces initial files, then an
     independent reviewer pass merges patches and tests on top.
     * In mock mode the generated code is stdlib-only and the whole run
       completes in under a second.
     * Click **Analyze** on the run row — this writes Claim rows.
   * **Drafts** → Generate draft.
   * **Reviews** → Run all reviewers → resolve / waive issues.
   * **Package** → Build package → big **⤓ Download ZIP** button on the
     "Latest package" card.
4. **Audit** page shows the full timeline of every action the pipeline took.

### Model policy

Every phase picks its provider + model through
`backend/app/config/model_policy.py`. Flip `RESEARCHOS_RUN_MODE` to
switch policy tables:

* `production` (default): OpenAI-only, using the configured Pro model
  (`gpt-5.4-pro` by default; set `RESEARCHOS_OPENAI_PRO_MODEL=gpt-5.5-pro`
  once the API key exposes it). Critical phases use xhigh reasoning; the
  code builder uses low reasoning for reliable JSON/code output. Pro calls
  default to `RESEARCHOS_PRO_REQUEST_TIMEOUT=7200` so long reasoning jobs are
  allowed to finish, and draft generation defaults to a 64k output budget so
  hidden reasoning does not consume the entire response.
* `smoke`: cheap smoke models, reasoning / thinking effort dropped,
  output length capped.
* `mock`: mock adapter on every phase (no network, deterministic).

Per-phase overrides live under `RESEARCHOS_MODEL_<PHASE>`,
`RESEARCHOS_REASONING_<PHASE>`, `RESEARCHOS_TEMP_<PHASE>`. Full
reference: `docs/model-policy.md`.

### Human-in-the-loop (HITL)

On the project create wizard you can flip **Enable approval checkpoints** to
`yes` and fill in the approver email + optional CC list + per-project
timeout and reminder intervals. When HITL is on:

* Starting a batch run (Runs tab) or freezing a package (Package tab) pauses
  with a 409 response while an approval row is pending; the console's
  **Approvals** tab exposes `approve` / `reject` / `request_changes`.
* If SMTP is configured (`RESEARCHOS_SMTP_HOST`) the approver receives a
  real email; otherwise an `.eml` file drops into `var/outbox/` with a JSON
  sidecar so the workflow remains testable without a relay.
* Reminders can be triggered manually with `POST /api/approvals/scan/reminders`
  (there is a button on the Approvals tab) and expirations with
  `POST /api/approvals/scan/expire`.

### Background context bundles (ZIP at intake)

The project create wizard has an optional **Background materials** file
picker. Uploads must be `.zip` and ≤ 512 MB. After upload the backend
streams the archive to disk, verifies the PK header, stores it under
`var/artifacts/<project>/context/`, safely extracts it (zip-slip rejected),
and indexes text-like files (`.md .txt .json .yaml .csv .log`) to splice
into the idea-generation prompt. The Brief tab surfaces a summary row per
bundle.

## Tests

```bash
cd researchos-local/backend
source .venv/bin/activate
pytest -q
```

The suite runs the full pipeline in mock mode under pytest including ZIP
creation on a tmp path.

## Environment variables

| Var                           | Default                                           | Purpose                                |
| ----------------------------- | ------------------------------------------------- | -------------------------------------- |
| `APP_MASTER_KEY`              | dev placeholder (**change for real usage**)       | Derives Fernet key for secret store    |
| `RESEARCHOS_HOST`             | 127.0.0.1                                         | Bind address                           |
| `RESEARCHOS_PORT`             | 8000                                              | Bind port                              |
| `RESEARCHOS_DB_URL`           | `sqlite:///var/data/researchos.db`                | SQLAlchemy URL                         |
| `RESEARCHOS_ARTIFACTS_DIR`    | `var/artifacts`                                   | Artifact store root                    |
| `RESEARCHOS_WORKSPACES_DIR`   | `var/workspaces`                                  | Experiment workspace root              |
| `RESEARCHOS_PACKAGES_DIR`     | `var/packages`                                    | Packaged ZIP output root               |
| `RESEARCHOS_SECRETS_DIR`      | `var/secrets`                                     | Encrypted secret store root            |
| `RESEARCHOS_MAX_CONCURRENCY`  | 2                                                 | Max parallel run subprocesses          |
| `RESEARCHOS_RUN_TIMEOUT`      | 600                                               | Hard run timeout (seconds)             |
| `RESEARCHOS_DEFAULT_PROVIDER` | `mock`                                            | Fallback provider when nothing routed  |
| `RESEARCHOS_OPENAI_CREDENTIAL_TEST_MODEL` | `gpt-4.1-mini`                       | Model used by `/api/providers/test` for OpenAI |
| `RESEARCHOS_ANTHROPIC_CREDENTIAL_TEST_MODEL` | `claude-sonnet-4-6`               | Model used by `/api/providers/test` for Anthropic |

## Troubleshooting

* **Backend won't start — `ModuleNotFoundError: app`.** Make sure you are in
  `backend/` when running uvicorn, and that the venv is activated.
* **Frontend can't reach backend.** The Vite dev server proxies `/api/*` to
  `http://127.0.0.1:8000`. If you changed the backend port, update
  `frontend/vite.config.ts`.
* **`APP_MASTER_KEY must be set to at least 16 characters`.** Edit your
  `.env` and restart the backend.
* **"cannot decrypt stored credential"** after you rotated `APP_MASTER_KEY`
  **or** deleted `var/secrets/.salt`. That is expected — old ciphertexts
  can't be decrypted with the new derived key. Delete `var/secrets/*.enc`
  plus the `provider_credentials` rows in `var/data/researchos.db` (or the
  whole DB) and re-create the credentials through the modal. A fresh
  `.salt` will be generated on next boot.
