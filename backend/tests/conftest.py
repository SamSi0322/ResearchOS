"""Shared pytest fixtures.

We use an isolated on-disk SQLite + temp var directories so tests don't
collide with a running dev server. The in-memory approach doesn't play
nicely with SQLAlchemy's threaded connection pooling here.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture(scope="session", autouse=True)
def _isolated_env(tmp_path_factory: pytest.TempPathFactory):
    root = tmp_path_factory.mktemp("researchos")
    (root / "var").mkdir(exist_ok=True)
    os.environ["APP_MASTER_KEY"] = "test-master-key-please-use-a-real-one-in-prod"
    os.environ["RESEARCHOS_DB_URL"] = f"sqlite:///{(root / 'var' / 'test.db').as_posix()}"
    os.environ["RESEARCHOS_ARTIFACTS_DIR"] = str(root / "var" / "artifacts")
    os.environ["RESEARCHOS_WORKSPACES_DIR"] = str(root / "var" / "workspaces")
    os.environ["RESEARCHOS_PACKAGES_DIR"] = str(root / "var" / "packages")
    os.environ["RESEARCHOS_SECRETS_DIR"] = str(root / "var" / "secrets")
    os.environ["RESEARCHOS_OUTBOX_DIR"] = str(root / "var" / "outbox")
    os.environ["RESEARCHOS_RUN_TIMEOUT"] = "60"
    os.environ["RESEARCHOS_MAX_CONCURRENCY"] = "1"
    os.environ["RESEARCHOS_DEFAULT_PROVIDER"] = "mock"
    # CRITICAL: isolate tests from any real API_KEYS.txt on the host machine.
    # We point the loader at an empty file under our tmp tree so the
    # credential bootstrap cannot accidentally import real user keys, and we
    # strip the common provider env vars.
    safe_keys = root / "var" / "empty_api_keys.txt"
    safe_keys.write_text("", encoding="utf-8")
    os.environ["RESEARCHOS_API_KEYS_FILE"] = str(safe_keys)
    for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        os.environ.pop(k, None)
    yield root


@pytest.fixture()
def fresh_db():
    """Create a fresh schema per test."""
    # Import inside the fixture so env overrides take effect before settings cache.
    from app.config import get_settings, reset_settings_cache
    from app.core import models  # noqa: F401 register models
    from app.db.base import Base
    from app.db.session import engine
    from app.storage.secret_store import reset_secret_store_cache

    reset_settings_cache()
    reset_secret_store_cache()
    get_settings().ensure_dirs()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield engine
