"""Owns the lifecycle of provider credentials (metadata + secret store).

* The raw ``api_key`` never leaves this service once stored.
* The DB row carries only non-sensitive metadata (provider, label, masked
  preview, default model, etc.) plus a ``secret_ref`` pointing at the
  encrypted file.
* ``test_connection`` decrypts the key, asks the adapter to ping the provider,
  and returns a short preview.
"""

from __future__ import annotations

import time

from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.enums import AuditKind, ProviderName
from app.core.models import ProviderCredential
from app.core.schemas import (
    ProviderCredentialIn,
    ProviderCredentialUpdateIn,
    ProviderTestIn,
    ProviderValidationResult,
    ValidationCategory,
)
from app.providers.anthropic_adapter import AnthropicProvider
from app.providers.base import CompletionRequest, ProviderError
from app.providers.mock_adapter import MockProvider
from app.providers.openai_adapter import OpenAIProvider
from app.services.audit_service import AuditService
from app.services.provider_validation import (
    build_result,
    classify_exception,
    credential_test_model_for,
    persist_validation,
)
from app.storage import get_secret_store
from app.utils import get_logger, mask_secret, new_id

logger = get_logger(__name__)


class ProviderSecretService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.secret_store = get_secret_store()
        self.audit = AuditService(db)

    # --- lifecycle ---------------------------------------------------------

    def add(self, payload: ProviderCredentialIn) -> ProviderCredential:
        if payload.is_default:
            self._clear_default_flag()

        meta = self.secret_store.put(provider=payload.provider, api_key=payload.api_key)

        cred = ProviderCredential(
            id=new_id("cred"),
            provider=payload.provider,
            label=payload.label,
            masked_preview=meta.masked_preview,
            default_model=payload.default_model,
            default_for=payload.default_for or [],
            base_url=payload.base_url,
            is_default=payload.is_default,
            notes=payload.notes,
            secret_ref=meta.ref,
        )
        self.db.add(cred)
        try:
            self.db.flush()
            self.audit.log(
                project_id=None,
                kind=AuditKind.provider_credential_added,
                message=f"Added {payload.provider} credential",
                subject_kind="provider_credential",
                subject_id=cred.id,
                payload={"provider": payload.provider, "masked_preview": meta.masked_preview},
            )
            self.db.commit()
        except Exception:
            # DB side failed after the ciphertext was written - don't leave
            # an orphaned secret on disk. Roll back both sides then re-raise.
            self.db.rollback()
            try:
                self.secret_store.delete(meta.ref)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "orphaned secret ref after db rollback",
                    extra={"ref": meta.ref, "provider": payload.provider},
                )
            raise
        return cred

    def update(self, credential_id: str, payload: ProviderCredentialUpdateIn) -> ProviderCredential:
        cred = self._get_or_404(credential_id)
        if payload.is_default is True:
            self._clear_default_flag(exclude_id=cred.id)

        for field in (
            "label",
            "default_model",
            "default_for",
            "base_url",
            "is_default",
            "notes",
        ):
            value = getattr(payload, field)
            if value is not None:
                setattr(cred, field, value)

        # Hold the previous masked preview so we can reason about rollback even
        # though the ciphertext itself is unrecoverable once rotated.
        rotated = False
        if payload.api_key:
            meta = self.secret_store.rotate(
                cred.secret_ref, provider=cred.provider, api_key=payload.api_key
            )
            cred.masked_preview = meta.masked_preview
            rotated = True

        try:
            self.db.flush()
            self.db.commit()
        except Exception:
            self.db.rollback()
            if rotated:
                # We cannot un-rotate the secret; at least log loudly so an
                # operator can re-enter the previous key.
                logger.warning(
                    "credential DB commit failed after secret rotation",
                    extra={"cred_id": cred.id, "provider": cred.provider},
                )
            raise
        return cred

    def delete(self, credential_id: str) -> None:
        cred = self._get_or_404(credential_id)
        # Delete the DB row first, then the ciphertext. If the DB step fails
        # we still hold the ciphertext and the credential is recoverable.  If
        # the ciphertext delete fails after a successful commit, we log the
        # orphan but the credential is already unreferenced.
        secret_ref = cred.secret_ref
        cred_id = cred.id
        cred_provider = cred.provider
        self.db.delete(cred)
        try:
            self.audit.log(
                project_id=None,
                kind=AuditKind.provider_credential_deleted,
                message=f"Deleted {cred_provider} credential",
                subject_kind="provider_credential",
                subject_id=cred_id,
                payload={"provider": cred_provider},
            )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        try:
            self.secret_store.delete(secret_ref)
        except Exception:  # noqa: BLE001
            logger.warning(
                "failed to remove ciphertext after credential delete",
                extra={"ref": secret_ref, "provider": cred_provider},
            )

    def list(self) -> list[ProviderCredential]:
        return self.db.query(ProviderCredential).order_by(ProviderCredential.created_at.desc()).all()

    def get(self, credential_id: str) -> ProviderCredential:
        return self._get_or_404(credential_id)

    # --- test --------------------------------------------------------------

    async def test_connection(
        self, payload: ProviderTestIn
    ) -> ProviderValidationResult:
        """Validate a stored credential against a known-good cheap test model.

        This endpoint is deliberately isolated from the production policy /
        alias layer: we want to answer "does this key work?", not "is today's
        future-dated production model live?". The model used is:

            1. ``payload.model`` if the operator supplied one,
            2. otherwise the per-provider credential-test model from settings
               (``openai_credential_test_model`` / ``anthropic_credential_test_model``),
            3. otherwise the credential's own ``default_model``.

        The result carries a canonical ``ValidationCategory`` so the UI can
        pick an unambiguous message (auth vs model vs network vs config)
        instead of parsing a free-form error string.
        """
        cred = self._get_or_404(payload.credential_id)
        settings = get_settings()

        requested_model = (
            payload.model
            or credential_test_model_for(cred.provider, settings)
            or cred.default_model
        )

        start = time.time()
        response_preview: str | None = None
        actual_model: str | None = requested_model
        validation: ProviderValidationResult
        try:
            adapter = self._build_adapter_for_test(cred, model=requested_model)
            req = CompletionRequest(prompt=payload.prompt, max_tokens=32)
            result = await adapter.complete(req)
            latency_ms = int((time.time() - start) * 1000)
            actual_model = getattr(adapter, "model", None) or requested_model
            response_preview = (result.text or "")[:160] or None
            validation = build_result(
                ok=True,
                category=ValidationCategory.ok,
                provider=cred.provider,
                requested_model=requested_model,
                actual_model=actual_model,
                http_status=200,
                provider_error_code=None,
                upstream_message=None,
                response_preview=response_preview,
                latency_ms=latency_ms,
            )
        except Exception as exc:  # noqa: BLE001
            latency_ms = int((time.time() - start) * 1000)
            classified = classify_exception(exc)
            validation = build_result(
                ok=False,
                category=classified.category,
                provider=cred.provider,
                requested_model=requested_model,
                actual_model=actual_model,
                http_status=classified.http_status,
                provider_error_code=classified.provider_error_code,
                upstream_message=classified.message,
                response_preview=None,
                latency_ms=latency_ms,
            )

        self.audit.log(
            project_id=None,
            kind=AuditKind.provider_credential_tested,
            message=(
                f"Tested {cred.provider} credential "
                f"({validation.category.value})"
            ),
            subject_kind="provider_credential",
            subject_id=cred.id,
            payload={
                "provider": cred.provider,
                "ok": validation.ok,
                "category": validation.category.value,
                "http_status": validation.http_status,
                "requested_model": validation.requested_model,
                "actual_model": validation.actual_model,
                "latency_ms": validation.latency_ms,
                "execution_mode": validation.execution_mode,
            },
        )
        self.db.commit()
        # Persistent validation history (best-effort; never block the caller).
        persist_validation(
            self.db,
            result=validation,
            credential_id=cred.id,
            source="providers_test",
        )
        return validation

    # --- helpers -----------------------------------------------------------

    def _clear_default_flag(self, exclude_id: str | None = None) -> None:
        q = self.db.query(ProviderCredential).filter(ProviderCredential.is_default.is_(True))
        if exclude_id is not None:
            q = q.filter(ProviderCredential.id != exclude_id)
        for other in q.all():
            other.is_default = False

    def _get_or_404(self, credential_id: str) -> ProviderCredential:
        cred = (
            self.db.query(ProviderCredential)
            .filter(ProviderCredential.id == credential_id)
            .first()
        )
        if cred is None:
            raise LookupError(f"credential not found: {credential_id}")
        return cred

    def _build_adapter_for_test(
        self, cred: ProviderCredential, *, model: str | None = None
    ):
        """Construct a real adapter pinned to the credential-test model.

        ``model`` takes precedence over the credential's stored default. This
        is the hook that makes credential validation independent from the
        production policy / alias layer.
        """
        if cred.provider == ProviderName.mock.value:
            return MockProvider(model=model or cred.default_model or "mock-1")
        api_key = self.secret_store.get(cred.secret_ref)
        if cred.provider == ProviderName.openai.value:
            return OpenAIProvider(
                api_key,
                model=model or cred.default_model or "gpt-4.1-mini",
                base_url=cred.base_url,
            )
        if cred.provider == ProviderName.anthropic.value:
            return AnthropicProvider(
                api_key,
                model=model or cred.default_model or "claude-sonnet-4-6",
                base_url=cred.base_url,
            )
        raise ProviderError(cred.provider, f"unsupported provider: {cred.provider}")
