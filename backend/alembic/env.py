"""Alembic environment.

Reads the DB URL from ``app.config.get_settings().db_url`` so we never
duplicate the connection string. Uses the ORM metadata from
``app.core.models`` as the ``target_metadata`` so autogenerate has a full
picture of the schema.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

# Load the Alembic config.
config = context.config
# NOTE: We deliberately do NOT call ``logging.config.fileConfig`` here.
# ResearchOS configures the root logger with a redacting filter (see
# ``app.utils.logger._configure_root``) that must stay installed. fileConfig
# would wipe our handlers + filter and reapply the [logger_root] block from
# alembic.ini, dropping the secret-redaction behaviour other services rely
# on. Alembic's own info-level output still reaches stderr via the root
# logger we already configured.

# Import models lazily so Alembic can load even when app deps are missing.
from app.config import get_settings  # noqa: E402
from app.core import models  # noqa: E402,F401 - register ORM tables
from app.db.base import Base  # noqa: E402

target_metadata = Base.metadata


def _resolved_url() -> str:
    # The .ini file deliberately leaves sqlalchemy.url empty; source the URL
    # from runtime settings so Alembic and the app agree.
    return get_settings().db_url


def run_migrations_offline() -> None:
    """Generate SQL without a live DB connection (``alembic upgrade --sql``)."""
    context.configure(
        url=_resolved_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,  # SQLite-friendly ALTER via batch mode
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations against a live DB connection."""
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = _resolved_url()
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,  # SQLite-friendly ALTER via batch mode
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
