# Smoke mode: cheap real-provider validation

Smoke mode exists so the mentor team can verify the full pipeline against
**real** provider APIs without spending real money. It clamps models,
token budgets, idea count, and prompt length to small values, and wires
concurrency at a tiny scale (two ideas in parallel by default).

Use it when:

- you have just rotated provider keys and want to confirm the chain still works,
- you want to validate a new adapter, prompt, or model selection,
- you want to produce a sample ZIP package with real-provider artifacts.

Do **not** use it for real research runs — the limits are intentionally too
small.

---

## 1. Where to put API keys

Preferred: set environment variables before launching the backend.

```bash
export OPENAI_API_KEY=sk-proj-...
export ANTHROPIC_API_KEY=sk-ant-api03-...
```

Alternative: drop a plain-text `API_KEYS.txt` file. The backend looks in
**two** places by default and uses whichever it finds first:

```
researchos-local/API_KEYS.txt
../API_KEYS.txt      (the parent of researchos-local/)
```

You can also set `RESEARCHOS_API_KEYS_FILE` to override the path.

Two formats are accepted (mix freely):

```
# Canonical KEY=VALUE (preferred, documented)
OPENAI_API_KEY=sk-proj-abc...
ANTHROPIC_API_KEY=sk-ant-api03-def...
OPENAI_SMOKE_MODEL=gpt-4.1-mini
ANTHROPIC_SMOKE_MODEL=claude-haiku-4-5-20251001
```

```
# Free-form labelled list (also accepted)
- [ ] GPT 1: sk-proj-...
- [ ] Anthropic 1: sk-ant-api03-...
```

Blank lines and lines starting with `#`, `//`, or `;` are ignored. First
match per provider wins; lines with unrecognised labels are ignored.

**`API_KEYS.txt` is in `.gitignore`.** It is a local-dev convenience only.
Never commit it. Never paste real keys into browser storage or logs.

### What the backend does at startup

1. Calls `credential_bootstrap_service.run_bootstrap()` once on FastAPI
   `startup` and again on each `/api/smoke/*` request (idempotent).
2. Precedence: process env vars → existing backend-stored credentials →
   `API_KEYS.txt`.
3. If no source yields a value for a provider, no credential is created and
   the router falls back to the mock adapter. Unrelated routes keep working.
4. Credentials are stored encrypted via the per-install salted Fernet key
   (see `docs/architecture.md`). Raw keys never leave backend memory.

### Sanity-check what the backend sees

```bash
curl -s http://localhost:8000/api/smoke/health | jq
```

You will see `openai_ready`, `anthropic_ready`, masked previews, and which
source filled each credential (`env` / `file` / `existing`). No raw keys.

---

## 2. Turning on smoke mode

Set `RESEARCHOS_SMOKE_MODE=true` before starting the backend.

```bash
# on macOS / linux
export RESEARCHOS_SMOKE_MODE=true

# on Windows (powershell)
$env:RESEARCHOS_SMOKE_MODE="true"
```

When it is on, the backend:

- clamps every `CompletionRequest.max_tokens` to `RESEARCHOS_SMOKE_MAX_TOKENS`
  (default 400),
- truncates prompts + system prompts to `RESEARCHOS_SMOKE_PROMPT_BUDGET_CHARS`
  (default 6000),
- shortens the provider HTTP timeout to `RESEARCHOS_SMOKE_REQUEST_TIMEOUT`
  (default 30s),
- forces the configured smoke models:
  `RESEARCHOS_OPENAI_SMOKE_MODEL` (default `gpt-4.1-mini`) and
  `RESEARCHOS_ANTHROPIC_SMOKE_MODEL` (default `claude-haiku-4-5-20251001`),
- caps every batch at `RESEARCHOS_MAX_IDEAS_PER_RUN` ideas (default 2),
- caps concurrent idea pipelines at `RESEARCHOS_CONCURRENCY_PER_BATCH`
  (default 2).

Every clamp has a `RESEARCHOS_*` override, so you can dial this up later
without code changes.

---

## 3. How to run a smoke

### Option A — CLI (easiest, no HTTP round-trips)

From `researchos-local/backend` with the venv active:

```bash
# Real providers, smoke mode on, 2 ideas, builder+reviewer worker:
export RESEARCHOS_SMOKE_MODE=true
python -m app.cli.smoke --ideas 2 --worker two_step

# Mock only, no cost, no network:
python -m app.cli.smoke --mock --ideas 2 --worker claude_code
```

Exit 0 means the whole chain worked (idea generation → spec → code worker
→ experiment run → result analysis → draft → review → package ZIP).

Expected output:

```
[smoke] bootstrap: openai=file anthropic=file sources=['file:.../API_KEYS.txt']
[smoke] generated 2 idea(s)
[smoke]   [OK] idea=abcd... run=... status=succeeded result=succeeded_valid verdict=inconclusive claims=2 err=None
[smoke]   [OK] idea=efgh... run=... status=succeeded result=succeeded_valid verdict=inconclusive claims=2 err=None
[smoke] draft v1 generated id=...
[smoke] review issues: 5
[smoke] package v1 bytes=... at ...var/packages/smoke_project/package_v1.zip
[smoke] {"project_id": "smoke_project", ...}
```

### Option B — HTTP route (good if the backend is already running)

```bash
curl -s -X POST http://localhost:8000/api/smoke/run \
     -H 'content-type: application/json' \
     -d '{"idea_count":2,"worker":"two_step"}' | jq
```

### Option C — one-shot connectivity ping

```bash
curl -s -X POST http://localhost:8000/api/smoke/ping \
     -H 'content-type: application/json' \
     -d '{"provider":"openai"}' | jq

curl -s -X POST http://localhost:8000/api/smoke/ping \
     -H 'content-type: application/json' \
     -d '{"provider":"anthropic"}' | jq
```

These fire exactly **one** completion of 8 tokens with `temperature=0`.
Useful for verifying the key + network path without running the full chain.

---

## 4. What to look at afterwards

Open the frontend and check:

- **Providers**: both credentials listed with masked previews.
- **Projects** → the auto-created "smoke mode: real-provider validation" project.
- **Runs** tab: 2 experiment runs with real `provider_routing.provider` /
  `provider_routing.model` values.
- **Package** tab: the latest ZIP, downloadable.

Or from the CLI:

```bash
unzip -l researchos-local/var/packages/smoke_project/package_v1.zip | head -30
```

The manifest lists `counts.runs >= 2` and every artifact has a sha256.

---

## 5. Guardrails

- We never log raw key values. The logging filter in `app/utils/logger.py`
  redacts known key shapes at handler time.
- We never echo raw keys in API responses or ZIP manifests.
- We never persist keys in browser storage.
- Smoke mode hard-caps all of: idea count, tokens per request, prompt
  length, per-stage real-provider call budget.

If you need a bigger run, drop `RESEARCHOS_SMOKE_MODE=true` and raise the
relevant env var — but first make sure the project, billing, and model
choice are deliberate.
