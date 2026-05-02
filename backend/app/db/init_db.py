"""Create schema and optionally seed demo data.

Run with:
    python -m app.db.init_db             # just create tables
    python -m app.db.init_db --demo       # create + seed a demo project
    python -m app.db.init_db --reset      # drop + recreate (DESTRUCTIVE)
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta

from app.core import models  # noqa: F401  - registers all models
from app.db.base import Base
from app.db.session import SessionLocal, engine
from app.utils import get_logger, new_id

logger = get_logger(__name__)


def create_all() -> None:
    Base.metadata.create_all(bind=engine)
    logger.info("database schema ensured")


def drop_all() -> None:
    Base.metadata.drop_all(bind=engine)
    logger.info("database schema dropped")


def seed_demo() -> str:
    from app.core.enums import (
        AuditKind,
        IdeaStage,
        ProjectStatus,
        SessionStatus,
    )

    with SessionLocal() as db:
        existing = db.query(models.StudentProject).filter_by(id="demo_project").first()
        if existing:
            logger.info("demo project already present, skipping seed")
            return existing.id

        project = models.StudentProject(
            id="demo_project",
            title="Demo: attention dropout for small transformers",
            status=ProjectStatus.active.value,
            student_name="Demo Student",
            student_ref="S-0001",
            mentor_name="Mentor Ada",
            advisor_name="Advisor Lin",
            research_direction=(
                "Investigate whether structured attention dropout improves "
                "small-transformer calibration on short-context tasks."
            ),
            target_venues=["NeurIPS Workshop", "ICML Workshop"],
            constraints="<=2 GPUs, <=48h compute, no proprietary data",
            exploration_strategy="breadth-first",
            provider_profile="default",
            notes="Seeded demo project for local testing.",
        )
        db.add(project)
        db.flush()

        brief = models.ResearchBrief(
            id=new_id("brief"),
            project_id=project.id,
            research_direction=project.research_direction,
            constraints=project.constraints,
            target_venues=project.target_venues,
            budget_usd=50.0,
            strategy=project.exploration_strategy,
            raw_context="",
        )
        db.add(brief)

        db.add(
            models.BudgetPolicy(
                id=new_id("bud"),
                project_id=project.id,
                ceiling_usd=50.0,
                warn_ratio=0.8,
                notes="demo policy",
            )
        )
        db.add(
            models.MentorshipSession(
                id=new_id("sess"),
                project_id=project.id,
                scheduled_at=datetime.utcnow() + timedelta(days=3),
                mentor_name="Mentor Ada",
                status=SessionStatus.scheduled.value,
                notes="Kickoff session with demo student.",
                next_actions=["Confirm brief", "Generate initial idea set"],
                unresolved_blockers=[],
                student_must_understand=["why we do not claim numbers without runs"],
            )
        )
        db.add(
            models.AuditEvent(
                id=new_id("aud"),
                project_id=project.id,
                kind=AuditKind.project_created.value,
                actor="system",
                subject_kind="project",
                subject_id=project.id,
                message="demo project seeded",
            )
        )

        db.commit()
        logger.info("seeded demo project", extra={"project_id": project.id})
        return project.id


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo", action="store_true", help="Seed demo project")
    parser.add_argument("--reset", action="store_true", help="Drop schema first")
    args = parser.parse_args()

    if args.reset:
        drop_all()
    create_all()
    if args.demo:
        seed_demo()


if __name__ == "__main__":
    main()
