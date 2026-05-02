"""Smoke-mode integration tests (mock adapter only).

These verify:

* ``apply_smoke_limits`` clamps max_tokens and the prompt budget when smoke
  mode is on, and is a pure no-op when it is off.
* The smoke API route's /health endpoint reports sanitised info only.
* The smoke CLI completes end-to-end with ``--mock``.

The real-provider smoke happens out-of-band; see ``docs/smoke-mode.md``.
"""

from __future__ import annotations

import json
import sys

import pytest


def test_apply_smoke_limits_is_noop_when_off():
    from app.providers.base import CompletionRequest, apply_smoke_limits

    class S:
        smoke_mode = False
        smoke_max_tokens = 100
        smoke_prompt_budget_chars = 500

    req = CompletionRequest(prompt="x" * 9000, max_tokens=3000)
    out = apply_smoke_limits(req, S())
    assert out.max_tokens == 3000
    assert out.prompt == req.prompt


def test_apply_smoke_limits_clamps_when_on():
    from app.providers.base import CompletionRequest, apply_smoke_limits

    class S:
        smoke_mode = True
        smoke_max_tokens = 200
        smoke_prompt_budget_chars = 900

    req = CompletionRequest(prompt="x" * 9000, system="y" * 9000, max_tokens=3000)
    out = apply_smoke_limits(req, S())
    assert out.max_tokens == 200
    assert len(out.prompt) <= 900
    assert out.system is not None and len(out.system) <= 450  # budget//2


@pytest.mark.asyncio
async def test_smoke_health_endpoint_never_echoes_secret(fresh_db):
    from app.api.routes.smoke import smoke_health
    from app.core.schemas import ProviderCredentialIn
    from app.db.session import SessionLocal
    from app.services import ProviderSecretService

    with SessionLocal() as db:
        ProviderSecretService(db).add(
            ProviderCredentialIn(
                provider="mock",
                label="health-test",
                api_key="mock-sentinel-value-xyz12345",
            )
        )

    with SessionLocal() as db:
        body = smoke_health(db).model_dump(mode="json")
    # No raw key fragment may appear anywhere in the response.
    raw = json.dumps(body)
    assert "mock-sentinel-value-xyz12345" not in raw
    assert body["smoke_mode"] in (True, False)
    assert isinstance(body["credentials"], list)
    assert any(c["provider"] == "mock" for c in body["credentials"])


def test_smoke_cli_mock_mode_exit_zero(fresh_db, capsys):
    """``python -m app.cli.smoke --mock --ideas 2`` should return 0.

    This test is intentionally NOT async: main() opens its own event loop via
    asyncio.run().
    """
    from app.cli.smoke import main

    rc = main(["--mock", "--ideas", "2", "--worker", "claude_code"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "package v" in out
    # Never print raw keys.
    for fragment in ("sk-proj-", "sk-ant-"):
        assert fragment not in out


@pytest.mark.asyncio
async def test_smoke_api_run_mock_mode(fresh_db):
    from app.api.routes.smoke import SmokeRunIn, smoke_run
    from app.core.schemas import ProviderCredentialIn
    from app.db.session import SessionLocal
    from app.services import ProviderSecretService

    with SessionLocal() as db:
        ProviderSecretService(db).add(
            ProviderCredentialIn(
                provider="mock",
                label="api-smoke",
                api_key="mock-run-key",
                is_default=True,
            )
        )

    with SessionLocal() as db:
        body = (
            await smoke_run(
                SmokeRunIn(idea_count=2, worker="claude_code"),
                db=db,
            )
        ).model_dump(mode="json")
    assert body["project_id"] == "smoke_project"
    assert len(body["batch"]) <= 2 and body["batch"]
    assert body["package_zip_path"]
