from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.enums import AuditKind
from app.core.models import BudgetPolicy, ResearchBrief, StudentProject
from app.core.schemas import ProjectCreateIn, ProjectUpdateIn, ResearchBriefIn
from app.services.audit_service import AuditService
from app.utils import new_id


class ResearchBriefService:
    """Owns StudentProject + ResearchBrief + BudgetPolicy."""

    def __init__(self, db: Session) -> None:
        self.db = db
        self.audit = AuditService(db)

    def create_project(
        self, payload: ProjectCreateIn, *, project_id: str | None = None
    ) -> StudentProject:
        """Create a project + brief + budget policy atomically.

        ``project_id`` is optional. When supplied, that id is used for the
        project row AND the FK on the brief/budget rows — so operator scripts
        that need a stable, readable project id (e.g. ``run_real_pipeline``)
        do not have to rewrite the primary key after the fact, which would
        orphan the related rows.
        """
        pid = project_id or new_id("proj")
        project = StudentProject(
            id=pid,
            title=payload.title,
            student_name=payload.student_name,
            student_ref=payload.student_ref,
            mentor_name=payload.mentor_name,
            advisor_name=payload.advisor_name,
            research_direction=payload.research_direction,
            target_venues=payload.target_venues,
            constraints=payload.constraints,
            exploration_strategy=payload.exploration_strategy,
            provider_profile=payload.provider_profile,
            notes=payload.notes,
            human_in_loop_enabled=bool(payload.human_in_loop_enabled),
            primary_approver_email=(payload.primary_approver_email or None),
            cc_emails=list(payload.cc_emails or []),
            approval_timeout_hours=int(payload.approval_timeout_hours or 72),
            reminder_interval_hours=int(payload.reminder_interval_hours or 24),
            approval_gates=list(payload.approval_gates or []),
        )
        self.db.add(project)
        self.db.flush()

        brief = ResearchBrief(
            id=new_id("brief"),
            project_id=project.id,
            research_direction=payload.research_direction,
            constraints=payload.constraints,
            target_venues=payload.target_venues,
            budget_usd=payload.budget_usd,
            strategy=payload.exploration_strategy,
        )
        self.db.add(brief)

        self.db.add(
            BudgetPolicy(
                id=new_id("bud"),
                project_id=project.id,
                ceiling_usd=payload.budget_usd or 50.0,
            )
        )

        self.audit.log(
            project_id=project.id,
            kind=AuditKind.project_created,
            message=f"Project created: {project.title}",
            subject_kind="project",
            subject_id=project.id,
        )
        self.db.commit()
        return project

    def update_project(self, project_id: str, payload: ProjectUpdateIn) -> StudentProject:
        project = self._get_or_404(project_id)
        for k, v in payload.model_dump(exclude_unset=True).items():
            setattr(project, k, v)
        self.db.flush()
        self.db.commit()
        return project

    def replace_brief(self, project_id: str, payload: ResearchBriefIn) -> ResearchBrief:
        project = self._get_or_404(project_id)
        if project.brief is None:
            brief = ResearchBrief(id=new_id("brief"), project_id=project.id, **payload.model_dump())
            self.db.add(brief)
        else:
            brief = project.brief
            for k, v in payload.model_dump().items():
                setattr(brief, k, v)
        self.db.flush()
        self.db.commit()
        return brief

    def list_projects(self) -> list[StudentProject]:
        return (
            self.db.query(StudentProject).order_by(StudentProject.created_at.desc()).all()
        )

    def get_project(self, project_id: str) -> StudentProject:
        return self._get_or_404(project_id)

    def _get_or_404(self, project_id: str) -> StudentProject:
        project = (
            self.db.query(StudentProject).filter(StudentProject.id == project_id).first()
        )
        if project is None:
            raise LookupError(f"project not found: {project_id}")
        return project
