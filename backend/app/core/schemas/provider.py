from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ProviderCredentialIn(BaseModel):
    provider: str  # openai | anthropic | mock
    label: str = "default"
    api_key: str
    default_model: str | None = None
    default_for: list[str] = Field(default_factory=list)
    base_url: str | None = None
    is_default: bool = False
    notes: str | None = None

    @field_validator("provider")
    @classmethod
    def _provider_allowed(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in {"openai", "anthropic", "mock"}:
            raise ValueError("provider must be openai | anthropic | mock")
        return v

    @field_validator("api_key")
    @classmethod
    def _api_key_nonempty(cls, v: str) -> str:
        if not v or len(v.strip()) < 3:
            raise ValueError("api_key is required")
        return v


class ProviderCredentialUpdateIn(BaseModel):
    label: str | None = None
    default_model: str | None = None
    default_for: list[str] | None = None
    base_url: str | None = None
    is_default: bool | None = None
    notes: str | None = None
    api_key: str | None = None  # if supplied, rotates the stored secret


class ProviderCredentialOut(BaseModel):
    id: str
    provider: str
    label: str
    masked_preview: str
    default_model: str | None = None
    default_for: list[str] = Field(default_factory=list)
    base_url: str | None = None
    is_default: bool = False
    notes: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ProviderTestIn(BaseModel):
    credential_id: str
    # Operator-supplied override, rarely needed. Default is a tiny
    # neutral prompt that cannot accidentally echo anything sensitive back.
    prompt: str = "Respond with the single word OK."
    # Optional override for the model used by the validation call. Ignored for
    # mock. When unset we pick the per-provider credential-test model from
    # settings so validation never accidentally uses a future-dated policy id.
    model: str | None = None


# Legacy response shape kept as an alias over the canonical
# ``ProviderValidationResult``. New code should import the canonical one
# directly from ``provider_validation``.
from .provider_validation import ProviderValidationResult as ProviderTestOut  # noqa: E402, F401
