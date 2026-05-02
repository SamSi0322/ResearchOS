"""SQLAlchemy session / engine plumbing."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

_settings = get_settings()


def _build_engine():
    url = _settings.resolve_db_url(_settings.db_url)
    connect_args: dict = {}
    if url.startswith("sqlite"):
        # SQLite needs this for multithreaded FastAPI dev server.
        connect_args["check_same_thread"] = False
        # Real runs can hold provider calls for many seconds. Give short-lived
        # write conflicts time to clear instead of failing immediately.
        connect_args["timeout"] = 30.0
    engine = create_engine(
        url,
        future=True,
        connect_args=connect_args,
        pool_pre_ping=True,
    )
    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _set_sqlite_busy_timeout(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA busy_timeout=30000")
            finally:
                cursor.close()

    return engine


engine = _build_engine()
# expire_on_commit=False so instances returned from services remain usable by
# the caller after the service commits (e.g. for audit-logging, returning to
# FastAPI to serialize, or passing between sessions in tests).
SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
    future=True,
)


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Context manager for background / CLI usage."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
