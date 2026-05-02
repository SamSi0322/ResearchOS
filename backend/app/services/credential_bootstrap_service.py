"""Idempotent startup bootstrap of provider credentials.

Lookup precedence (first winner keeps going):

    1. Explicit runtime environment variables (``OPENAI_API_KEY``, ``ANTHROPIC_API_KEY``).
    2. Existing backend secret store (we leave the credential alone).
    3. ``API_KEYS.txt`` in the configured location.

Behaviour:

* If a credential labelled ``bootstrap-<provider>`` already exists AND its
  backing ciphertext is readable, we never touch it. The operator retains
  full control via the Settings / Providers modal.
* If only env-var material is available and no ``bootstrap-<provider>`` row
  exists, we create one.
* If only ``API_KEYS.txt`` material is available and no row exists, we create
  one.
* If env AND file both provide a value and no row exists, env wins.
* If a row exists but the ciphertext is unreadable (e.g. after an
  ``APP_MASTER_KEY`` rotation), we silently *rotate* it with the next
  available source so the system returns to a usable state - the operator
  intent was always "use this key".

Crucially:
* No raw key value is ever logged or returned from any API surface here.
* We only log which sources we consulted and whether they yielded a value.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from sqlalchemy.orm import Session

from app.config import (
    ANTHROPIC_ENV,
    OPENAI_ENV,
    ParsedKeys,
    get_settings,
    resolve_api_keys_file,
)
from app.core.enums import ProviderName, TaskKind
from app.core.models import ProviderCredential
from app.core.schemas import ProviderCredentialIn, ProviderCredentialUpdateIn
from app.services.provider_secret_service import ProviderSecretService
from app.storage import get_secret_store
from app.utils import get_logger

logger = get_logger(__name__)


_BOOTSTRAP_LABEL_FMT = "bootstrap-{provider}"


def _default_for(provider: str) -> list[str]:
    """Reasonable per-task-kind routing for each auto-bootstrapped credential.

    The operator can still override via the Providers modal.
    """
    if provider == ProviderName.openai.value:
        # Put code-oriented + cheap structured tasks on the OpenAI side.
        return [
            TaskKind.code_generation.value,
            TaskKind.code_review.value,
            TaskKind.result_analysis.value,
            TaskKind.structured_screening.value,
        ]
    if provider == ProviderName.anthropic.value:
        # Put long-form generation on the Anthropic side.
        return [
            TaskKind.idea_generation.value,
            TaskKind.spec_generation.value,
            TaskKind.draft_generation.value,
            TaskKind.review.value,
        ]
    return []


@dataclass
class BootstrapReport:
    openai: str = "skipped"  # one of: env, file, existing, unavailable, rotated
    anthropic: str = "skipped"
    sources_consulted: list[str] = field(default_factory=list)
    file_path: str | None = None
    file_keys_found: list[str] = field(default_factory=list)


class CredentialBootstrapService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.settings = get_settings()
        self.store = get_secret_store()
        self.provider_service = ProviderSecretService(db)

    # --- public --------------------------------------------------------

    def run(self) -> BootstrapReport:
        """Run the bootstrap synchronously. Idempotent across restarts."""
        report = BootstrapReport()

        # 1. environment
        import os

        env_openai = os.environ.get(OPENAI_ENV)
        env_anthropic = os.environ.get(ANTHROPIC_ENV)
        if env_openai or env_anthropic:
            report.sources_consulted.append("env")

        # 2. file
        parsed = self._load_api_keys_file()
        if parsed.pairs:
            report.sources_consulted.append(f"file:{parsed.source_path}")
            report.file_path = str(parsed.source_path) if parsed.source_path else None
            report.file_keys_found = sorted(parsed.pairs.keys())

        file_openai = parsed.get(OPENAI_ENV)
        file_anthropic = parsed.get(ANTHROPIC_ENV)

        # 3. materialise for each provider
        report.openai = self._apply(
            provider=ProviderName.openai.value,
            env_value=env_openai,
            file_value=file_openai,
            default_model=self.settings.openai_smoke_model
            if self.settings.smoke_mode
            else None,
        )
        report.anthropic = self._apply(
            provider=ProviderName.anthropic.value,
            env_value=env_anthropic,
            file_value=file_anthropic,
            default_model=self.settings.anthropic_smoke_model
            if self.settings.smoke_mode
            else None,
        )

        logger.info(
            "credential bootstrap complete",
            extra={
                "openai": report.openai,
                "anthropic": report.anthropic,
                "sources": report.sources_consulted,
                "file_keys": report.file_keys_found,
            },
        )
        return report

    # --- internals -----------------------------------------------------

    def _load_api_keys_file(self) -> ParsedKeys:
        override = self.settings.api_keys_file_override
        candidates = list(self.settings.api_keys_file_candidates)
        return resolve_api_keys_file(override=override, candidates=candidates)

    def _apply(
        self,
        *,
        provider: str,
        env_value: str | None,
        file_value: str | None,
        default_model: str | None,
    ) -> str:
        value = env_value or file_value
        label = _BOOTSTRAP_LABEL_FMT.format(provider=provider)

        existing = (
            self.db.query(ProviderCredential)
            .filter(
                ProviderCredential.provider == provider,
                ProviderCredential.label == label,
            )
            .first()
        )
        if existing is not None:
            # If the row is there but its ciphertext is unreadable, attempt a
            # rotation using whatever source we have.
            if not self.store.exists(existing.secret_ref) and value:
                try:
                    self.provider_service.update(
                        existing.id, ProviderCredentialUpdateIn(api_key=value)
                    )
                    return "rotated"
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "bootstrap rotation failed",
                        extra={"provider": provider, "err": str(e)},
                    )
                    return "unavailable"
            return "existing"

        if not value:
            return "unavailable"

        try:
            self.provider_service.add(
                ProviderCredentialIn(
                    provider=provider,
                    label=label,
                    api_key=value,
                    default_model=default_model,
                    default_for=_default_for(provider),
                    is_default=False,
                    notes="Auto-created by CredentialBootstrapService on startup.",
                )
            )
        except Exception as e:  # noqa: BLE001
            # Don't let a bootstrap failure kill the whole app.
            logger.warning(
                "bootstrap create failed",
                extra={"provider": provider, "err": str(e)},
            )
            return "unavailable"

        return "env" if env_value else "file"


def run_bootstrap(db: Session) -> BootstrapReport:
    return CredentialBootstrapService(db).run()
