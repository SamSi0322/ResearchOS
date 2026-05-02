"""polish patch: runtime cost + validation log + reminder-sent persistence

Revision ID: 0001_polish_patch_schema
Revises:
Create Date: 2026-04-23

This migration brings an existing ResearchOS install up to the polish-patch
schema WITHOUT requiring a DB delete. The local MVP has always relied on
``Base.metadata.create_all`` at startup, which is fine for fresh installs
but cannot alter pre-existing tables. This migration closes that gap.

It is **idempotent** on purpose:
    * fresh installs where ``create_all`` already built the new column/table
      will see the inspector report them present and skip each step;
    * existing installs missing either piece will have it added.

That lets ``alembic upgrade head`` be safe to run on any install, and lets
the FastAPI startup hook call it every boot without fighting ``create_all``.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001_polish_patch_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _bind():
    return op.get_bind()


def _table_exists(name: str) -> bool:
    return sa.inspect(_bind()).has_table(name)


def _column_exists(table: str, column: str) -> bool:
    insp = sa.inspect(_bind())
    if not insp.has_table(table):
        return False
    return column in {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    # 1. experiment_runs.total_estimated_cost -----------------------------
    # Added in the cost-visibility patch. Float, default 0.0, NOT NULL with
    # server-side default so existing rows get a valid value on upgrade.
    if _table_exists("experiment_runs") and not _column_exists(
        "experiment_runs", "total_estimated_cost"
    ):
        with op.batch_alter_table("experiment_runs") as batch:
            batch.add_column(
                sa.Column(
                    "total_estimated_cost",
                    sa.Float(),
                    nullable=False,
                    server_default="0.0",
                )
            )

    # 2. approval_requests.last_reminder_sent_at --------------------------
    # Explicit persistence field for the scheduler guard. Nullable — a fresh
    # approval has never been reminded yet.
    if _table_exists("approval_requests") and not _column_exists(
        "approval_requests", "last_reminder_sent_at"
    ):
        with op.batch_alter_table("approval_requests") as batch:
            batch.add_column(
                sa.Column("last_reminder_sent_at", sa.DateTime(), nullable=True)
            )

    # 3. provider_validation_logs -----------------------------------------
    # Persistent history of /providers/test + /smoke/ping calls.
    if not _table_exists("provider_validation_logs"):
        op.create_table(
            "provider_validation_logs",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column(
                "credential_id",
                sa.String(length=64),
                sa.ForeignKey(
                    "provider_credentials.id", ondelete="SET NULL"
                ),
                nullable=True,
            ),
            sa.Column(
                "source",
                sa.String(length=32),
                nullable=False,
                server_default="providers_test",
            ),
            sa.Column("provider", sa.String(length=32), nullable=False),
            sa.Column("category", sa.String(length=32), nullable=False),
            sa.Column("http_status", sa.Integer(), nullable=True),
            sa.Column(
                "provider_error_code", sa.String(length=128), nullable=True
            ),
            sa.Column("requested_model", sa.String(length=128), nullable=True),
            sa.Column("actual_model", sa.String(length=128), nullable=True),
            sa.Column(
                "latency_ms", sa.Integer(), nullable=False, server_default="0"
            ),
            sa.Column("message", sa.Text(), nullable=False, server_default=""),
            sa.Column(
                "execution_mode",
                sa.String(length=32),
                nullable=False,
                server_default="headless_api",
            ),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.current_timestamp(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.current_timestamp(),
            ),
        )


def downgrade() -> None:
    # Downgrade is best-effort: drop the validation log table first, then
    # the added columns. ``create_all`` can rebuild on fresh install, but
    # we give operators a path off this migration if they need it.
    if _table_exists("provider_validation_logs"):
        op.drop_table("provider_validation_logs")

    if _column_exists("approval_requests", "last_reminder_sent_at"):
        with op.batch_alter_table("approval_requests") as batch:
            batch.drop_column("last_reminder_sent_at")

    if _column_exists("experiment_runs", "total_estimated_cost"):
        with op.batch_alter_table("experiment_runs") as batch:
            batch.drop_column("total_estimated_cost")
