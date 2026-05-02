from __future__ import annotations

from sqlalchemy import Boolean, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class ProviderCredential(Base, TimestampMixin):
    """Metadata about a stored provider credential.

    The actual API key ciphertext lives in the filesystem-backed secret store.
    We only persist non-sensitive metadata here (provider name, label, masked
    preview, default model, etc.).
    """

    __tablename__ = "provider_credentials"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32))  # openai | anthropic | mock
    label: Mapped[str] = mapped_column(String(128), default="default")
    masked_preview: Mapped[str] = mapped_column(String(64), default="")
    default_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    default_for: Mapped[list] = mapped_column(JSON, default=list)  # list of TaskKind values
    base_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    secret_ref: Mapped[str] = mapped_column(String(128))  # filename inside secret_store


class ProviderValidationLog(Base, TimestampMixin):
    """Persistent record of every ``/providers/test`` + ``/smoke/ping`` call.

    Stores only structured metadata — never the key, never a raw upstream
    response body. The UI uses this to replace the in-memory "Last
    validation" cell with a persistent one.
    """

    __tablename__ = "provider_validation_logs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # Nullable: ``/smoke/ping`` doesn't have a credential id (it resolves via
    # the runtime policy path), so we still log it but leave the FK empty.
    credential_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("provider_credentials.id", ondelete="SET NULL"),
        nullable=True,
    )
    source: Mapped[str] = mapped_column(String(32), default="providers_test")
    provider: Mapped[str] = mapped_column(String(32))
    category: Mapped[str] = mapped_column(String(32))
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    provider_error_code: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    requested_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    actual_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str] = mapped_column(Text, default="")
    execution_mode: Mapped[str] = mapped_column(String(32), default="headless_api")
