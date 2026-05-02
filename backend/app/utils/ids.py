from __future__ import annotations

import secrets


def new_id(prefix: str = "") -> str:
    """Short URL-safe id, optionally prefixed with a domain tag."""
    tok = secrets.token_urlsafe(9).replace("-", "_")[:12]
    return f"{prefix}_{tok}" if prefix else tok


def short_id() -> str:
    return secrets.token_urlsafe(6).replace("-", "_")[:8]
