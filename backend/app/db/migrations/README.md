# Migrations

Schema lifecycle on a fresh boot is two steps, run by FastAPI's startup hook
(`app/main.py:_ensure_schema`) in this order:

1. `Base.metadata.create_all` — fresh installs come up with the latest schema
   in one shot, no Alembic dependency on first boot.
2. `app.db.migrate.run_upgrade()` — Alembic upgrade to head. Existing installs
   that pre-date a column or table get caught up here.

Migrations live in `backend/alembic/versions/`. The `alembic.ini` at
`backend/alembic.ini` reads the database URL from
`app.config.get_settings().db_url` so the local SQLite path stays the
single source of truth.

## Writing a new migration

```bash
cd backend
alembic revision -m "add_foo_to_bar"
# edit alembic/versions/<rev>_add_foo_to_bar.py
alembic upgrade head    # apply locally
```

**Make every migration idempotent.** The startup hook calls `run_upgrade()`
on every boot, including fresh installs where `create_all` already built the
target schema. Use `sa.inspect(bind)` to check whether a column / table /
index already exists and skip the operation when it does. The existing
`0001_polish_patch_schema.py` is the reference pattern.

This idempotence is what lets a fresh install AND an existing install on an
older schema both reach the latest state without operator intervention — you
no longer need to delete `var/data/researchos.db` to add a column.
