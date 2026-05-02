"""Polish-patch regression tests.

Covers the five improvements in the final-production pass:

    1. test_alias_metadata_present        — MODEL_ALIAS_METADATA shape + keys
    2. test_alias_can_be_disabled         — RESEARCHOS_DISABLE_ALIAS short-circuits
    3. test_validation_persisted          — /providers/test writes a ProviderValidationLog
    4. test_cost_estimation_added         — CompletionResult.estimated_cost_usd populated
    5. test_reminder_trigger              — scan_pending_approvals sends a reminder

Everything is additive; existing tests must still pass.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

import httpx
import pytest


# ---------------------------------------------------------------------------
# 1. Alias metadata is present and well-formed
# ---------------------------------------------------------------------------


def test_alias_metadata_present():
    from app.config.model_alias import (
        MODEL_ALIAS,
        MODEL_ALIAS_METADATA,
        alias_info,
        alias_status,
    )

    # Every alias in MODEL_ALIAS has matching lifecycle metadata — otherwise
    # manifests would emit aliases without a "why it exists" annotation.
    for requested in MODEL_ALIAS:
        assert requested in MODEL_ALIAS_METADATA, (
            f"MODEL_ALIAS has '{requested}' but MODEL_ALIAS_METADATA is missing it"
        )
    for requested, meta in MODEL_ALIAS_METADATA.items():
        assert "target" in meta and isinstance(meta["target"], str)
        assert "status" in meta and meta["status"] in {
            "temporary",
            "permanent",
            "deprecated",
        }

    # Lookup helpers return the right shape for a remaining alias.
    info = alias_info("claude-opus-4-7")
    assert info["requested"] == "claude-opus-4-7"
    assert info["actual"] == MODEL_ALIAS["claude-opus-4-7"]
    assert info["alias_applied"] is True
    assert info["alias_status"] in {"temporary", "permanent", "deprecated"}
    assert alias_status("claude-opus-4-7") == info["alias_status"]

    # Pass-through (not in MODEL_ALIAS): applied=False, status=None.
    passthrough = alias_info("gpt-4.1-mini")
    assert passthrough["alias_applied"] is False
    assert passthrough["alias_status"] is None


# ---------------------------------------------------------------------------
# 2. Kill switch: RESEARCHOS_DISABLE_ALIAS=true
# ---------------------------------------------------------------------------


def test_alias_can_be_disabled(monkeypatch):
    from app.config.model_alias import (
        MODEL_ALIAS,
        alias_info,
        alias_was_applied,
        resolve_model_alias,
    )

    # Sanity: alias active without the flag.
    assert resolve_model_alias("claude-opus-4-7") == MODEL_ALIAS["claude-opus-4-7"]
    assert alias_was_applied("claude-opus-4-7") is True

    # With the kill switch set, the same call returns the requested id
    # verbatim and alias_was_applied returns False.
    monkeypatch.setenv("RESEARCHOS_DISABLE_ALIAS", "true")
    assert resolve_model_alias("claude-opus-4-7") == "claude-opus-4-7"
    assert alias_was_applied("claude-opus-4-7") is False
    info = alias_info("claude-opus-4-7")
    assert info["alias_applied"] is False
    assert info["alias_disabled"] is True
    assert info["actual"] == "claude-opus-4-7"  # no translation

    # Other truthy spellings must also turn it off.
    for val in ("1", "yes", "on", "TRUE"):
        monkeypatch.setenv("RESEARCHOS_DISABLE_ALIAS", val)
        assert resolve_model_alias("claude-opus-4-7") == "claude-opus-4-7"

    # Falsy / missing values leave the alias layer on.
    for val in ("", "false", "0", "no"):
        monkeypatch.setenv("RESEARCHOS_DISABLE_ALIAS", val)
        assert resolve_model_alias("claude-opus-4-7") == MODEL_ALIAS["claude-opus-4-7"]


# ---------------------------------------------------------------------------
# 3. Validation results persisted
# ---------------------------------------------------------------------------


class _FakeHTTPXResponse:
    def __init__(self, status_code: int, body):
        self.status_code = status_code
        import json as _j

        self._body = body
        self.text = _j.dumps(body) if isinstance(body, dict) else str(body)

    def json(self):
        if isinstance(self._body, dict):
            return self._body
        raise ValueError("not json")


@pytest.mark.asyncio
async def test_validation_persisted(fresh_db, monkeypatch):
    """A /providers/test call must write a ProviderValidationLog row that
    survives across sessions and is reachable via the new history endpoint."""
    from app.core.models import ProviderValidationLog
    from app.core.schemas import ProviderCredentialIn, ProviderTestIn
    from app.core.schemas.provider_validation import ValidationCategory
    from app.db.session import SessionLocal
    from app.services import ProviderSecretService

    async def _post(self, url, headers=None, json=None):
        return _FakeHTTPXResponse(
            200,
            {
                "id": "resp_pvl",
                "output_text": "OK",
                "usage": {"total_tokens": 4, "input_tokens": 3, "output_tokens": 1},
                "status": "completed",
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", _post)

    with SessionLocal() as db:
        svc = ProviderSecretService(db)
        cred = svc.add(
            ProviderCredentialIn(
                provider="openai",
                label="persist-me",
                api_key="sk-good-key-1234567890",
                is_default=True,
            )
        )
        res = await svc.test_connection(ProviderTestIn(credential_id=cred.id))
        assert res.category is ValidationCategory.ok

    # New session: the row must still be there.
    with SessionLocal() as db:
        rows = (
            db.query(ProviderValidationLog)
            .filter(ProviderValidationLog.credential_id == cred.id)
            .all()
        )
        assert len(rows) == 1
        row = rows[0]
        assert row.source == "providers_test"
        assert row.provider == "openai"
        assert row.category == "ok"
        assert row.http_status == 200
        assert row.requested_model == "gpt-4.1-mini"
        # Raw key never in the persisted message.
        assert "sk-good-key-1234567890" not in (row.message or "")
        assert row.execution_mode == "headless_api"


# ---------------------------------------------------------------------------
# 4. Cost estimation added
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cost_estimation_added():
    """CompletionResult carries both the legacy blended estimate and the
    new split input/output estimate in ``estimated_cost_usd``."""
    from app.providers.base import CompletionRequest
    from app.providers.openai_adapter import OpenAIProvider

    class _FakeResp:
        status_code = 200
        text = "{}"

        def json(self):
            return {
                "id": "r",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "x"}],
                    }
                ],
                "usage": {
                    "total_tokens": 2000,
                    "input_tokens": 1500,
                    "output_tokens": 500,
                },
                "status": "completed",
            }

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, headers=None, json=None):
            return _FakeResp()

    adapter = OpenAIProvider(api_key="sk-proj-cost", model="gpt-4.1")
    with patch(
        "app.providers.openai_adapter.httpx.AsyncClient", return_value=_FakeClient()
    ):
        result = await adapter.complete(CompletionRequest(prompt="hi"))

    # Blended legacy: 2000 tokens * $0.0025/1k = $0.005
    assert result.estimated_cost == pytest.approx(0.005)
    # Split: 1500 input * $0.0025 + 500 output * $0.01 = 0.00375 + 0.005 = $0.00875
    assert result.estimated_cost_usd == pytest.approx(0.00875)


def test_cost_table_split_helper():
    from app.config.model_alias import MODEL_COST_TABLE, estimate_split_cost

    # Known model: input 0.0025/1k, output 0.01/1k.
    assert "gpt-4.1" in MODEL_COST_TABLE
    assert estimate_split_cost(1000, 1000, "gpt-4.1") == pytest.approx(0.0025 + 0.01)
    # Zero tokens: zero.
    assert estimate_split_cost(0, 0, "gpt-4.1") == 0.0
    # Unknown model: falls back to blended, never raises.
    val = estimate_split_cost(1000, 1000, "made-up-model")
    assert isinstance(val, float)


def test_experiment_run_has_total_estimated_cost_column():
    """The run row stores the aggregate so the packager can sum them."""
    from app.core.models import ExperimentRun

    col = ExperimentRun.__table__.c.get("total_estimated_cost")
    assert col is not None


# ---------------------------------------------------------------------------
# 5. Reminder scheduler trigger
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reminder_trigger(fresh_db):
    """scan_pending_approvals() sends a reminder when the interval elapsed."""
    from app.core.schemas import ProjectCreateIn, ProviderCredentialIn
    from app.db.session import SessionLocal
    from app.services import ProviderSecretService, ResearchBriefService
    from app.services.approval_service import ApprovalService
    from app.services.reminder_scheduler import scan_pending_approvals

    with SessionLocal() as db:
        ProviderSecretService(db).add(
            ProviderCredentialIn(
                provider="mock", label="r", api_key="mock-rem", is_default=True
            )
        )
        project = ResearchBriefService(db).create_project(
            ProjectCreateIn(
                title="Reminder scheduler test",
                student_name="x",
                mentor_name="x",
                research_direction="test",
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

    # Backdate the approval so the reminder interval has elapsed. Without
    # this the scan is a no-op (correctly — the approval was just created).
    with SessionLocal() as db:
        from app.core.models import ApprovalRequest

        row = (
            db.query(ApprovalRequest)
            .filter(ApprovalRequest.id == approval_id)
            .first()
        )
        assert row is not None
        row.requested_at = datetime.utcnow() - timedelta(hours=5)
        db.commit()

    summary = await scan_pending_approvals()
    assert summary["reminded_count"] >= 1
    assert approval_id in summary["reminded"]

    # A second immediate call is a no-op (idempotent): last_reminder_at was
    # just updated, so the interval has not elapsed again.
    summary2 = await scan_pending_approvals()
    assert summary2["reminded_count"] == 0


def test_reminder_loop_disabled_by_default():
    """The background loop must be off unless explicitly enabled."""
    from app.config import reset_settings_cache

    reset_settings_cache()
    from app.services.reminder_scheduler import start_reminder_loop

    # With no env override the loop returns None.
    task = start_reminder_loop()
    assert task is None


# ---------------------------------------------------------------------------
# Bonus: manifest block wiring (compile-time / smoke-adjacent)
# ---------------------------------------------------------------------------


def test_package_manifest_has_model_resolution_and_cost_summary_keys():
    """Static check that the manifest-builder references both the new
    top-level ``model_resolution`` and ``cost_summary`` keys."""
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "services"
        / "package_service.py"
    ).read_text(encoding="utf-8")
    assert '"model_resolution"' in src
    assert '"cost_summary"' in src
    assert "alias_status" in src


def test_model_resolution_snapshot_carries_alias_status():
    """The _snapshot_model_policy helper must attach alias_status per phase."""
    # Import the helper directly.
    from app.services.package_service import _snapshot_model_policy

    snap = _snapshot_model_policy()
    assert "model_resolution" in snap
    for phase, entry in snap["model_resolution"].items():
        # Every phase must expose the four required keys so manifest readers
        # can rely on the shape.
        assert set(entry.keys()) >= {
            "requested",
            "actual",
            "alias_applied",
            "alias_status",
        }, f"phase {phase} missing keys: {entry}"
