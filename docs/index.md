# ResearchOS

A local-first research decision and execution system. Turn a research brief into runnable experiments, evidence-checked claims, and a packaged manuscript draft — without anything leaving your laptop.

!!! info "Status"
    Local MVP, single-tenant, single-host. Open-source under [AGPL-3.0](https://github.com/SamSi0322/ResearchOS/blob/main/LICENSE). Not a hosted service. Not a paper generator. Every draft requires human validation before it leaves the system.

## What's here

- **[Architecture](architecture.md)** — three-layer separation (providers / workers / services), data model, headless-by-design runtime, known limitations.
- **[Local run](local-run.md)** — end-to-end setup, console walk-through, REST quickstart.
- **[API](api.md)** — REST endpoint reference.
- **[Model policy](model-policy.md)** — phase × provider × model × effort, alias layer, capability gates, pro-tier timeouts.
- **[Smoke mode](smoke-mode.md)** — cheap real-provider validation, the `python -m app.cli.smoke` CLI.

## Quick start (mock mode, no API keys)

```bash
git clone https://github.com/SamSi0322/ResearchOS
cd ResearchOS
cp .env.example .env

# Backend
cd backend
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m app.db.init_db --demo
uvicorn app.main:app --reload --port 8000
```

In a second terminal, from the repo root:

```bash
cd frontend
npm install
npm run dev
```

Open `http://localhost:5173`, go to **Settings → Providers**, add a `mock` credential (any string works), then **Dashboard → New Project** and walk the wizard.

See [Local run](local-run.md) for the full walk-through and how to swap in real OpenAI / Anthropic keys.

## What ResearchOS is not

> **Not a paper generator.** Every draft is decision support that requires human validation. Mock-mode artefacts are tagged `MOCK` end-to-end; the freeze step refuses to ship if any P0 / P1 reviewer issue is open.

> **Not an auto-submission pipeline.** Optional Human-in-the-Loop approval gates pause the pipeline at three named checkpoints; the operator must explicitly approve before each run continues.

> **Not a hosted service.** AGPL-3.0, self-hosted on `localhost`, single-tenant. No managed cloud version, no public API.

## License

ResearchOS is released under the [GNU AGPL-3.0](https://github.com/SamSi0322/ResearchOS/blob/main/LICENSE). Strong copyleft: if you run a modified copy as a network-accessible service, you must offer the corresponding source to its users.

## Contributing

Pull requests welcome. See [CONTRIBUTING.md](https://github.com/SamSi0322/ResearchOS/blob/main/CONTRIBUTING.md) for code constraints (provider router, model policy, audit metadata, run reproducibility, headless runtime, mock-mode tests).
