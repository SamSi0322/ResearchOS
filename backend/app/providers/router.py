"""Provider routing layer.

Services never instantiate provider adapters directly. They ask the router for
an adapter given a ``TaskKind``, and the router decides which credential to
use based on:

    1. explicit default_for mapping (per-credential)
    2. the ``is_default`` flag on a credential
    3. the process-level default provider from settings (usually ``mock``)

This keeps the provider selection policy out of the services and makes it
easy to reroute specific task types later (e.g. route code_generation to a
different provider than idea_generation).
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.enums import ProviderName, TaskKind
from app.core.models import ProviderCredential
from app.providers.anthropic_adapter import AnthropicProvider
from app.providers.base import BaseProvider, ProviderError
from app.providers.mock_adapter import MockProvider
from app.providers.openai_adapter import OpenAIProvider
from app.storage import get_secret_store
from app.utils import get_logger

logger = get_logger(__name__)


@dataclass
class ResolvedAdapter:
    adapter: BaseProvider
    credential_id: str | None
    provider: str
    model: str
    mock: bool


class ProviderRouter:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.settings = get_settings()
        self.secret_store = get_secret_store()

    def _pick_credential(self, task_kind: TaskKind) -> ProviderCredential | None:
        creds = self.db.query(ProviderCredential).all()
        if not creds:
            return None
        kind_val = task_kind.value

        # 1. explicit per-task-kind mapping wins.
        for c in creds:
            if isinstance(c.default_for, list) and kind_val in c.default_for:
                logger.info(
                    "router: task_kind default_for match",
                    extra={"task_kind": kind_val, "cred_id": c.id, "provider": c.provider},
                )
                return c

        # 2. an is_default credential wins next. This is the common operator
        #    shortcut - "use this one unless I said otherwise".
        for c in creds:
            if c.is_default:
                logger.info(
                    "router: is_default credential",
                    extra={"task_kind": kind_val, "cred_id": c.id, "provider": c.provider},
                )
                return c

        # 3. if exactly one credential exists, use it without a configured
        #    default - the operator clearly only set up one thing.
        if len(creds) == 1:
            only = creds[0]
            logger.info(
                "router: single credential auto-pick",
                extra={"task_kind": kind_val, "cred_id": only.id, "provider": only.provider},
            )
            return only

        # 4. fall back to the configured process-wide default provider
        #    (RESEARCHOS_DEFAULT_PROVIDER).
        default = self.settings.default_provider
        for c in creds:
            if c.provider == default:
                return c
        return creds[0]

    def resolve(self, task_kind: TaskKind, *, credential_id: str | None = None) -> ResolvedAdapter:
        cred: ProviderCredential | None = None
        if credential_id:
            cred = (
                self.db.query(ProviderCredential)
                .filter(ProviderCredential.id == credential_id)
                .first()
            )
            if cred is None:
                raise ProviderError("router", f"credential not found: {credential_id}")
        else:
            cred = self._pick_credential(task_kind)

        if cred is None:
            logger.info(
                "no provider credentials configured, falling back to mock",
                extra={"task_kind": task_kind.value},
            )
            return ResolvedAdapter(
                adapter=MockProvider(), credential_id=None, provider="mock", model="mock-1", mock=True
            )

        return self._build(cred)

    def resolve_with_policy(
        self, policy, *, credential_id: str | None = None
    ) -> ResolvedAdapter:
        """Pick a credential that matches ``policy.provider``.

        * If ``policy.provider == "mock"`` we short-circuit to the mock
          adapter without consulting the credential table. This is what
          makes mock-mode isolation bulletproof.
        * Otherwise we prefer an explicit ``credential_id`` if provided, then
          a credential with ``provider == policy.provider``, then fall back
          to mock with a clear log entry so the operator sees why.

        Alias resolution (``model_alias.resolve_model_alias``) is applied to
        the adapter's model just before the wire call. The policy object is
        NOT mutated - the caller keeps the requested name, and the
        ``ResolvedAdapter`` / ``CompletionResult`` carries both sides.
        """
        # Mock mode: no network, no credential resolution.
        if policy.provider == "mock":
            return ResolvedAdapter(
                adapter=MockProvider(model=policy.model or "mock-1"),
                credential_id=None,
                provider="mock",
                model=policy.model or "mock-1",
                mock=True,
            )

        if credential_id:
            cred = (
                self.db.query(ProviderCredential)
                .filter(ProviderCredential.id == credential_id)
                .first()
            )
            if cred is None:
                raise ProviderError("router", f"credential not found: {credential_id}")
        else:
            creds = (
                self.db.query(ProviderCredential)
                .filter(ProviderCredential.provider == policy.provider)
                .order_by(ProviderCredential.is_default.desc(), ProviderCredential.created_at.asc())
                .all()
            )
            cred = creds[0] if creds else None

        if cred is None:
            logger.info(
                "no credential for policy provider; falling back to mock",
                extra={
                    "policy_phase": policy.phase,
                    "policy_provider": policy.provider,
                    "policy_model": policy.model,
                },
            )
            return ResolvedAdapter(
                adapter=MockProvider(),
                credential_id=None,
                provider="mock",
                model="mock-1",
                mock=True,
            )

        # Build the adapter but override the model/timeout with the policy's
        # values so code + review + analysis + draft pick the right model
        # even if the stored credential has a different default.
        # Apply the alias layer here: the policy keeps its requested name
        # (e.g. claude-opus-4-7) while the adapter sends the alias target
        # (e.g. claude-3-opus-20240229) to avoid 404s on not-yet-shipped ids.
        from app.config.model_alias import resolve_model_alias

        actual_model = resolve_model_alias(policy.model)
        resolved = self._build(cred)
        adapter = resolved.adapter
        if hasattr(adapter, "model"):
            adapter.model = actual_model
        if getattr(policy, "timeout", None):
            if hasattr(adapter, "_timeout"):
                adapter._timeout = float(policy.timeout)
        return ResolvedAdapter(
            adapter=adapter,
            credential_id=cred.id,
            provider=cred.provider,
            model=actual_model,
            mock=False,
        )

    def _smoke_model_for(self, provider_name: str) -> str | None:
        if not self.settings.smoke_mode:
            return None
        if provider_name == ProviderName.openai.value:
            return self.settings.openai_smoke_model
        if provider_name == ProviderName.anthropic.value:
            return self.settings.anthropic_smoke_model
        return None

    def _smoke_timeout(self) -> float | None:
        if not self.settings.smoke_mode:
            return None
        return float(self.settings.smoke_request_timeout)

    def _build(self, cred: ProviderCredential) -> ResolvedAdapter:
        provider_name = cred.provider
        if provider_name == ProviderName.mock.value:
            return ResolvedAdapter(
                adapter=MockProvider(model=cred.default_model or "mock-1"),
                credential_id=cred.id,
                provider="mock",
                model=cred.default_model or "mock-1",
                mock=True,
            )

        try:
            api_key = self.secret_store.get(cred.secret_ref)
        except KeyError as e:
            raise ProviderError(provider_name, f"missing secret for credential {cred.id}") from e

        # Smoke mode forces the configured cheap model + short timeout. The
        # stored default_model is preserved for real runs outside smoke mode.
        smoke_model = self._smoke_model_for(provider_name)
        smoke_timeout = self._smoke_timeout()

        if provider_name == ProviderName.openai.value:
            model_name = smoke_model or cred.default_model or "gpt-4.1-mini"
            kwargs: dict = {"model": model_name, "base_url": cred.base_url}
            if smoke_timeout is not None:
                kwargs["timeout"] = smoke_timeout
            adapter: BaseProvider = OpenAIProvider(api_key, **kwargs)
            return ResolvedAdapter(
                adapter=adapter,
                credential_id=cred.id,
                provider="openai",
                model=model_name,
                mock=False,
            )
        if provider_name == ProviderName.anthropic.value:
            model_name = smoke_model or cred.default_model or "claude-sonnet-4-6"
            kwargs = {"model": model_name, "base_url": cred.base_url}
            if smoke_timeout is not None:
                kwargs["timeout"] = smoke_timeout
            adapter = AnthropicProvider(api_key, **kwargs)
            return ResolvedAdapter(
                adapter=adapter,
                credential_id=cred.id,
                provider="anthropic",
                model=model_name,
                mock=False,
            )
        raise ProviderError(provider_name, f"unsupported provider: {provider_name}")


def get_provider_router(db: Session) -> ProviderRouter:
    return ProviderRouter(db)
