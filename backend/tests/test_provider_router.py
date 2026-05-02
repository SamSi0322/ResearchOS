from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_router_falls_back_to_mock_when_no_credentials(fresh_db):
    from app.core.enums import TaskKind
    from app.db.session import SessionLocal
    from app.providers.router import get_provider_router

    with SessionLocal() as db:
        resolved = get_provider_router(db).resolve(TaskKind.idea_generation)
    assert resolved.provider == "mock"
    assert resolved.mock is True


@pytest.mark.asyncio
async def test_router_picks_default_credential(fresh_db):
    from app.core.enums import TaskKind
    from app.core.schemas import ProviderCredentialIn
    from app.db.session import SessionLocal
    from app.providers.router import get_provider_router
    from app.services import ProviderSecretService

    with SessionLocal() as db:
        ProviderSecretService(db).add(
            ProviderCredentialIn(
                provider="mock", label="m", api_key="mock-key-xyz", is_default=True
            )
        )

    with SessionLocal() as db:
        resolved = get_provider_router(db).resolve(TaskKind.code_generation)
    assert resolved.provider == "mock"
    assert resolved.credential_id is not None


@pytest.mark.asyncio
async def test_router_per_task_default_for_override(fresh_db):
    from app.core.enums import TaskKind
    from app.core.schemas import ProviderCredentialIn
    from app.db.session import SessionLocal
    from app.providers.router import get_provider_router
    from app.services import ProviderSecretService

    with SessionLocal() as db:
        a = ProviderSecretService(db).add(
            ProviderCredentialIn(
                provider="mock", label="generalist", api_key="mock-key-1", is_default=True
            )
        )
        b = ProviderSecretService(db).add(
            ProviderCredentialIn(
                provider="mock",
                label="code-specialist",
                api_key="mock-key-2",
                default_for=[TaskKind.code_generation.value],
            )
        )

    with SessionLocal() as db:
        resolved_idea = get_provider_router(db).resolve(TaskKind.idea_generation)
        resolved_code = get_provider_router(db).resolve(TaskKind.code_generation)
    assert resolved_idea.credential_id == a.id
    assert resolved_code.credential_id == b.id
