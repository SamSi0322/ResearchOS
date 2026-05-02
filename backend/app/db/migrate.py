"""Programmatic Alembic invocation.

FastAPI's startup hook calls ``run_upgrade()`` after ``Base.metadata.create_all``
so that:

* Fresh installs get the new tables via ``create_all`` (unchanged behaviour).
* Existing installs missing columns/tables from newer migrations get them
  applied automatically, without the operator having to delete the DB.

The migration itself uses inspector-based idempotency, so running both
code paths on the same DB is safe.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

from app.config import get_settings
from app.utils import get_logger

logger = get_logger(__name__)

# backend/alembic.ini lives at the repo's backend root, one level above app/.
_ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


def _build_config() -> Config:
    cfg = Config(str(_ALEMBIC_INI))
    # Script location is relative to alembic.ini's directory by default.
    # Explicitly set the URL so offline/online both work without editing ini.
    cfg.set_main_option("sqlalchemy.url", get_settings().db_url)
    return cfg


def run_upgrade(revision: str = "head") -> None:
    """Apply all pending migrations up to ``revision``. Idempotent.

    Failures are logged but never raise — we do not want a migration glitch
    to prevent the dev server from starting. Fresh installs where
    ``create_all`` already created everything just hit the inspector-guarded
    no-ops in the migration.
    """
    try:
        cfg = _build_config()
        command.upgrade(cfg, revision)
        logger.info("alembic upgrade complete", extra={"revision": revision})
    except Exception as e:  # noqa: BLE001
        logger.warning("alembic upgrade skipped: %s", e)
