"""Tests for the canonical provider-validation flow.

Covers:
    1. ``classify_exception`` maps each error shape to the right category.
    2. ``/providers/test`` returns ``ValidationCategory.auth_error`` for a
       401 and ``ValidationCategory.model_error`` for a 404 — the two
       failure modes the operator previously saw as generic "fail".
    3. ``/providers/test`` uses the dedicated credential-test model, not
       ``cred.default_model`` (so a future-dated default cannot poison
       validation).
    4. ``/smoke/ping`` exercises the runtime policy path (model alias layer
       applied) and returns the canonical result shape.
    5. Validation results never contain raw key material or a full upstream
       response body.
    6. Runtime does not shell out to interactive ``codex`` / ``claude``
       binaries.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest


# --- classifier unit tests ------------------------------------------------


def test_classify_http_401_is_auth_error():
    from app.providers.base import ProviderError
    from app.services.provider_validation import classify_exception
    from app.core.schemas.provider_validation import ValidationCategory

    err = ProviderError("openai", "http 401: invalid_api_key: Invalid key", status=401)
    cls = classify_exception(err)
    assert cls.category is ValidationCategory.auth_error
    assert cls.http_status == 401
    assert cls.provider_error_code == "invalid_api_key"


def test_classify_http_403_is_auth_error():
    from app.providers.base import ProviderError
    from app.services.provider_validation import classify_exception
    from app.core.schemas.provider_validation import ValidationCategory

    err = ProviderError("anthropic", "http 403: forbidden: no access", status=403)
    cls = classify_exception(err)
    assert cls.category is ValidationCategory.auth_error


def test_classify_http_404_is_model_error():
    from app.providers.base import ProviderError
    from app.services.provider_validation import classify_exception
    from app.core.schemas.provider_validation import ValidationCategory

    err = ProviderError(
        "openai", "http 404: model_not_found: The model was not found", status=404
    )
    cls = classify_exception(err)
    assert cls.category is ValidationCategory.model_error
    assert cls.http_status == 404


def test_classify_http_400_with_model_hint_is_model_error():
    """OpenAI frequently 400s (not 404s) on a bad model id."""
    from app.providers.base import ProviderError
    from app.services.provider_validation import classify_exception
    from app.core.schemas.provider_validation import ValidationCategory

    err = ProviderError(
        "openai",
        "http 400: invalid_request_error: The model `gpt-xyz` does not exist",
        status=400,
    )
    cls = classify_exception(err)
    assert cls.category is ValidationCategory.model_error, cls


def test_classify_network_error_no_status():
    from app.providers.base import ProviderError
    from app.services.provider_validation import classify_exception
    from app.core.schemas.provider_validation import ValidationCategory

    err = ProviderError("openai", "network error: connection refused")
    cls = classify_exception(err)
    assert cls.category is ValidationCategory.network_error


def test_classify_bare_httpx_error_is_network_error():
    from app.services.provider_validation import classify_exception
    from app.core.schemas.provider_validation import ValidationCategory

    class _T(httpx.HTTPError):
        pass

    cls = classify_exception(_T("boom"))
    assert cls.category is ValidationCategory.network_error


def test_classify_lookup_or_value_is_config_error():
    from app.services.provider_validation import classify_exception
    from app.core.schemas.provider_validation import ValidationCategory

    assert (
        classify_exception(LookupError("missing cred")).category
        is ValidationCategory.config_error
    )
    assert (
        classify_exception(ValueError("bad url")).category
        is ValidationCategory.config_error
    )


def test_classify_generic_5xx_is_provider_error():
    from app.providers.base import ProviderError
    from app.services.provider_validation import classify_exception
    from app.core.schemas.provider_validation import ValidationCategory

    err = ProviderError("openai", "http 500: server_error: oops", status=500)
    cls = classify_exception(err)
    assert cls.category is ValidationCategory.provider_error
    assert cls.http_status == 500


# --- end-to-end /providers/test --------------------------------------------


class _FakeHTTPXResponse:
    """Just enough ``httpx.Response``-shaped surface for the adapters' error
    path."""

    def __init__(self, status_code: int, body: dict[str, Any] | str):
        self.status_code = status_code
        if isinstance(body, str):
            self.text = body
            self._json_err = ValueError("not json")
        else:
            import json as _j

            self.text = _j.dumps(body)
            self._data = body
            self._json_err = None

    def json(self):
        if self._json_err:
            raise self._json_err
        return self._data


def _install_fake_openai(monkeypatch, status: int, body: dict[str, Any] | str):
    async def _fake_post(self, url, headers=None, json=None):
        return _FakeHTTPXResponse(status, body)

    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)


@pytest.mark.asyncio
async def test_test_connection_wrong_key_is_auth_error(fresh_db, monkeypatch):
    """A real-shape 401 from the provider must surface as auth_error, NOT
    as a generic failure or a model error."""
    from app.core.schemas import ProviderCredentialIn, ProviderTestIn
    from app.core.schemas.provider_validation import ValidationCategory
    from app.db.session import SessionLocal
    from app.services import ProviderSecretService

    _install_fake_openai(
        monkeypatch,
        401,
        {"error": {"type": "invalid_api_key", "message": "Invalid API key"}},
    )

    with SessionLocal() as db:
        svc = ProviderSecretService(db)
        cred = svc.add(
            ProviderCredentialIn(
                provider="openai",
                label="bad-key",
                api_key="sk-bogus-testkey-1234567890",
                is_default=True,
            )
        )
        res = await svc.test_connection(ProviderTestIn(credential_id=cred.id))

    assert res.ok is False
    assert res.category is ValidationCategory.auth_error
    assert res.http_status == 401
    assert res.provider == "openai"
    # Credential-test model must have been used (not whatever default the
    # operator stored) so validation does not depend on policy/alias state.
    assert res.requested_model == "gpt-4.1-mini"
    # Message is short, structured, and never echoes the raw key.
    assert "sk-bogus" not in res.message
    assert res.response_preview is None
    assert res.execution_mode == "headless_api"


@pytest.mark.asyncio
async def test_test_connection_bad_model_is_model_error(fresh_db, monkeypatch):
    """A 404 on the model id must be reported as model_error, not auth_error."""
    from app.core.schemas import ProviderCredentialIn, ProviderTestIn
    from app.core.schemas.provider_validation import ValidationCategory
    from app.db.session import SessionLocal
    from app.services import ProviderSecretService

    _install_fake_openai(
        monkeypatch,
        404,
        {"error": {"type": "model_not_found", "message": "no such model"}},
    )

    with SessionLocal() as db:
        svc = ProviderSecretService(db)
        cred = svc.add(
            ProviderCredentialIn(
                provider="openai",
                label="k",
                api_key="sk-valid-key-abc-1234567890",
                is_default=True,
            )
        )
        res = await svc.test_connection(
            ProviderTestIn(credential_id=cred.id, model="gpt-does-not-exist")
        )

    assert res.ok is False
    assert res.category is ValidationCategory.model_error
    assert res.http_status == 404
    assert res.requested_model == "gpt-does-not-exist"


@pytest.mark.asyncio
async def test_test_connection_uses_dedicated_credential_test_model(
    fresh_db, monkeypatch
):
    """The credential-test model must be independent from ``cred.default_model``.

    Store a credential with a future-dated default that would 404 today; the
    validation call must still hit the cheap test model and succeed when the
    fake provider returns 200.
    """
    from app.core.schemas import ProviderCredentialIn, ProviderTestIn
    from app.core.schemas.provider_validation import ValidationCategory
    from app.db.session import SessionLocal
    from app.services import ProviderSecretService

    body = {
        "id": "resp_x",
        "output_text": "OK",
        "usage": {"total_tokens": 4},
        "status": "completed",
    }
    _install_fake_openai(monkeypatch, 200, body)

    seen_models: list[str] = []

    async def _post(self, url, headers=None, json=None):
        seen_models.append(json.get("model"))
        return _FakeHTTPXResponse(200, body)

    monkeypatch.setattr(httpx.AsyncClient, "post", _post)

    with SessionLocal() as db:
        svc = ProviderSecretService(db)
        cred = svc.add(
            ProviderCredentialIn(
                provider="openai",
                label="future-default",
                api_key="sk-good-key-1234567890",
                default_model="gpt-5.4",  # future-dated
                is_default=True,
            )
        )
        res = await svc.test_connection(ProviderTestIn(credential_id=cred.id))

    assert res.ok is True
    assert res.category is ValidationCategory.ok
    assert seen_models, "adapter was never called"
    assert seen_models[-1] == "gpt-4.1-mini", (
        f"expected credential-test model, got {seen_models[-1]}"
    )


@pytest.mark.asyncio
async def test_test_connection_network_failure(fresh_db, monkeypatch):
    from app.core.schemas import ProviderCredentialIn, ProviderTestIn
    from app.core.schemas.provider_validation import ValidationCategory
    from app.db.session import SessionLocal
    from app.services import ProviderSecretService

    async def _post(self, url, headers=None, json=None):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(httpx.AsyncClient, "post", _post)

    with SessionLocal() as db:
        svc = ProviderSecretService(db)
        cred = svc.add(
            ProviderCredentialIn(
                provider="openai",
                label="k",
                api_key="sk-any-key-1234567890",
                is_default=True,
            )
        )
        res = await svc.test_connection(ProviderTestIn(credential_id=cred.id))

    assert res.ok is False
    assert res.category is ValidationCategory.network_error
    assert res.http_status is None


@pytest.mark.asyncio
async def test_test_connection_mock_is_ok(fresh_db):
    from app.core.schemas import ProviderCredentialIn, ProviderTestIn
    from app.core.schemas.provider_validation import ValidationCategory
    from app.db.session import SessionLocal
    from app.services import ProviderSecretService

    with SessionLocal() as db:
        svc = ProviderSecretService(db)
        cred = svc.add(
            ProviderCredentialIn(
                provider="mock",
                label="m",
                api_key="mock-secret-abcdef",
                is_default=True,
            )
        )
        res = await svc.test_connection(ProviderTestIn(credential_id=cred.id))

    assert res.ok is True
    assert res.category is ValidationCategory.ok
    assert res.provider == "mock"
    # Mock provider always returns execution_mode=headless_api — the frozen
    # field on the schema enforces that.
    assert res.execution_mode == "headless_api"


# --- /smoke/ping runtime policy --------------------------------------------


@pytest.mark.asyncio
async def test_smoke_ping_uses_runtime_policy_path(fresh_db, monkeypatch):
    """``/smoke/ping`` must route through ``resolve_model_policy`` +
    ``router.resolve_with_policy`` so the alias layer is applied."""
    from app.core.schemas import ProviderCredentialIn
    from app.db.session import SessionLocal
    from app.services import ProviderSecretService

    # The call must reach the HTTP layer; record the model id sent on the wire.
    wire_models: list[str] = []
    body = {"id": "resp_p", "output_text": "OK", "usage": {"total_tokens": 4}}

    async def _post(self, url, headers=None, json=None):
        wire_models.append(json.get("model"))
        return _FakeHTTPXResponse(200, body)

    monkeypatch.setattr(httpx.AsyncClient, "post", _post)

    # Force production mode so the test exercises the alias layer rather
    # than falling back to the cheap smoke model.
    monkeypatch.setenv("RESEARCHOS_RUN_MODE", "production")
    monkeypatch.setenv("RESEARCHOS_SMOKE_MODE", "false")
    from app.config import reset_settings_cache

    reset_settings_cache()

    with SessionLocal() as db:
        ProviderSecretService(db).add(
            ProviderCredentialIn(
                provider="openai",
                label="k",
                api_key="sk-any-key-1234567890",
                is_default=True,
            )
        )

    # Exercise the route function directly (no HTTP client needed).
    from app.api.routes.smoke import SmokePingIn, smoke_ping
    from app.db.session import SessionLocal as _S

    with _S() as db:
        res = await smoke_ping(SmokePingIn(provider="openai"), db=db)

    assert res.ok is True, res.message
    assert res.provider == "openai"
    # Requested model is whatever the production policy asked for for the
    # code_generation phase. Current production sends the Pro model directly.
    assert res.requested_model == "gpt-5.4-pro"
    assert wire_models and wire_models[-1] == "gpt-5.4-pro", wire_models
    assert res.execution_mode == "headless_api"


@pytest.mark.asyncio
async def test_smoke_ping_without_credential_returns_config_error(fresh_db):
    from app.api.routes.smoke import SmokePingIn, smoke_ping
    from app.core.schemas.provider_validation import ValidationCategory
    from app.db.session import SessionLocal

    with SessionLocal() as db:
        res = await smoke_ping(SmokePingIn(provider="openai"), db=db)
    assert res.ok is False
    assert res.category is ValidationCategory.config_error


# --- secret redaction in validation responses -----------------------------


@pytest.mark.asyncio
async def test_validation_response_never_contains_raw_key(fresh_db, monkeypatch):
    from app.core.schemas import ProviderCredentialIn, ProviderTestIn
    from app.db.session import SessionLocal
    from app.services import ProviderSecretService

    secret = "sk-DEFINITELY-SENSITIVE-KEY-abcdef012345"
    body = {
        "error": {
            "type": "invalid_api_key",
            "message": f"Invalid key {secret}",
        }
    }
    _install_fake_openai(monkeypatch, 401, body)

    with SessionLocal() as db:
        svc = ProviderSecretService(db)
        cred = svc.add(
            ProviderCredentialIn(
                provider="openai",
                label="paranoid",
                api_key=secret,
                is_default=True,
            )
        )
        res = await svc.test_connection(ProviderTestIn(credential_id=cred.id))

    # The adapter already truncates/summarises the upstream message, so the
    # raw key material must not bubble up through the classifier into the
    # final message field. (The adapter caps at 120 chars; we additionally
    # assert the literal secret never appears.)
    serialised = res.model_dump_json()
    assert secret not in serialised, serialised


# --- runtime guardrail ----------------------------------------------------


def test_job_runner_refuses_agent_binaries():
    from app.workers.job_runner import assert_not_interactive_agent

    assert_not_interactive_agent("/usr/bin/python3")  # ok

    for forbidden in (
        "codex",
        "/usr/local/bin/codex",
        "C:\\tools\\codex.exe",
        "codex.cmd",
        "claude",
        "claude.exe",
        "claude-code",
    ):
        with pytest.raises(RuntimeError) as ei:
            assert_not_interactive_agent(forbidden)
        assert "headless" in str(ei.value).lower()


def test_no_runtime_code_shells_out_to_codex_or_claude_binary():
    """Walk app/ and make sure no file in the runtime path spawns an
    interactive coding agent by name."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "app"
    # subprocess + ``codex`` / ``claude`` / ``claude-code`` strings on the
    # same line are the shape we want to catch. Docstrings / comments that
    # mention those names are fine.
    suspicious = re.compile(
        r"(subprocess\.(run|Popen)|asyncio\.create_subprocess_exec)"
        r"[\s\S]{0,80}[\"']("  # tolerate multiline kwargs
        r"codex|claude|claude-code|codex\.exe|claude\.exe|claude-code\.exe"
        r")[\"']",
        re.IGNORECASE,
    )
    offenders: list[str] = []
    for py in root.rglob("*.py"):
        text = py.read_text(encoding="utf-8", errors="ignore")
        if suspicious.search(text):
            offenders.append(str(py.relative_to(root.parent)))
    assert not offenders, (
        f"runtime code must not spawn interactive agent binaries, "
        f"offenders: {offenders}"
    )


# --- run metadata records headless_api -----------------------------------


def test_run_provider_routing_includes_execution_mode():
    """Static assertion: the experiment runner sets execution_mode=headless_api
    on every run's provider_routing blob."""
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "services"
        / "experiment_runner_service.py"
    ).read_text(encoding="utf-8")
    assert '"execution_mode": "headless_api"' in src, (
        "experiment_runner_service must stamp execution_mode=headless_api on "
        "every ExperimentRun.provider_routing"
    )


def test_package_manifest_includes_execution_mode():
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "services"
        / "package_service.py"
    ).read_text(encoding="utf-8")
    assert '"execution_mode": "headless_api"' in src
