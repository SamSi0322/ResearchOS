from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api.routes import make_api_router
from app.config import get_settings
from app.core import models  # noqa: F401 - register ORM models
from app.db.base import Base
from app.db.session import engine
from app.utils import get_logger

logger = get_logger(__name__)
settings = get_settings()


def create_app() -> FastAPI:
    app = FastAPI(
        title="ResearchOS (local)",
        version=__version__,
        description=(
            "Internal operator console + runtime for a research decision and "
            "execution system. Generates candidate ideas, screens them, runs "
            "real experiments, extracts evidence-backed claims, and packages "
            "an auditable artefact. Not a paper generator and not an "
            "auto-submission pipeline — every draft requires human validation. "
            "Runs entirely on localhost. Not a public API."
        ),
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(make_api_router(), prefix="/api")

    # Background reminder loop handle — set on startup, cancelled on shutdown.
    app.state.reminder_task = None

    @app.on_event("startup")
    def _ensure_schema() -> None:
        settings.ensure_dirs()
        Base.metadata.create_all(bind=engine)
        # Apply any pending Alembic migrations. Idempotent — on fresh installs
        # ``create_all`` above already built the latest schema, and the
        # migration file uses inspector guards to skip what's already there.
        # This lets an existing install self-heal to a new schema without
        # requiring an operator to delete var/data/researchos.db.
        try:
            from app.db.migrate import run_upgrade

            run_upgrade()
        except Exception as e:  # noqa: BLE001
            logger.warning("schema migration step skipped: %s", e)

        # Auto-seed provider credentials from env / API_KEYS.txt so the
        # operator does not have to re-click through the Settings modal on
        # every restart. Intentionally best-effort: a bootstrap failure must
        # never take the app down.
        try:
            from app.db.session import SessionLocal
            from app.services.credential_bootstrap_service import run_bootstrap

            with SessionLocal() as db:
                report = run_bootstrap(db)
            logger.info(
                "credential bootstrap report",
                extra={
                    "openai": report.openai,
                    "anthropic": report.anthropic,
                    "sources": report.sources_consulted,
                    "smoke_mode": settings.smoke_mode,
                },
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("credential bootstrap skipped: %s", e)

        logger.info(
            "researchos ready",
            extra={
                "version": __version__,
                "db_url": settings.db_url,
                "smoke_mode": settings.smoke_mode,
            },
        )

        # Start the approval reminder loop if enabled. Disabled by default so
        # tests and first-boot devs do not pay for a background task.
        try:
            from app.services.reminder_scheduler import start_reminder_loop

            app.state.reminder_task = start_reminder_loop()
        except Exception as e:  # noqa: BLE001
            logger.warning("reminder loop not started: %s", e)

    @app.on_event("shutdown")
    async def _stop_reminder_loop() -> None:
        task = getattr(app.state, "reminder_task", None)
        if task is not None:
            task.cancel()
            try:
                await task
            except BaseException:  # noqa: BLE001
                pass
            app.state.reminder_task = None

    @app.get("/")
    def root() -> dict:
        return {
            "service": "researchos",
            "version": __version__,
            "docs": "/docs",
            "api": "/api",
        }

    return app


app = create_app()
