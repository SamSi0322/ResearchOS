"""Human-in-the-loop approval gate tests.

Covers:
* HITL OFF → pipeline proceeds normally.
* HITL ON + first batch call → ensure_gate creates a pending ApprovalRequest,
  BatchOrchestrator raises BatchBlockedError with approval_id.
* Approve → resumes.
* Reject / request_changes → stays blocked.
* File-outbox fallback when SMTP not configured.
* Decision emails never contain raw key fragments.
"""

from __future__ import annotations

from pathlib import Path

import pytest


async def _setup(db, *, hitl: bool, approver: str | None = "mentor@example.com"):
    from app.core.schemas import (
        IdeaGenerateIn,
        ProjectCreateIn,
        ProviderCredentialIn,
    )
    from app.services import (
        IdeaGenerationService,
        ProviderSecretService,
        ResearchBriefService,
    )

    ProviderSecretService(db).add(
        ProviderCredentialIn(
            provider="mock", label="t", api_key="mock-hitl", is_default=True
        )
    )
    project = ResearchBriefService(db).create_project(
        ProjectCreateIn(
            title=f"HITL test {hitl}",
            student_name="tester",
            mentor_name="tester",
            research_direction="Test HITL gates.",
            human_in_loop_enabled=hitl,
            primary_approver_email=approver,
            approval_timeout_hours=1,
            reminder_interval_hours=1,
        )
    )
    ideas = await IdeaGenerationService(db).generate(
        project.id, IdeaGenerateIn(count=2)
    )
    return project, [i.id for i in ideas]


@pytest.mark.asyncio
async def test_hitl_off_runs_batch_normally(fresh_db):
    from app.db.session import SessionLocal
    from app.services import BatchOrchestratorService

    with SessionLocal() as db:
        project, idea_ids = await _setup(db, hitl=False)

    with SessionLocal() as db:
        outcomes = await BatchOrchestratorService(db).run_batch(
            project_id=project.id,
            idea_ids=idea_ids,
            worker="claude_code",
        )
    assert all(o.ok for o in outcomes), [
        (o.idea_id, o.run_status, o.result_class, o.error) for o in outcomes
    ]


@pytest.mark.asyncio
async def test_hitl_on_first_batch_pauses_and_writes_outbox(fresh_db, monkeypatch):
    from app.db.session import SessionLocal
    from app.services import BatchOrchestratorService
    from app.services.batch_orchestrator_service import BatchBlockedError

    with SessionLocal() as db:
        project, idea_ids = await _setup(db, hitl=True)

    with SessionLocal() as db:
        with pytest.raises(BatchBlockedError) as exc:
            await BatchOrchestratorService(db).run_batch(
                project_id=project.id,
                idea_ids=idea_ids,
                worker="claude_code",
            )
    err = exc.value
    assert err.status == "paused"
    assert err.stage_key == "post_shortlist"
    assert err.approval_id

    # Outbox must have received the approval REQUEST email (no SMTP
    # configured in tests). Other tests can leave unrelated .eml files in the
    # session-shared outbox - filter by our subject line + gate key.
    from app.config import get_settings

    outbox_dir: Path = get_settings().resolve_path(get_settings().outbox_dir)
    emls = list(outbox_dir.glob("*.eml"))
    bodies = [p.read_text(encoding="utf-8", errors="replace") for p in emls]
    creation_bodies = [
        b for b in bodies if "Approval needed" in b and "post_shortlist" in b
    ]
    assert creation_bodies, "expected an outbox entry for the approval request"
    body = creation_bodies[-1]
    # No provider-key fragments may appear in ANY outbox file.
    for b in bodies:
        assert "sk-proj-" not in b
        assert "sk-ant-" not in b


@pytest.mark.asyncio
async def test_hitl_approve_resumes(fresh_db):
    from app.db.session import SessionLocal
    from app.services import ApprovalService, BatchOrchestratorService
    from app.services.batch_orchestrator_service import BatchBlockedError

    with SessionLocal() as db:
        project, idea_ids = await _setup(db, hitl=True)

    with SessionLocal() as db:
        with pytest.raises(BatchBlockedError) as exc:
            await BatchOrchestratorService(db).run_batch(
                project_id=project.id,
                idea_ids=idea_ids,
                worker="claude_code",
            )
        approval_id = exc.value.approval_id

    with SessionLocal() as db:
        await ApprovalService(db).decide(
            approval_id, decision="approve", note="sanity ok", actor="test"
        )

    with SessionLocal() as db:
        outcomes = await BatchOrchestratorService(db).run_batch(
            project_id=project.id,
            idea_ids=idea_ids,
            worker="claude_code",
        )
    assert outcomes, "batch should proceed after approval"
    assert all(o.ok for o in outcomes), f"unexpected outcomes: {outcomes}"


