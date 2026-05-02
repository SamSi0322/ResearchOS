"""Classifier used by the provider-test and smoke-ping flows.

The provider adapters raise ``ProviderError`` with a ``status`` field on any
upstream 4xx/5xx; ``httpx.HTTPError`` (wrapped by adapters) on transport
failures; ``ValueError`` / ``KeyError`` on local config problems. This
module turns those into the canonical
``ProviderValidationResult.category`` so the UI can show an unambiguous
message instead of parsing a free-form error string.

We intentionally never include the raw provider response body. The adapters
already extract a short structured summary; we keep that and trim it further
here so nothing larger than ~160 chars leaks upstream.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.schemas.provider_validation import (
    ProviderValidationResult,
    ValidationCategory,
)
from app.providers.base import ProviderError


# Snippets in structured error payloads that strongly suggest "model id
# not recognised" rather than a generic 4xx. Ordered roughly by specificity.
# Patterns are case-insensitive.
_MODEL_ERROR_HINTS = (
    r"model[_\s-]*not[_\s-]*found",
    r"no such model",
    r"invalid[_\s-]*model",
    r"unsupported[_\s-]*model",
    r"does not (?:exist|have access)",
    r"model `[^`]+` does not exist",
    r"does not exist or you do not have access to it",
    r"the model `[^`]+` was not found",
)
_MODEL_ERROR_RE = re.compile("|".join(_MODEL_ERROR_HINTS), re.IGNORECASE)


@dataclass
class ClassifiedError:
    category: ValidationCategory
    http_status: int | None
    provider_error_code: str | None
    message: str


def _short(msg: str, limit: int = 160) -> str:
    msg = (msg or "").strip()
    if len(msg) > limit:
        return msg[: limit - 1] + "…"
    return msg


def _extract_provider_code(err_message: str) -> str | None:
    """Both OpenAI and Anthropic adapters format provider messages as
    ``http <status>: <type>: <message>`` — pull out the ``<type>``.
    """
    m = re.match(r"http\s+\d+:\s*([a-zA-Z0-9_.\-]+):", err_message or "")
    if m:
        return m.group(1)
    return None


def classify_exception(exc: BaseException) -> ClassifiedError:
    """Map an adapter / service exception onto the canonical category."""
    # Local configuration problems: missing credential, missing secret, unsupported provider.
    if isinstance(exc, (KeyError, LookupError, FileNotFoundError)):
        return ClassifiedError(
            ValidationCategory.config_error,
            http_status=None,
            provider_error_code=None,
            message=_short(f"config error: {exc!s}"),
        )
    if isinstance(exc, ValueError):
        return ClassifiedError(
            ValidationCategory.config_error,
            http_status=None,
            provider_error_code=None,
            message=_short(f"config error: {exc!s}"),
        )

    if isinstance(exc, ProviderError):
        status = exc.status
        msg = exc.message or str(exc)
        code = _extract_provider_code(msg)

        # Network errors are wrapped by the adapters with ``network error:``
        # prefixed, and carry no HTTP status.
        if status is None and msg.lower().startswith("network error"):
            return ClassifiedError(
                ValidationCategory.network_error,
                http_status=None,
                provider_error_code=code,
                message=_short(msg),
            )

        if status in (401, 403):
            return ClassifiedError(
                ValidationCategory.auth_error,
                http_status=status,
                provider_error_code=code,
                message=_short(msg),
            )
        if status == 404:
            return ClassifiedError(
                ValidationCategory.model_error,
                http_status=status,
                provider_error_code=code,
                message=_short(msg),
            )
        if status == 400 and _MODEL_ERROR_RE.search(msg):
            # OpenAI often returns 400 with "The model `xyz` does not exist"
            # rather than a 404. Catch that variant explicitly.
            return ClassifiedError(
                ValidationCategory.model_error,
                http_status=status,
                provider_error_code=code,
                message=_short(msg),
            )
        if status is not None and status >= 400:
            return ClassifiedError(
                ValidationCategory.provider_error,
                http_status=status,
                provider_error_code=code,
                message=_short(msg),
            )

        # No status and not network-prefixed: treat as opaque provider issue.
        return ClassifiedError(
            ValidationCategory.provider_error,
            http_status=None,
            provider_error_code=code,
            message=_short(msg),
        )

    if isinstance(exc, httpx.HTTPError):
        return ClassifiedError(
            ValidationCategory.network_error,
            http_status=None,
            provider_error_code=None,
            message=_short(f"network error: {exc!s}"),
        )

    # Defensive catch-all: don't leak internal tracebacks, but still give
    # the operator something actionable.
    return ClassifiedError(
        ValidationCategory.provider_error,
        http_status=None,
        provider_error_code=None,
        message=_short(f"unexpected error: {type(exc).__name__}"),
    )


def category_message(
    category: ValidationCategory,
    *,
    provider: str,
    requested_model: str | None,
    actual_model: str | None,
    http_status: int | None,
    upstream: str | None,
) -> str:
    """Human-readable summary for a given category. The UI has its own
    translation layer but both backends and automation scripts consume this
    string directly, so we keep it short and unambiguous.
    """
    if category is ValidationCategory.ok:
        model = actual_model or requested_model or ""
        return f"Credential valid against {provider}/{model}."
    if category is ValidationCategory.auth_error:
        return f"Credential invalid for {provider} (http {http_status or '?'})."
    if category is ValidationCategory.model_error:
        shown = actual_model or requested_model or "?"
        return (
            f"Credential accepted by {provider} but model '{shown}' is unavailable "
            f"(http {http_status or '?'}). The key itself is fine."
        )
    if category is ValidationCategory.network_error:
        return f"{provider} unreachable: {upstream or 'network error'}."
    if category is ValidationCategory.config_error:
        return f"Configuration incomplete: {upstream or 'missing credential or provider'}."
    return f"{provider} returned an error (http {http_status or '?'}): {upstream or ''}"


def persist_validation(
    db,
    *,
    result: ProviderValidationResult,
    credential_id: str | None,
    source: str,
) -> None:
    """Write a ``ProviderValidationLog`` row for a test/ping invocation.

    Best-effort: a DB write failure here must never affect the operator's
    validation call. The caller has already committed the audit event.
    """
    try:
        from app.core.models import ProviderValidationLog
        from app.utils import new_id

        row = ProviderValidationLog(
            id=new_id("pvl"),
            credential_id=credential_id,
            source=source,
            provider=result.provider,
            category=result.category.value,
            http_status=result.http_status,
            provider_error_code=result.provider_error_code,
            requested_model=result.requested_model,
            actual_model=result.actual_model,
            latency_ms=int(result.latency_ms or 0),
            message=result.message or "",
            execution_mode=result.execution_mode,
        )
        db.add(row)
        db.commit()
    except Exception:  # noqa: BLE001
        # Swallow — validation history is a nice-to-have, not load-bearing.
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass


def build_result(
    *,
    ok: bool,
    category: ValidationCategory,
    provider: str,
    requested_model: str | None,
    actual_model: str | None,
    http_status: int | None,
    provider_error_code: str | None,
    upstream_message: str | None,
    response_preview: str | None,
    latency_ms: int,
) -> ProviderValidationResult:
    message = category_message(
        category,
        provider=provider,
        requested_model=requested_model,
        actual_model=actual_model,
        http_status=http_status,
        upstream=upstream_message,
    )
    return ProviderValidationResult(
        ok=ok,
        category=category,
        provider=provider,
        requested_model=requested_model,
        actual_model=actual_model,
        http_status=http_status,
        provider_error_code=provider_error_code,
        message=message,
        response_preview=response_preview,
        latency_ms=latency_ms,
    )


def credential_test_model_for(provider: str, settings: Any) -> str | None:
    """Return the per-provider credential-test model.

    Deliberately decoupled from the production/smoke policy: validation should
    prove the credential works, not that today's production model id is live.
    Operators can override via env vars (``RESEARCHOS_OPENAI_CREDENTIAL_TEST_MODEL``
    etc.) without touching the policy table.
    """
    if provider == "openai":
        return getattr(settings, "openai_credential_test_model", None) or "gpt-4.1-mini"
    if provider == "anthropic":
        return (
            getattr(settings, "anthropic_credential_test_model", None)
            or "claude-sonnet-4-6"
        )
    if provider == "mock":
        return "mock-1"
    return None
