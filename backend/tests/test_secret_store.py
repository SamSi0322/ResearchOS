from __future__ import annotations

import os

import pytest


def test_secret_store_round_trip(fresh_db):
    from app.storage.secret_store import get_secret_store, reset_secret_store_cache

    reset_secret_store_cache()
    store = get_secret_store()
    meta = store.put(provider="openai", api_key="sk-test-abcdef-secret-value-12345")
    assert meta.ref
    assert "secret" not in meta.masked_preview
    assert "abcdef" not in meta.masked_preview
    assert meta.masked_preview.startswith("sk-")
    assert meta.masked_preview.endswith("2345")
    assert "*" in meta.masked_preview
    plain = store.get(meta.ref)
    assert plain == "sk-test-abcdef-secret-value-12345"
    store.delete(meta.ref)
    assert not store.exists(meta.ref)


def test_secret_store_requires_strong_master_key(tmp_path, monkeypatch):
    """Weak master key should be rejected."""
    from app.storage.secret_store import _derive_key  # noqa: PLC0415

    with pytest.raises(RuntimeError):
        _derive_key("short")


def test_secret_store_persists_across_instances(fresh_db):
    from app.storage.secret_store import get_secret_store, reset_secret_store_cache

    reset_secret_store_cache()
    a = get_secret_store()
    meta = a.put(provider="anthropic", api_key="sk-ant-1234567890abcd")

    reset_secret_store_cache()
    b = get_secret_store()
    plain = b.get(meta.ref)
    assert plain == "sk-ant-1234567890abcd"
