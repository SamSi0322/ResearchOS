"""Regression tests for the hardening pass.

Covers:
- RunStartIn defaults to the two_step worker.
- CodexWorker loads the dedicated reviewer prompt, not the builder prompt.
- Secret store uses a per-install random salt that persists across process
  restarts and is NOT the legacy fixed salt.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest


def test_run_start_default_worker_is_two_step():
    from app.core.schemas import RunStartIn

    # `worker` field default is two_step, and the field still accepts the
    # other two single-worker modes.
    assert RunStartIn(spec_id="spec_x").worker == "two_step"
    assert RunStartIn(spec_id="spec_x", worker="claude_code").worker == "claude_code"
    assert RunStartIn(spec_id="spec_x", worker="codex").worker == "codex"


def test_code_review_prompt_file_exists():
    """The reviewer worker depends on this file existing."""
    from app.services._prompts import load_prompt

    body = load_prompt("code_review.md")
    assert body, "code_review.md must ship alongside the app"
    # Prompt should explicitly frame the model as an independent reviewer,
    # not a second builder.
    lower = body.lower()
    assert "reviewer" in lower
    assert "not the author" in lower or "not a second builder" in lower or "second pass" in lower
    # And enumerate the required review axes.
    for keyword in ("correctness", "reproducibility", "metrics", "tests"):
        assert keyword in lower, f"code_review.md should discuss {keyword}"


@pytest.mark.asyncio
async def test_codex_worker_does_not_load_builder_prompt(fresh_db, monkeypatch):
    """CodexWorker must load code_review.md, not code_generation.md."""
    import app.workers.codex_worker as cw

    seen: list[str] = []

    real_load = cw.load_prompt

    def fake_load(name: str) -> str:
        seen.append(name)
        return real_load(name)

    monkeypatch.setattr(cw, "load_prompt", fake_load)

    # We don't need the adapter to actually make a network call - the mock
    # adapter is fine and gets selected when no real creds exist.
    from app.db.session import SessionLocal
    from app.workers.base import CodeWorkerRequest

    req = CodeWorkerRequest(
        spec_id="spec_x",
        project_id="proj_x",
        idea_id="idea_x",
        hypothesis="h",
        experiment_plan="p",
        target_metrics=["accuracy"],
        baseline="none",
        constraints="",
        dataset_assumptions="",
        success_criteria=[],
        stop_criteria=[],
        variant_name="review_test",
        previous_files=[{"path": "train.py", "content": "print('hi')"}],
    )
    with SessionLocal() as db:
        await cw.CodexWorker(db).run(req)

    assert seen, "CodexWorker never called load_prompt"
    assert "code_review.md" in seen
    assert "code_generation.md" not in seen, (
        "CodexWorker must not reuse the builder prompt"
    )


def test_secret_store_persists_per_install_salt(fresh_db):
    """First boot generates a random salt; later boots reuse it."""
    from app.config import get_settings
    from app.storage.secret_store import (
        _SALT_LEGACY_FALLBACK,
        get_secret_store,
        reset_secret_store_cache,
    )

    reset_secret_store_cache()
    store = get_secret_store()
    salt_path: Path = get_settings().resolve_path(get_settings().secrets_dir) / ".salt"
    assert salt_path.exists(), "expected per-install salt file to be created"
    salt_a = salt_path.read_bytes()
    assert len(salt_a) >= 16
    assert salt_a != _SALT_LEGACY_FALLBACK, "must not use the legacy fixed salt"

    # Store a secret with the current salt.
    meta = store.put(provider="openai", api_key="sk-persist-check-0123456789")
    plain_a = store.get(meta.ref)
    assert plain_a == "sk-persist-check-0123456789"

    # Rebuild the store; the existing salt must be reused verbatim.
    reset_secret_store_cache()
    store2 = get_secret_store()
    salt_b = salt_path.read_bytes()
    assert salt_b == salt_a
    # The rebuilt store must still decrypt the ciphertext written by `store`.
    assert store2.get(meta.ref) == "sk-persist-check-0123456789"


def test_router_auto_picks_single_credential(fresh_db):
    """When exactly one credential exists, the router uses it without a
    default flag (important for zero-config operator onboarding)."""
    from app.core.enums import TaskKind
    from app.core.schemas import ProviderCredentialIn
    from app.db.session import SessionLocal
    from app.providers.router import get_provider_router
    from app.services import ProviderSecretService

    with SessionLocal() as db:
        cred = ProviderSecretService(db).add(
            ProviderCredentialIn(
                provider="mock",
                label="only",
                api_key="mock-only-credential",
                is_default=False,
            )
        )

    with SessionLocal() as db:
        resolved = get_provider_router(db).resolve(TaskKind.code_generation)
    assert resolved.credential_id == cred.id
