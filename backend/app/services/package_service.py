"""ZIP packaging.

Collects: research brief, ideas, funnel decisions, specs, run summaries,
selected artifacts (metrics.json, stdout.log, code files), manuscript drafts,
review summary, mentorship session notes, audit summary. Computes checksums
and writes a manifest.json alongside.

Freeze rules:

* refuse if there are any OPEN P0 or P1 issues.
* refuse if there are OPEN P2 issues unless `allow_with_waived_p2=True`.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.enums import AuditKind, PackageStatus, ReviewSeverity, ReviewState
from app.core.models import (
    Artifact,
    AuditEvent,
    BudgetPolicy,
    Claim,
    DeliveryPackage,
    Draft,
    DraftSection,
    ExperimentRun,
    ExperimentSpec,
    FunnelDecision,
    Idea,
    Manuscript,
    MentorshipSession,
    ResearchBrief,
    ReviewIssue,
    StudentProject,
)
from app.core.enums import GateKey
from app.core.schemas import PackageCreateIn
from app.services.approval_service import ApprovalService, GateDecision
from app.services.audit_service import AuditService
from app.services.budget_service import BudgetService
from app.services.manuscript_quality_service import ManuscriptQualityService
from app.services.pdf_service import PDFBuildRequest, PDFService
from app.services.readiness_service import ReadinessService
from app.services.review_summary_service import ReviewSummaryService
from app.storage import get_artifact_store
from app.utils import build_zip, get_logger, new_id, sha256_file


class PackageBlockedError(Exception):
    """HITL is on and Gate C has not been approved for this project."""

    def __init__(self, *, stage_key: str, reason: str, approval_id: str | None, status: str):
        super().__init__(f"package blocked at {stage_key}: {reason}")
        self.stage_key = stage_key
        self.reason = reason
        self.approval_id = approval_id
        self.status = status

logger = get_logger(__name__)


class PackageService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.audit = AuditService(db)

    def list(self, project_id: str) -> list[DeliveryPackage]:
        return (
            self.db.query(DeliveryPackage)
            .filter(DeliveryPackage.project_id == project_id)
            .order_by(DeliveryPackage.created_at.desc())
            .all()
        )

    def get(self, package_id: str) -> DeliveryPackage:
        pkg = (
            self.db.query(DeliveryPackage).filter(DeliveryPackage.id == package_id).first()
        )
        if pkg is None:
            raise LookupError(f"package not found: {package_id}")
        return pkg

    async def build(
        self,
        project_id: str,
        payload: PackageCreateIn,
        *,
        require_approval: bool = True,
    ) -> DeliveryPackage:
        project = (
            self.db.query(StudentProject).filter(StudentProject.id == project_id).first()
        )
        if project is None:
            raise LookupError(f"project not found: {project_id}")

        self._enforce_freeze_rules(project_id, payload)

        # Gate C: pre-package freeze. Only applies when HITL is on and the
        # project has the ``pre_package_freeze`` gate in its enabled list.
        if require_approval and project.human_in_loop_enabled:
            gate = await ApprovalService(self.db).ensure_gate(
                project_id=project_id,
                stage_key=GateKey.pre_package_freeze.value,
                context_snapshot={
                    "reason": "pre_package_freeze",
                    "include_mock": bool(payload.include_mock),
                    "allow_with_waived_p2": bool(payload.allow_with_waived_p2),
                    "notes": payload.notes,
                },
            )
            if gate.decision is GateDecision.paused:
                raise PackageBlockedError(
                    stage_key=GateKey.pre_package_freeze.value,
                    reason=gate.reason or "awaiting_approval",
                    approval_id=gate.approval.id if gate.approval else None,
                    status="paused",
                )
            if gate.decision is GateDecision.blocked:
                raise PackageBlockedError(
                    stage_key=GateKey.pre_package_freeze.value,
                    reason=gate.reason or "blocked",
                    approval_id=gate.approval.id if gate.approval else None,
                    status="blocked",
                )

        settings = get_settings()
        packages_root = settings.resolve_path(settings.packages_dir) / project_id
        packages_root.mkdir(parents=True, exist_ok=True)
        prior = (
            self.db.query(DeliveryPackage)
            .filter(DeliveryPackage.project_id == project_id)
            .order_by(DeliveryPackage.version.desc())
            .first()
        )
        version = (prior.version + 1) if prior else 1

        # --- Collect data ---
        brief = (
            self.db.query(ResearchBrief)
            .filter(ResearchBrief.project_id == project_id)
            .first()
        )
        ideas = self.db.query(Idea).filter(Idea.project_id == project_id).all()
        funnel_decisions = (
            self.db.query(FunnelDecision)
            .filter(FunnelDecision.idea_id.in_([i.id for i in ideas] or [""]))
            .all()
        )
        specs = (
            self.db.query(ExperimentSpec).filter(ExperimentSpec.project_id == project_id).all()
        )
        runs = (
            self.db.query(ExperimentRun).filter(ExperimentRun.project_id == project_id).all()
        )
        artifacts = self.db.query(Artifact).filter(Artifact.project_id == project_id).all()
        claims = self.db.query(Claim).filter(Claim.project_id == project_id).all()
        manuscripts = (
            self.db.query(Manuscript).filter(Manuscript.project_id == project_id).all()
        )
        drafts = []
        sections = []
        for m in manuscripts:
            for d in (
                self.db.query(Draft)
                .filter(Draft.manuscript_id == m.id)
                .order_by(Draft.version.desc())
                .all()
            ):
                drafts.append(d)
                sections.extend(
                    self.db.query(DraftSection)
                    .filter(DraftSection.draft_id == d.id)
                    .order_by(DraftSection.order_index.asc())
                    .all()
                )
        issues = self.db.query(ReviewIssue).filter(ReviewIssue.project_id == project_id).all()
        sessions = (
            self.db.query(MentorshipSession)
            .filter(MentorshipSession.project_id == project_id)
            .all()
        )
        budget = (
            self.db.query(BudgetPolicy)
            .filter(BudgetPolicy.project_id == project_id)
            .first()
        )
        audit_events = (
            self.db.query(AuditEvent)
            .filter(AuditEvent.project_id == project_id)
            .order_by(AuditEvent.created_at.asc())
            .all()
        )

        has_mock = (
            any(r.mock for r in runs)
            or any(c.mock for c in claims)
            or any(d.mock for d in drafts)
            or any(a.mock for a in artifacts)
        )

        # --- Assemble virtual files for the ZIP ---
        virtual: dict[str, str] = {}

        virtual["README.md"] = self._render_readme(project, version, has_mock)
        virtual["manifest.json"] = ""  # filled in later, placeholder

        virtual["data/project.json"] = _dumps(_project_doc(project, brief, budget))
        virtual["data/ideas.json"] = _dumps(
            [_idea_doc(i) for i in ideas]
        )
        virtual["data/funnel_decisions.json"] = _dumps(
            [_funnel_doc(fd) for fd in funnel_decisions]
        )
        virtual["data/specs.json"] = _dumps([_spec_doc(s) for s in specs])
        virtual["data/runs.json"] = _dumps([_run_doc(r) for r in runs])
        virtual["data/claims.json"] = _dumps([_claim_doc(c) for c in claims])
        virtual["data/review_issues.json"] = _dumps([_issue_doc(i) for i in issues])
        virtual["data/manuscripts.json"] = _dumps([_ms_doc(m) for m in manuscripts])
        virtual["data/drafts.json"] = _dumps(
            [_draft_doc(d, sections) for d in drafts]
        )
        virtual["data/sessions.json"] = _dumps([_session_doc(s) for s in sessions])
        virtual["data/audit.json"] = _dumps([_audit_doc(a) for a in audit_events])

        # Render manuscript as markdown too for human readability.
        latest_draft: Draft | None = None
        manuscript_md_arcname: str | None = None
        manuscript_pdf_arcname: str | None = None
        quality_arcname: str | None = None
        review_summary_arcname: str | None = None
        readiness_arcname: str | None = None

        if drafts:
            latest_draft = _select_package_draft(drafts)
            if _requires_pro_manuscript(drafts=drafts, runs=runs, has_mock=has_mock) and (
                latest_draft is None or not _is_openai_pro_xhigh_draft(latest_draft)
            ):
                raise PackageBlockedError(
                    stage_key="draft_generation",
                    reason=(
                        "final package requires an OpenAI Pro/xhigh manuscript draft; "
                        "regenerate the draft before building the PDF package"
                    ),
                    status="blocked",
                )
            md = self._render_draft_markdown(latest_draft, sections, has_mock=has_mock)
            manuscript_md_arcname = f"manuscript/draft_v{latest_draft.version}.md"
            virtual[manuscript_md_arcname] = md

        # Quality + review + readiness summaries (always written when there is a project).
        quality_report = (
            ManuscriptQualityService(self.db).report_for_draft(latest_draft)
            if latest_draft
            else None
        )
        quality_dict = quality_report.as_dict() if quality_report else None
        if quality_dict is not None:
            quality_arcname = "manuscript/quality.json"
            virtual[quality_arcname] = _dumps(quality_dict)

        review_summary = ReviewSummaryService(self.db).summary(project_id)
        review_summary_arcname = "manuscript/review_summary.json"
        virtual[review_summary_arcname] = _dumps(review_summary)

        readiness = ReadinessService(self.db).summary(project_id)
        readiness_arcname = "manuscript/readiness.json"
        virtual[readiness_arcname] = _dumps(readiness)

        # PDF rendering from the newest draft.
        pdf_real_path: Path | None = None
        pdf_size_bytes = 0
        if latest_draft is not None:
            try:
                pdf_out = (
                    get_settings().resolve_path(get_settings().artifacts_dir)
                    / project_id
                    / "manuscripts"
                    / f"draft_v{latest_draft.version}.pdf"
                )
                PDFService().build(
                    PDFBuildRequest(
                        project=project,
                        manuscript=(
                            [m for m in manuscripts if m.id == latest_draft.manuscript_id]
                            or [manuscripts[0]]
                        )[0],
                        draft=latest_draft,
                        sections=[s for s in sections if s.draft_id == latest_draft.id],
                        claims=claims,
                        run_summaries=[_run_doc(r) for r in runs],
                        smoke_mode=bool(get_settings().smoke_mode),
                        quality_summary=quality_dict,
                    ),
                    pdf_out,
                )
                pdf_real_path = pdf_out
                pdf_size_bytes = pdf_out.stat().st_size
                manuscript_pdf_arcname = f"manuscript/draft_v{latest_draft.version}.pdf"

                # Persist as an artifact row so it is retrievable via the
                # normal artifact listing (and so the frontend can download
                # it without needing the package).
                art = get_artifact_store().copy_in(
                    project_id,
                    f"manuscripts/draft_v{latest_draft.version}.pdf",
                    pdf_out,
                )
                self.db.add(
                    Artifact(
                        id=new_id("art"),
                        project_id=project_id,
                        run_id=None,
                        kind="manuscript_pdf",
                        name=f"draft_v{latest_draft.version}.pdf",
                        path=str(art.path),
                        sha256=art.sha256,
                        size_bytes=art.size_bytes,
                        mock=has_mock,
                        meta={
                            "draft_id": latest_draft.id,
                            "manuscript_id": latest_draft.manuscript_id,
                            "smoke_mode": bool(get_settings().smoke_mode),
                        },
                    )
                )
                self.db.flush()
            except Exception as e:  # noqa: BLE001
                logger.warning("pdf rendering failed: %s", e)
                pdf_real_path = None

        # Collect real files: code + logs + metrics + predictions + artifact blobs.
        real_files: list[tuple[str, str]] = []
        include_mock = payload.include_mock
        for art in artifacts:
            if art.mock and not include_mock:
                continue
            src = Path(art.path)
            if not src.exists():
                continue
            arc = f"artifacts/{art.kind}/{art.name}" if not art.run_id else f"artifacts/runs/{art.run_id}/{art.kind}/{art.name}"
            real_files.append((arc, str(src)))

        # Add the newly rendered PDF as a real file under the manuscript/ prefix.
        if pdf_real_path is not None and manuscript_pdf_arcname:
            real_files.append((manuscript_pdf_arcname, str(pdf_real_path)))

        # --- Write the zip ---
        zip_path = packages_root / f"package_v{version}.zip"

        # Prepare manifest before the write so we can include it.
        file_index = [
            {"path": arc, "sha256": sha256_file(src), "size": Path(src).stat().st_size}
            for arc, src in real_files
        ] + [{"path": arc, "size": len(content.encode("utf-8"))} for arc, content in virtual.items() if arc != "manifest.json"]

        manifest = {
            "package_version": version,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "project": {
                "id": project.id,
                "title": project.title,
                "owner_name": project.student_name,
                "reviewer_name": project.mentor_name,
                "advisor_name": project.advisor_name,
                "target_venues": project.target_venues,
            },
            "smoke_mode": bool(get_settings().smoke_mode),
            "has_mock_artifacts": has_mock,
            "include_mock_artifacts": include_mock,
            "counts": {
                "ideas": len(ideas),
                "specs": len(specs),
                "runs": len(runs),
                "claims": len(claims),
                "drafts": len(drafts),
                "issues": len(issues),
                "sessions": len(sessions),
                "artifacts": len(real_files),
            },
            "manuscript": {
                "markdown_path": manuscript_md_arcname,
                "pdf_path": manuscript_pdf_arcname,
                "quality_report_path": quality_arcname,
                "review_summary_path": review_summary_arcname,
                "readiness_summary_path": readiness_arcname,
                "has_pdf": manuscript_pdf_arcname is not None,
                "pdf_size_bytes": pdf_size_bytes,
                "draft_id": latest_draft.id if latest_draft else None,
                "draft_version": latest_draft.version if latest_draft else None,
                "mock_draft": bool(latest_draft.mock) if latest_draft else False,
            },
            "readiness_tier": readiness.get("tier"),
            "package_decision": readiness.get("package_decision"),
            # Compute each block once; the manifest exposes them both under
            # the consolidated ``runtime_metadata`` key AND at the top level
            # for backward compatibility with existing readers/tests.
            "model_policy": (_policy := _snapshot_model_policy()),
            "model_resolution": _policy.get("model_resolution", {}),
            "cost_summary": (
                _cost := _cost_summary(
                    runs,
                    ledger_summary=BudgetService(self.db).summary(project_id),
                )
            ),
            # Runtime execution-mode contract: every provider call for this
            # package went through the HTTP adapters. No interactive Claude
            # Code / Codex sessions are involved. A future reader of the
            # manifest can assert on this literal.
            "execution_mode": "headless_api",
            # Consolidated view preferred by new consumers. Nests the three
            # runtime-concerned blocks for discoverability while the top-level
            # mirrors above stay as a back-compat shim.
            "runtime_metadata": {
                "model": {
                    "policy": _policy,
                    "resolution": _policy.get("model_resolution", {}),
                },
                "cost": _cost,
                "execution": {"mode": "headless_api"},
            },
            "files": file_index,
        }
        virtual["manifest.json"] = _dumps(manifest)

        build_zip(zip_path, files=real_files, virtual_files=virtual)

        size_bytes = zip_path.stat().st_size
        sha = sha256_file(zip_path)

        if prior is not None:
            prior.status = PackageStatus.superseded.value

        pkg = DeliveryPackage(
            id=new_id("pkg"),
            project_id=project_id,
            version=version,
            status=PackageStatus.frozen.value,
            zip_path=str(zip_path),
            manifest_path=str(zip_path),  # same file holds manifest.json
            sha256=sha,
            size_bytes=size_bytes,
            summary=f"v{version} ({'with' if has_mock else 'without'} mock artifacts)",
            notes=payload.notes,
            supersedes_id=prior.id if prior else None,
            included_ids={
                "runs": [r.id for r in runs],
                "drafts": [d.id for d in drafts],
                "ideas": [i.id for i in ideas],
            },
            mock=has_mock,
        )
        self.db.add(pkg)

        self.audit.log(
            project_id=project_id,
            kind=AuditKind.package_created,
            message=f"Package v{version} built ({size_bytes} bytes)",
            subject_kind="package",
            subject_id=pkg.id,
            payload={"zip_path": str(zip_path), "sha256": sha, "has_mock": has_mock},
        )
        self.db.commit()
        return pkg

    # --- helpers -----------------------------------------------------------

    def _enforce_freeze_rules(self, project_id: str, payload: PackageCreateIn) -> None:
        issues = (
            self.db.query(ReviewIssue)
            .filter(
                ReviewIssue.project_id == project_id,
                ReviewIssue.state == ReviewState.open.value,
            )
            .all()
        )
        p0 = [i for i in issues if i.severity == ReviewSeverity.P0.value]
        p1 = [i for i in issues if i.severity == ReviewSeverity.P1.value]
        p2 = [i for i in issues if i.severity == ReviewSeverity.P2.value]
        if p0:
            raise ValueError(
                f"Cannot freeze package: {len(p0)} open P0 issues. Resolve or waive them first."
            )
        if p1:
            raise ValueError(
                f"Cannot freeze package: {len(p1)} open P1 issues. Resolve or waive them first."
            )
        if p2 and not payload.allow_with_waived_p2:
            raise ValueError(
                f"Cannot freeze package: {len(p2)} open P2 issues. "
                "Waive them, or pass allow_with_waived_p2=True to acknowledge."
            )

    def _render_readme(self, project: StudentProject, version: int, has_mock: bool) -> str:
        mock_note = (
            "\n> **This package contains MOCK-tagged artifacts or runs. Do not use for "
            "external publication without replacing them with real results.**\n"
            if has_mock
            else ""
        )
        return (
            f"# ResearchOS package v{version}\n\n"
            f"Project: **{project.title}**\n"
            f"Owner: {project.student_name}\n"
            f"Reviewer: {project.mentor_name}\n\n"
            f"{mock_note}\n"
            "## Contents\n\n"
            "- `manifest.json` — file index + checksums\n"
            "- `data/*.json` — structured JSON dump of every table touched by this package\n"
            "- `artifacts/...` — copied artifact files from the filesystem\n"
            "- `manuscript/draft_v*.md` — latest manuscript draft as markdown\n"
        )

    def _render_draft_markdown(
        self, draft: Draft, sections: list[DraftSection], *, has_mock: bool
    ) -> str:
        title = draft.manuscript.title if draft.manuscript else f"Draft v{draft.version}"
        lines = [f"# {title}", f"\n_Draft version: v{draft.version}_\n"]
        if has_mock:
            lines.append("\n> This draft contains MOCK evidence. Not for publication.\n")
        for sec in sorted(
            [s for s in sections if s.draft_id == draft.id], key=lambda s: s.order_index
        ):
            lines.append(f"\n## {sec.title}\n\n{sec.content}")
        lines.append(
            "\n## Evidence Traceability\n\n"
            "Structured claim and run mappings are included in `data/claims.json`, "
            "`data/runs.json`, and the appendices of the PDF package. Raw claim ids "
            "are intentionally kept out of the manuscript body.\n"
        )
        return "\n".join(lines) + "\n"


def _dumps(obj) -> str:
    return json.dumps(obj, indent=2, default=str, sort_keys=False)


def _cost_summary(
    runs: list[ExperimentRun], *, ledger_summary: dict | None = None
) -> dict:
    """Aggregate per-run cost estimates into a compact manifest block.

    ``ExperimentRun.total_estimated_cost`` is populated by the code worker
    service when a real provider returns token usage. Mock runs contribute
    zero. When no run has a cost we still emit the block with zeros so a
    reader can trust the key to exist.

    ``ledger_summary`` is the output of ``BudgetService.summary`` — when
    provided, we also embed the ledger-backed totals, including all
    per-provider-call spend, so the manifest reader can audit the full LLM
    cost surface instead of only experiment-run aggregates.
    """
    total = 0.0
    per_run: list[dict] = []
    for r in runs:
        cost = float(getattr(r, "total_estimated_cost", 0.0) or 0.0)
        total += cost
        per_run.append({"run_id": r.id, "estimated_cost_usd": round(cost, 6)})
    out = {
        "total_estimated_cost": round(total, 6),
        "currency": "USD",
        "per_run": per_run,
        # Two strings on purpose: ``note`` is the short technical line readers
        # already rely on; ``explanation`` is the human-facing one the sales
        # / review layer surfaces. Both are always present so manifest
        # consumers can pick either without a None check.
        "note": (
            "Heuristic estimate derived from MODEL_COST_TABLE × token usage. "
            "Not an invoice."
        ),
        "explanation": (
            "Estimated cost based on model usage across provider calls and experiments. Not a bill."
        ),
    }
    if ledger_summary is not None:
        # Source of truth for budget-gate enforcement. This is what the
        # runtime checks against ``BudgetPolicy.ceiling_usd`` before starting
        # any new provider-backed work.
        out["ledger"] = {
            "ceiling_usd": float(ledger_summary.get("ceiling_usd") or 0.0),
            "spent_usd": float(ledger_summary.get("spent_usd") or 0.0),
            "remaining_usd": float(ledger_summary.get("remaining_usd") or 0.0),
            "warn": bool(ledger_summary.get("warn")),
            "entries": int(ledger_summary.get("entries") or 0),
            "by_kind": ledger_summary.get("by_kind") or {},
            "entries_by_kind": ledger_summary.get("entries_by_kind") or {},
            "provider_call_spent_usd": float(
                ledger_summary.get("provider_call_spent_usd") or 0.0
            ),
            "provider_call_entries": int(
                ledger_summary.get("provider_call_entries") or 0
            ),
        }
    return out


def _select_package_draft(drafts: list[Draft]) -> Draft | None:
    ordered = sorted(drafts, key=lambda d: d.version, reverse=True)
    for draft in ordered:
        if _is_openai_pro_xhigh_draft(draft):
            return draft
    return ordered[0] if ordered else None


def _requires_pro_manuscript(
    *, drafts: list[Draft], runs: list[ExperimentRun], has_mock: bool
) -> bool:
    if not drafts or has_mock:
        return False
    if get_settings().smoke_mode:
        return False
    # Real execution packages should never freeze a fallback or non-Pro final
    # manuscript/PDF. Packages without runs are often test/intake bundles.
    return any(not r.mock for r in runs)


def _is_openai_pro_xhigh_draft(draft: Draft) -> bool:
    meta = draft.meta or {}
    requested = str(meta.get("requested_model") or meta.get("model") or "")
    actual = str(meta.get("actual_model") or meta.get("model") or "")
    requested_effort = str(meta.get("requested_reasoning_effort") or "").lower()
    return (
        meta.get("provider") == "openai"
        and not bool(meta.get("mock"))
        and meta.get("provider_fallback") is None
        and not meta.get("fallback_section_keys")
        and "pro" in requested.lower()
        and "pro" in actual.lower()
        and requested_effort == "xhigh"
    )


def _snapshot_model_policy() -> dict:
    """Compact current-policy digest for the manifest + readers.

    Also surfaces the alias layer:
    * ``phases[phase].model`` is the *requested* model (policy id).
    * ``phases[phase].actual_model`` is what the adapter sends on the wire.
    * ``alias_applied`` is true iff any phase had its model aliased.
    * ``model_resolution`` is a compact ``phase -> {requested, actual}``.
    """
    from app.config import active_run_mode, current_policy_snapshot
    from app.config.model_alias import (
        alias_info,
        alias_was_applied,
        resolve_model_alias,
    )

    snap = current_policy_snapshot()
    mode = snap.pop("__run_mode__", {}).get("mode", active_run_mode().value)
    per_phase: dict = {}
    resolution: dict = {}
    any_alias = False
    for phase, meta in snap.items():
        requested = meta.get("model")
        actual = resolve_model_alias(requested) if requested else requested
        info = alias_info(requested) if requested else {}
        applied = alias_was_applied(requested)
        if applied:
            any_alias = True
        per_phase[phase] = {
            "provider": meta.get("provider"),
            "model": requested,
            "actual_model": actual,
            "reasoning_effort": meta.get("reasoning_effort"),
            "thinking_mode": meta.get("thinking_mode"),
        }
        resolution[phase] = {
            "requested": requested,
            "actual": actual,
            "alias_applied": applied,
            "alias_status": info.get("alias_status"),
        }
    return {
        "run_mode": mode,
        "phases": per_phase,
        "alias_applied": any_alias,
        "alias_disabled": bool((alias_info("") or {}).get("alias_disabled")),
        "model_resolution": resolution,
    }


def _project_doc(p: StudentProject, b: ResearchBrief | None, bud: BudgetPolicy | None) -> dict:
    return _scrub_export_value({
        "id": p.id,
        "title": p.title,
        "status": p.status,
        "owner_name": p.student_name,
        "owner_ref": p.student_ref,
        "reviewer_name": p.mentor_name,
        "advisor_name": p.advisor_name,
        "research_direction": p.research_direction,
        "target_venues": p.target_venues,
        "constraints": p.constraints,
        "exploration_strategy": p.exploration_strategy,
        "notes": p.notes,
        "brief": None
        if b is None
        else {
            "research_direction": b.research_direction,
            "constraints": b.constraints,
            "target_venues": b.target_venues,
            "budget_usd": b.budget_usd,
            "strategy": b.strategy,
        },
        "budget_policy": None
        if bud is None
        else {"ceiling_usd": bud.ceiling_usd, "warn_ratio": bud.warn_ratio, "notes": bud.notes},
        "created_at": p.created_at,
        "updated_at": p.updated_at,
    })


def _idea_doc(i: Idea) -> dict:
    return _scrub_export_value({
        "id": i.id,
        "title": i.title,
        "summary": i.summary,
        "hypothesis": i.hypothesis,
        "novelty_claim": i.novelty_claim,
        "target_metric": i.target_metric,
        "cluster_tag": i.cluster_tag,
        "stage": i.stage,
        "decision": i.decision,
        "score": i.score,
        "rationale": i.rationale,
        "meta": i.meta,
        "created_at": i.created_at,
    })


def _funnel_doc(fd: FunnelDecision) -> dict:
    return _scrub_export_value({
        "id": fd.id,
        "idea_id": fd.idea_id,
        "from_stage": fd.from_stage,
        "to_stage": fd.to_stage,
        "decision": fd.decision,
        "reason": fd.reason,
        "decided_by": fd.decided_by,
        "created_at": fd.created_at,
    })


def _spec_doc(s: ExperimentSpec) -> dict:
    return _scrub_export_value({
        "id": s.id,
        "idea_id": s.idea_id,
        "version": s.version,
        "hypothesis": s.hypothesis,
        "problem_framing": s.problem_framing,
        "target_metrics": s.target_metrics,
        "dataset_assumptions": s.dataset_assumptions,
        "baseline": s.baseline,
        "experiment_plan": s.experiment_plan,
        "constraints": s.constraints,
        "success_criteria": s.success_criteria,
        "stop_criteria": s.stop_criteria,
        "budget_estimate_usd": s.budget_estimate_usd,
        "meta": s.meta,
        "created_at": s.created_at,
    })


def _run_doc(r: ExperimentRun) -> dict:
    return _scrub_export_value({
        "id": r.id,
        "spec_id": r.spec_id,
        "idea_id": r.idea_id,
        "status": r.status,
        "result_class": r.result_class,
        "exit_code": r.exit_code,
        "seed": r.seed,
        "code_hash": r.code_hash,
        "provider_routing": r.provider_routing,
        "config": r.config,
        "metrics": r.metrics,
        "mock": r.mock,
        "summary": r.summary,
        "workspace_path": r.workspace_path,
        "started_at": r.started_at,
        "ended_at": r.ended_at,
        "created_at": r.created_at,
    })


def _claim_doc(c: Claim) -> dict:
    return _scrub_export_value({
        "id": c.id,
        "idea_id": c.idea_id,
        "run_id": c.run_id,
        "text": c.text,
        "kind": c.kind,
        "value": c.value,
        "quantitative": c.quantitative,
        "evidence_refs": c.evidence_refs,
        "mock": c.mock,
        "created_at": c.created_at,
    })


def _issue_doc(i: ReviewIssue) -> dict:
    return _scrub_export_value({
        "id": i.id,
        "draft_id": i.draft_id,
        "subject_kind": i.subject_kind,
        "subject_id": i.subject_id,
        "reviewer_class": i.reviewer_class,
        "severity": i.severity,
        "state": i.state,
        "description": i.description,
        "evidence": i.evidence,
        "suggested_remediation": i.suggested_remediation,
        "resolution_note": i.resolution_note,
        "meta": i.meta,
        "created_at": i.created_at,
    })


def _ms_doc(m: Manuscript) -> dict:
    return _scrub_export_value({
        "id": m.id,
        "title": m.title,
        "target_venue": m.target_venue,
        "status": m.status,
        "created_at": m.created_at,
    })


def _draft_doc(d: Draft, sections: list[DraftSection]) -> dict:
    return _scrub_export_value({
        "id": d.id,
        "manuscript_id": d.manuscript_id,
        "version": d.version,
        "status": d.status,
        "claim_ids": d.claim_ids,
        "mock": d.mock,
        "meta": d.meta,
        "notes": d.notes,
        "sections": [
            {
                "key": s.key,
                "title": s.title,
                "content": s.content,
                "claim_refs": s.claim_refs,
                "evidence_refs": s.evidence_refs,
                "order_index": s.order_index,
            }
            for s in sections
            if s.draft_id == d.id
        ],
        "created_at": d.created_at,
    })


def _session_doc(s: MentorshipSession) -> dict:
    return _scrub_export_value({
        "id": s.id,
        "scheduled_at": s.scheduled_at,
        "reviewer_name": s.mentor_name,
        "status": s.status,
        "notes": s.notes,
        "owner_participation_notes": s.student_participation_notes,
        "next_actions": s.next_actions,
        "unresolved_blockers": s.unresolved_blockers,
        "owner_must_understand": s.student_must_understand,
        "created_at": s.created_at,
    })


def _audit_doc(a: AuditEvent) -> dict:
    return _scrub_export_value({
        "id": a.id,
        "kind": a.kind,
        "actor": a.actor,
        "subject_kind": a.subject_kind,
        "subject_id": a.subject_id,
        "message": a.message,
        "payload": a.payload,
        "created_at": a.created_at,
    })


def _scrub_export_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _scrub_export_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub_export_value(item) for item in value]
    if isinstance(value, str):
        return _scrub_export_string(value)
    return value


def _scrub_export_string(value: str) -> str:
    settings = get_settings()
    scrubbed = value.replace("\\", "/")
    roots = [
        (settings.resolve_path(settings.workspaces_dir).as_posix(), "workspaces"),
        (settings.resolve_path(settings.artifacts_dir).as_posix(), "artifact_store"),
        (settings.resolve_path(settings.packages_dir).as_posix(), "packages"),
        (settings.resolve_path(settings.outbox_dir).as_posix(), "outbox"),
        (settings.repo_root.as_posix(), "repo"),
    ]
    for prefix, label in sorted(roots, key=lambda item: len(item[0]), reverse=True):
        scrubbed = scrubbed.replace(prefix + "/", label + "/")
        if scrubbed == prefix:
            scrubbed = label
        scrubbed = scrubbed.replace(prefix, label)
    return scrubbed
