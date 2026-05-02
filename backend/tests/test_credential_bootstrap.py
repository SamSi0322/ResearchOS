from __future__ import annotations

import os
from pathlib import Path

import pytest


def _make_keys_file(tmp_path: Path) -> Path:
    p = tmp_path / "API_KEYS.txt"
    # Synthetic values only; never real keys.
    p.write_text(
        "OPENAI_API_KEY=sk-proj-bootstrap-test-openai-0000\n"
        "ANTHROPIC_API_KEY=sk-ant-api03-bootstrap-test-anthropic-0000\n",
        encoding="utf-8",
    )
    return p


def test_bootstrap_creates_credentials_from_file(fresh_db, tmp_path, monkeypatch):
    from app.config import get_settings, reset_settings_cache
    from app.db.session import SessionLocal
    from app.services.credential_bootstrap_service import run_bootstrap
    from app.core.models import ProviderCredential
    from app.storage.secret_store import reset_secret_store_cache

    keys = _make_keys_file(tmp_path)
    # Make the settings singleton pick up our file + a clean env.
    for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("RESEARCHOS_API_KEYS_FILE", str(keys))
    reset_settings_cache()
    reset_secret_store_cache()
    # Recreate schema in the new-settings DB path.
    from app.db.base import Base
    from app.db.session import engine

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)

    with SessionLocal() as db:
        report = run_bootstrap(db)
    assert report.openai == "file"
    assert report.anthropic == "file"
    assert report.file_path == str(keys)

    with SessionLocal() as db:
        creds = db.query(ProviderCredential).all()
    providers = {c.provider for c in creds}
    assert {"openai", "anthropic"} <= providers


def test_bootstrap_is_idempotent(fresh_db, tmp_path, monkeypatch):
    from app.config import reset_settings_cache
    from app.db.session import SessionLocal
    from app.services.credential_bootstrap_service import run_bootstrap
    from app.core.models import ProviderCredential
    from app.storage.secret_store import reset_secret_store_cache

    keys = _make_keys_file(tmp_path)
    for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("RESEARCHOS_API_KEYS_FILE", str(keys))
    reset_settings_cache()
    reset_secret_store_cache()
    from app.db.base import Base
    from app.db.session import engine

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)

    with SessionLocal() as db:
        run_bootstrap(db)
    with SessionLocal() as db:
        run_bootstrap(db)
    with SessionLocal() as db:
        creds = db.query(ProviderCredential).all()
    # One row per provider, never multiplied.
    providers = [c.provider for c in creds]
    assert providers.count("openai") == 1
    assert providers.count("anthropic") == 1


def test_env_wins_over_file(fresh_db, tmp_path, monkeypatch):
    from app.config import reset_settings_cache
    from app.db.session import SessionLocal
    from app.services.credential_bootstrap_service import run_bootstrap
    from app.core.models import ProviderCredential
    from app.storage import get_secret_store
    from app.storage.secret_store import reset_secret_store_cache

    keys = _make_keys_file(tmp_path)  # file has FILE-specific values
    monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-ENV-WINS-SENTINEL-0000")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("RESEARCHOS_API_KEYS_FILE", str(keys))
    reset_settings_cache()
    reset_secret_store_cache()
    from app.db.base import Base
    from app.db.session import engine

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)

    with SessionLocal() as db:
        report = run_bootstrap(db)
    assert report.openai == "env"
    assert report.anthropic == "file"

    with SessionLocal() as db:
        cred = (
            db.query(ProviderCredential)
            .filter(ProviderCredential.provider == "openai")
            .one()
        )
    # The secret store must hold the ENV value, not the file value.
    assert get_secret_store().get(cred.secret_ref) == "sk-proj-ENV-WINS-SENTINEL-0000"


def test_bootstrap_never_leaks_secret_in_report(fresh_db, tmp_path, monkeypatch):
    """BootstrapReport must never carry a raw key value."""
    from app.config import reset_settings_cache
    from app.db.session import SessionLocal
    from app.services.credential_bootstrap_service import run_bootstrap
    from app.storage.secret_store import reset_secret_store_cache

    keys = _make_keys_file(tmp_path)
    for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("RESEARCHOS_API_KEYS_FILE", str(keys))
    reset_settings_cache()
    reset_secret_store_cache()
    from app.db.base import Base
    from app.db.session import engine

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)

    with SessionLocal() as db:
        report = run_bootstrap(db)
    # The report carries only summary strings; no "sk-" fragments should appear.
    raw = repr(report)
    assert "sk-proj-" not in raw
    assert "sk-ant-" not in raw
