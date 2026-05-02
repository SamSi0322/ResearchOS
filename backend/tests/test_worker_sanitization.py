from __future__ import annotations

from pathlib import Path


def test_sanitize_files_rejects_traversal_and_absolute_paths(tmp_path: Path, fresh_db):
    from app.services.code_worker_service import _sanitize_files

    root = tmp_path / "code"
    root.mkdir()
    raw = [
        {"path": "train.py", "content": "print('ok')"},
        {"path": "../escape.py", "content": "evil"},
        {"path": "/etc/passwd", "content": "evil"},
        {"path": "C:/Windows/evil.py", "content": "evil"},
        {"path": "subdir/ok.py", "content": "ok"},
        {"path": "..\\..\\escape.bat", "content": "evil"},
        {"path": "", "content": "evil"},
    ]
    cleaned = _sanitize_files(raw, root)
    paths = {f["path"] for f in cleaned}
    assert paths == {"train.py", "subdir/ok.py"}


def test_scrub_env_drops_secrets():
    from app.workers.job_runner import _scrub_env

    source = {
        "PATH": "/usr/bin",
        "APP_MASTER_KEY": "top-secret",
        "OPENAI_API_KEY": "sk-xxx",
        "ANTHROPIC_API_KEY": "sk-ant-xxx",
        "RESEARCHOS_DB_URL": "sqlite:///x",
        "RESEARCHOS_PORT": "8000",
        "MY_AUTH_TOKEN": "tok",
        "HOME": "/home/x",
    }
    clean = _scrub_env(source)
    assert "PATH" in clean
    assert "HOME" in clean
    for leaked in (
        "APP_MASTER_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "RESEARCHOS_DB_URL",
        "RESEARCHOS_PORT",
        "MY_AUTH_TOKEN",
    ):
        assert leaked not in clean, f"{leaked} leaked into subprocess env"
