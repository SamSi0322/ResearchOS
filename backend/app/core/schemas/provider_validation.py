"""Canonical provider-validation result shape.

Used by both ``/api/providers/test`` (credential check) and ``/api/smoke/ping``
(runtime policy check). The point is to give the operator one unambiguous
classification instead of a generic ok/fail with a free-form error string.

Every response carries a ``category`` so the UI can decide what message to
show without parsing free text:

    ok             — credential works and the tiny call returned output
    auth_error     — provider returned 401/403 (bad key / revoked / wrong org)
    model_error    — provider returned 404 or a model-id validation failure
                     ("model not found / unsupported")
    network_error  — transport / DNS / timeout / TLS / connection refused
    config_error   — missing credential, missing base url, unsupported provider
    provider_error — everything else (rate limits, 5xx, bad request, …)

``requested_model`` / ``actual_model`` make model aliasing visible — the
wire-level id may be different from the one the policy asked for. ``http_status``
is the upstream status code when we actually reached the provider; it is
``None`` for network and config errors.

Raw provider response bodies are NEVER attached — they can echo prompt content
or leak other structured fragments. Only a short, safe ``message`` and an
optional ``response_preview`` (truncated completion text, only on success) are
returned.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class ValidationCategory(str, Enum):
    ok = "ok"
    auth_error = "auth_error"
    model_error = "model_error"
    network_error = "network_error"
    config_error = "config_error"
    provider_error = "provider_error"


class ProviderValidationResult(BaseModel):
    ok: bool
    category: ValidationCategory
    provider: str
    requested_model: str | None = None
    actual_model: str | None = None
    http_status: int | None = None
    provider_error_code: str | None = None
    message: str
    response_preview: str | None = None
    latency_ms: int = 0
    # ``execution_mode`` is always ``headless_api`` for this system. Pinned
    # here so the UI can display it on every validation result, and so a
    # future reader cannot mistake the audit trail for an interactive
    # Claude Code / Codex session.
    execution_mode: str = Field(default="headless_api", frozen=True)


class ProviderValidationLogOut(BaseModel):
    """Persistent row for one past validation call."""

    id: str
    credential_id: str | None = None
    source: str  # "providers_test" | "smoke_ping"
    provider: str
    category: str
    http_status: int | None = None
    provider_error_code: str | None = None
    requested_model: str | None = None
    actual_model: str | None = None
    latency_ms: int = 0
    message: str = ""
    execution_mode: str = "headless_api"
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
