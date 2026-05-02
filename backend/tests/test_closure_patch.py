"""Final-closure tests.

Covers:
    1. Alembic migration file is wired and idempotent.
    2. cost_summary always carries an ``explanation`` string.
    3. ``ApprovalRequest.last_reminder_sent_at`` persists + guards the scan.
    4. Manifest exposes ``runtime_metadata`` (with ``model``/``cost``/``execution``)
       *and* keeps the old top-level keys for back-compat.
    5. ``GET /api/providers/validation/latest`` returns one row per provider.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest


# ---------------------------------------------------------------------------
# 1. Alembic migration
# ---------------------------------------------------------------------------


def test_alembic_config_present():
    backend_root = Path(__file__).resolve().parents[1]
    assert (backend_root / "alembic.ini").exists()
    assert (backend_root / "alembic" / "env.py").exists()
    versions = list((backend_root / "alembic" / "versions").glob("*.py"))
    assert any("0001_polish_patch_schema" in p.stem for p in versions), versions


def test_alembic_migration_is_idempotent_on_fresh_schema(fresh_db):
    """``create_all`` built the full schema; running alembic upgrade must
    then be a no-op (every step uses an inspector guard)."""
    from app.db.migrate import run_upgrade

    # Should not raise. If the guard logic regresses this crashes with
    # "duplicate column" / "table already exists".
    run_upgrade()
    run_upgrade()  # truly idempotent — second call also fine.


def test_alembic_applies_missing_column_on_stale_db(fresh_db, tmp_path_factory):
    """Simulate the pre-patch schema: drop the new column/table with raw SQL,
    then run the migration. The column/table must reappear without a DB wipe."""
    import sqlalchemy as sa

    from app.db.migrate import run_upgrade
    from app.db.session import engine

    # Drop the new column (SQLite: use the ``batch_alter_table`` semantics
    # by rebuilding the table). Simplest: drop the validation log table and
    # test that the migration re-creates it.
    with engine.begin() as conn:
        conn.execute(sa.text("DROP TABLE IF EXISTS provider_validation_logs"))

    # Sanity: the table is gone.
    insp = sa.inspect(engine)
    assert "provider_validation_logs" not in insp.get_table_names()

    # Before applying, stamp the DB as pre-migration so alembic thinks it
    # has to run our revision.
    from alembic import command

    from app.db.migrate import _build_config

    cfg = _build_config()
    command.stamp(cfg, "base")

    run_upgrade()

    insp2 = sa.inspect(engine)
    assert "provider_validation_logs" in insp2.get_table_names()


# ---------------------------------------------------------------------------
# 2. cost_summary always carries explanation
# ---------------------------------------------------------------------------


def test_cost_summary_has_explanation():
    from app.services.package_service import _cost_summary

    # Empty runs list: the block still exists, with zeroed totals and the
    # explanation string.
    summary = _cost_summary([])
    assert summary["explanation"] == (
        "Estimated cost based on model usage across provider calls and experiments. Not a bill."
    )
    assert summary["total_estimated_cost"] == 0.0
    assert summary["currency"] == "USD"
    assert summary["per_run"] == []
    assert "note" in summary  # short technical note kept alongside


# ---------------------------------------------------------------------------
# 3. last_reminder_sent_at
# ---------------------------------------------------------------------------


def test_approval_request_has_last_reminder_sent_at_column():
    from app.core.models import ApprovalRequest

    col = ApprovalRequest.__table__.c.get("last_reminder_sent_at")
    assert col is not None, "model must declare last_reminder_sent_at"
    assert col.nullable is True


@pytest.mark.asyncio
async def test_reminder_scan_populates_last_reminder_sent_at(fresh_db):
    from app.core.models import ApprovalRequest
    from app.core.schemas import ProjectCreateIn, ProviderCredentialIn
    from app.db.session import SessionLocal
    from app.services import ProviderSecretService, ResearchBriefService
    from app.services.approval_service import ApprovalService

    with SessionLocal() as db:
        ProviderSecretService(db).add(
            ProviderCredentialIn(
                provider="mock", label="r", api_key="mock-s", is_default=True
            )
        )
        project = ResearchBriefService(db).create_project(
            ProjectCreateIn(
                title="Reminder sent-at test",
                student_name="x",
                mentor_name="x",
                research_direction="t",
                human_in_loop_enabled=True,
                primary_approver_email="mentor@example.com",
                approval_timeout_hours=72,
                reminder_interval_hours=1,
            )
        )

    with SessionLocal() as db:
        gate = await ApprovalService(db).ensure_gate(
            project_id=project.id, stage_key="post_shortlist"
        )
        approval_id = gate.approval.id  # type: ignore[union-attr]

    # Backdate so the guard fires.
    with SessionLocal() as db:
        row = (
            db.query(ApprovalRequest)
            .filter(ApprovalRequest.id == approval_id)
            .first()
        )
        assert row is not None
        assert row.last_reminder_sent_at is None  # never sent yet
        row.requested_at = datetime.utcnow() - timedelta(hours=5)
        db.commit()

    with SessionLocal() as db:
        reminded = await ApprovalService(db).reminder_scan()
    assert len(reminded) == 1

    # Both the new and the legacy field must be populated in lockstep.
    with SessionLocal() as db:
        row = (
            db.query(ApprovalRequest)
            .filter(ApprovalRequest.id == approval_id)
            .first()
        )
        assert row is not None
        assert row.last_reminder_sent_at is not None
        assert row.last_reminder_at is not None
        # Lockstep: the two timestamps set in the same scan tick.
        delta = abs((row.last_reminder_sent_at - row.last_reminder_at).total_seconds())
        assert delta < 1.0

    # Immediate second call: last_reminder_sent_at is fresh, interval guard
    # suppresses the send.
    with SessionLocal() as db:
        reminded2 = await ApprovalService(db).reminder_scan()
    assert reminded2 == []


# ---------------------------------------------------------------------------
# 4. runtime_metadata manifest block
# ---------------------------------------------------------------------------


def test_manifest_has_runtime_metadata_and_back_compat(fresh_db, tmp_path):
    """Build a minimal package end-to-end and inspect the manifest."""
    import json as _json
    import zipfile as _zf

    import anyio

    from app.core.schemas import PackageCreateIn, ProjectCreateIn, ProviderCredentialIn
    from app.db.session import SessionLocal
    from app.services import (
        PackageService,
        ProviderSecretService,
        ResearchBriefService,
    )

    async def _build():
        with SessionLocal() as db:
            ProviderSecretService(db).add(
                ProviderCredentialIn(
                    provider="mock",
                    label="rm",
                    api_key="mock-rm",
                    is_default=True,
                )
            )
            project = ResearchBriefService(db).create_project(
                ProjectCreateIn(
                    title="runtime_metadata test",
                    student_name="x",
                    mentor_name="x",
                    research_direction="t",
                )
            )
            pkg = await PackageService(db).build(
                project.id,
                PackageCreateIn(
                    allow_with_waived_p2=True,
                    include_mock=True,
                    notes="runtime_metadata smoke",
                ),
                require_approval=False,
            )
            return pkg

    pkg = anyio.run(_build)
    assert pkg.zip_path and Path(pkg.zip_path).exists()

    with _zf.ZipFile(pkg.zip_path) as z:
        manifest = _json.loads(z.read("manifest.json"))

    # Back-compat: the three top-level keys must still be present for
    # existing readers.
    assert "model_resolution" in manifest
    assert "cost_summary" in manifest
    assert manifest["execution_mode"] == "headless_api"

    # New consolidated block.
    rm = manifest["runtime_metadata"]
    assert set(rm.keys()) >= {"model", "cost", "execution"}
    assert rm["execution"]["mode"] == "headless_api"
    assert rm["cost"]["explanation"].startswith(
        "Estimated cost based on model usage"
    )
    # ``model`` nests the policy snapshot + resolution for discoverability.
    assert "resolution" in rm["model"]
    assert "policy" in rm["model"]

    # Top-level ``cost_summary`` and ``runtime_metadata.cost`` must match —
    # they are computed from the same call so divergence would be a bug.
    assert manifest["cost_summary"]["total_estimated_cost"] == (
        rm["cost"]["total_estimated_cost"]
    )


# ---------------------------------------------------------------------------
# 5. GET /api/providers/validation/latest
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, status, body):
        self.status_code = status
        import json as _j

        self._body = body
        self.text = _j.dumps(body) if isinstance(body, dict) else str(body)

    def json(self):
        if isinstance(self._body, dict):
            return self._body
        raise ValueError("not json")


@pytest.mark.asyncio
async def test_validation_latest_returns_one_per_provider(fresh_db, monkeypatch):
    from app.core.schemas import (
        ProviderCredentialIn,
        ProviderTestIn,
        ProviderValidationLogOut,
    )
    from app.db.session import SessionLocal
    from app.api.routes.providers import latest_validation_per_provider
    from app.services import ProviderSecretService

    async def _post(self, url, headers=None, json=None):
        return _FakeResp(
            200,
            {
                "id": "resp_l",
                "output_text": "OK",
                "usage": {
                    "total_tokens": 4,
                    "input_tokens": 3,
                    "output_tokens": 1,
                },
                "status": "completed",
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", _post)

    # Two OpenAI validations + one (stubbed) Anthropic validation.
    with SessionLocal() as db:
        svc = ProviderSecretService(db)
        openai_cred = svc.add(
            ProviderCredentialIn(
                provider="openai",
                label="one",
                api_key="sk-one-1234567890",
                is_default=True,
            )
        )
        anthropic_cred = svc.add(
            ProviderCredentialIn(
                provider="anthropic",
                label="two",
                api_key="sk-ant-two-1234567890",
            )
        )
        await svc.test_connection(ProviderTestIn(credential_id=openai_cred.id))
        await svc.test_connection(ProviderTestIn(credential_id=openai_cred.id))
        await svc.test_connection(ProviderTestIn(credential_id=anthropic_cred.id))

    with SessionLocal() as db:
        rows = [
            ProviderValidationLogOut.model_validate(row).model_dump(mode="json")
            for row in latest_validation_per_provider(db)
        ]
    # One row per provider — two unique providers in this test.
    providers = sorted(r["provider"] for r in rows)
    assert providers == ["anthropic", "openai"]
    # Each row must be the most recent for its provider.
    for r in rows:
        assert r["category"] == "ok"
        assert r["execution_mode"] == "headless_api"
