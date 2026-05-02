"""Runtime settings loaded from environment / .env file.

Centralised so that tests and scripts can override cleanly.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path, PureWindowsPath

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


# Repo root == two levels up from this file (backend/app/config -> backend -> root)
REPO_ROOT = Path(__file__).resolve().parents[3]
ENV_FILE = REPO_ROOT / ".env"


class Settings(BaseSettings):
    """All runtime settings.

    Fields are resolved from (in order): process env, .env file, defaults.
    """

    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_master_key: str = Field(
        default="dev-insecure-master-key-change-me-please-123456",
        alias="APP_MASTER_KEY",
        description="Used to derive the symmetric encryption key for the secret store.",
    )
    host: str = Field(default="127.0.0.1", alias="RESEARCHOS_HOST")
    port: int = Field(default=8000, alias="RESEARCHOS_PORT")

    db_url: str = Field(
        default=f"sqlite:///{(REPO_ROOT / 'var' / 'data' / 'researchos.db').as_posix()}",
        alias="RESEARCHOS_DB_URL",
    )

    artifacts_dir: Path = Field(
        default=REPO_ROOT / "var" / "artifacts", alias="RESEARCHOS_ARTIFACTS_DIR"
    )
    workspaces_dir: Path = Field(
        default=REPO_ROOT / "var" / "workspaces", alias="RESEARCHOS_WORKSPACES_DIR"
    )
    packages_dir: Path = Field(
        default=REPO_ROOT / "var" / "packages", alias="RESEARCHOS_PACKAGES_DIR"
    )
    secrets_dir: Path = Field(
        default=REPO_ROOT / "var" / "secrets", alias="RESEARCHOS_SECRETS_DIR"
    )

    max_concurrency: int = Field(default=2, alias="RESEARCHOS_MAX_CONCURRENCY")
    run_timeout: int = Field(default=600, alias="RESEARCHOS_RUN_TIMEOUT")

    default_provider: str = Field(default="mock", alias="RESEARCHOS_DEFAULT_PROVIDER")

    # -------- Model policy run mode --------
    # production: OpenAI-only strong model policy (Pro/xhigh for critical
    #             phases, Pro/low for the code builder)
    # smoke:      cheap smoke models, reasoning/thinking dropped
    # mock:       deterministic mock adapter on every phase
    # Leaving this at "production" while ``smoke_mode`` is true is equivalent
    # to explicitly picking smoke.
    run_mode: str = Field(default="production", alias="RESEARCHOS_RUN_MODE")

    # -------- Smoke / cheap-validation mode --------
    # When enabled, providers use the configured cheap models, prompts are
    # compacted, and generation limits are clamped so a full end-to-end pass
    # costs a handful of cents instead of dollars.
    smoke_mode: bool = Field(default=False, alias="RESEARCHOS_SMOKE_MODE")
    max_ideas_per_run: int = Field(default=2, alias="RESEARCHOS_MAX_IDEAS_PER_RUN")
    max_real_provider_calls_per_stage: int = Field(
        default=4, alias="RESEARCHOS_MAX_REAL_PROVIDER_CALLS_PER_STAGE"
    )
    # 400 tokens was too tight for multi-idea JSON output in practice; 800 is
    # still cheap (~fractions of a cent per call on the default cheap models)
    # but leaves room for two structured idea objects + a compact spec / code
    # reviewer patch list to complete.
    smoke_max_tokens: int = Field(default=800, alias="RESEARCHOS_SMOKE_MAX_TOKENS")
    smoke_prompt_budget_chars: int = Field(
        default=6000, alias="RESEARCHOS_SMOKE_PROMPT_BUDGET_CHARS"
    )
    smoke_request_timeout: float = Field(
        default=30.0, alias="RESEARCHOS_SMOKE_REQUEST_TIMEOUT"
    )
    concurrency_per_batch: int = Field(
        default=2, alias="RESEARCHOS_CONCURRENCY_PER_BATCH"
    )
    openai_smoke_model: str = Field(
        default="gpt-4.1-mini", alias="RESEARCHOS_OPENAI_SMOKE_MODEL"
    )
    anthropic_smoke_model: str = Field(
        default="claude-haiku-4-5-20251001",
        alias="RESEARCHOS_ANTHROPIC_SMOKE_MODEL",
    )

    # -------- Credential-test models --------
    # Used by /api/providers/test to validate a saved credential. Deliberately
    # independent of the production policy model: validating a key should prove
    # the key works, not that today's future-dated production model id is live.
    # These default to small, widely-available models and can be overridden
    # per provider without touching the policy table.
    openai_credential_test_model: str = Field(
        default="gpt-4.1-mini",
        alias="RESEARCHOS_OPENAI_CREDENTIAL_TEST_MODEL",
    )
    anthropic_credential_test_model: str = Field(
        default="claude-sonnet-4-6",
        alias="RESEARCHOS_ANTHROPIC_CREDENTIAL_TEST_MODEL",
    )

    # Extra candidate paths for API_KEYS.txt. The first existing + non-empty
    # match wins. We look in the repo root and its parent by default so the
    # user can keep the file outside the source tree if they want.
    api_keys_file_candidates: list[Path] = Field(
        default_factory=lambda: [
            REPO_ROOT / "API_KEYS.txt",
            REPO_ROOT.parent / "API_KEYS.txt",
        ]
    )
    api_keys_file_override: str | None = Field(
        default=None, alias="RESEARCHOS_API_KEYS_FILE"
    )

    # -------- SMTP (approval emails) --------
    # If smtp_host is unset the EmailService uses a file outbox fallback
    # (``var/outbox/``). Real email is opt-in.
    smtp_host: str | None = Field(default=None, alias="RESEARCHOS_SMTP_HOST")
    smtp_port: int = Field(default=587, alias="RESEARCHOS_SMTP_PORT")
    smtp_user: str | None = Field(default=None, alias="RESEARCHOS_SMTP_USER")
    smtp_password: str | None = Field(default=None, alias="RESEARCHOS_SMTP_PASSWORD")
    smtp_use_tls: bool = Field(default=True, alias="RESEARCHOS_SMTP_USE_TLS")
    smtp_sender: str = Field(
        default="researchos-local@localhost", alias="RESEARCHOS_SMTP_SENDER"
    )
    outbox_dir: Path = Field(
        default=REPO_ROOT / "var" / "outbox", alias="RESEARCHOS_OUTBOX_DIR"
    )
    # How often the background reminder loop runs, in minutes. Zero disables
    # the loop — reminders can still be triggered on demand via
    # ``POST /api/approvals/scan/reminders``. A small non-zero value (e.g. 15)
    # is the intended dev-server default once SMTP is configured.
    reminder_scan_interval_minutes: int = Field(
        default=0, alias="RESEARCHOS_REMINDER_INTERVAL_MINUTES"
    )
    # Operator-visible base URL used when assembling approval links into
    # emails. Defaults to the local frontend. If you move the console behind
    # a reverse proxy, set this to the user-facing URL.
    console_base_url: str = Field(
        default="http://localhost:5173", alias="RESEARCHOS_CONSOLE_BASE_URL"
    )

    # -------- Context bundles --------
    context_bundle_max_bytes: int = Field(
        default=512 * 1024 * 1024,
        alias="RESEARCHOS_CONTEXT_BUNDLE_MAX_BYTES",
    )
    context_bundle_max_extracted_files: int = Field(
        default=50_000,
        alias="RESEARCHOS_CONTEXT_BUNDLE_MAX_EXTRACTED_FILES",
    )
    context_bundle_snippet_char_limit: int = Field(
        default=3000,
        alias="RESEARCHOS_CONTEXT_BUNDLE_SNIPPET_CHAR_LIMIT",
    )

    cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"]
    )

    repo_root: Path = REPO_ROOT

    def resolve_path(self, p: Path) -> Path:
        """Coerce a (possibly relative) path to an absolute one under repo_root."""
        if not isinstance(p, Path):
            p = Path(p)
        if not p.is_absolute():
            p = (self.repo_root / p).resolve()
        return p

    def resolve_db_url(self, url: str | None = None) -> str:
        """Resolve relative SQLite URLs against ``repo_root``.

        ``.env`` commonly uses ``sqlite:///var/data/researchos.db``. SQLAlchemy
        interprets that relative to the process CWD, which breaks clean bootstraps
        from a fresh checkout. We canonicalise those paths under ``repo_root``
        while leaving absolute POSIX / Windows paths and non-SQLite URLs alone.
        """
        raw = str(url or self.db_url or "")
        for prefix in ("sqlite+pysqlite:///", "sqlite:///"):
            if not raw.startswith(prefix):
                continue
            path_text = raw.removeprefix(prefix)
            if not path_text or path_text == ":memory:":
                return raw
            if path_text.startswith("/") or PureWindowsPath(path_text).drive:
                return raw
            return f"{prefix}{self.resolve_path(Path(path_text)).as_posix()}"
        return raw

    def ensure_dirs(self) -> None:
        self.db_url = self.resolve_db_url(self.db_url)
        for p in (
            self.artifacts_dir,
            self.workspaces_dir,
            self.packages_dir,
            self.secrets_dir,
            self.outbox_dir,
        ):
            self.resolve_path(p).mkdir(parents=True, exist_ok=True)
        # SQLite file parent
        if self.db_url.startswith("sqlite:///"):
            db_file = Path(self.db_url.removeprefix("sqlite:///"))
            self.resolve_path(db_file.parent).mkdir(parents=True, exist_ok=True)


@lru_cache()
def get_settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    return s


def reset_settings_cache() -> None:
    """Only for tests."""
    get_settings.cache_clear()
    for k in list(os.environ.keys()):
        if k.startswith("RESEARCHOS_") and k.endswith("_TEST"):
            del os.environ[k]