@pytest.mark.asyncio
async def test_hitl_reject_stays_blocked(fresh_db):
    from app.db.session import SessionLocal
    from app.services import ApprovalService, BatchOrchestratorService
    from app.services.batch_orchestrator_service import BatchBlockedError

    with SessionLocal() as db:
        project, idea_ids = await _setup(db, hitl=True)

    with SessionLocal() as db:
        with pytest.raises(BatchBlockedError) as exc:
            await BatchOrchestratorService(db).run_batch(
                project_id=project.id,
                idea_ids=idea_ids,
                worker="claude_code",
            )
        approval_id = exc.value.approval_id

    with SessionLocal() as db:
        await ApprovalService(db).decide(
            approval_id, decision="reject", note="needs more thought"
        )

    with SessionLocal() as db:
        with pytest.raises(BatchBlockedError) as exc2:
            await BatchOrchestratorService(db).run_batch(
                project_id=project.id,
                idea_ids=idea_ids,
                worker="claude_code",
            )
    assert exc2.value.status == "blocked"


@pytest.mark.asyncio
async def test_hitl_clarification_stays_blocked(fresh_db):
    from app.db.session import SessionLocal
    from app.services import ApprovalService, BatchOrchestratorService
    from app.services.batch_orchestrator_service import BatchBlockedError

    with SessionLocal() as db:
        project, idea_ids = await _setup(db, hitl=True)

    with SessionLocal() as db:
        with pytest.raises(BatchBlockedError) as exc:
            await BatchOrchestratorService(db).run_batch(
                project_id=project.id,
                idea_ids=idea_ids,
                worker="claude_code",
            )
        approval_id = exc.value.approval_id

    with SessionLocal() as db:
        await ApprovalService(db).decide(
            approval_id, decision="request_changes", note="please expand"
        )

    with SessionLocal() as db:
        with pytest.raises(BatchBlockedError) as exc2:
            await BatchOrchestratorService(db).run_batch(
                project_id=project.id,
                idea_ids=idea_ids,
                worker="claude_code",
            )
    assert exc2.value.status == "blocked"


@pytest.mark.asyncio
async def test_approval_email_redacts_known_key_shapes(fresh_db, monkeypatch):
    """If something accidentally forwards a key-looking string into the
    approval context, the EmailService's redactor must scrub it."""
    from app.db.session import SessionLocal
    from app.services import ApprovalService
    from app.services.email_service import get_email_service

    # Seed a project with HITL enabled.
    from app.core.schemas import ProjectCreateIn, ProviderCredentialIn
    from app.services import ProviderSecretService, ResearchBriefService

    with SessionLocal() as db:
        ProviderSecretService(db).add(
            ProviderCredentialIn(
                provider="mock", label="r", api_key="mock-rdct", is_default=True
            )
        )
        project = ResearchBriefService(db).create_project(
            ProjectCreateIn(
                title="Redaction test",
                student_name="x",
                mentor_name="x",
                research_direction="test",
                human_in_loop_enabled=True,
                primary_approver_email="mentor@example.com",
            )
        )

    email = get_email_service()
    res = await email.send(
        to="mentor@example.com",
        subject="test redaction",
        body_text=(
            "Here is a key that should NOT leak: "
            "sk-proj-AAAAAAAAAAAAAAAAAAAAAAAA\n"
            "Bearer tok_ABCDEFGHIJKLMNOPQRSTUVWX"
        ),
    )
    assert res.transport == "outbox"
    body = Path(res.outbox_path).read_text(encoding="utf-8")
    assert "sk-proj-AAAAAAAAAAAAAAAAAAAAAAAA" not in body
    assert "tok_ABCDEFGHIJKLMNOPQRSTUVWX" not in body
    assert "REDACTED" in body
