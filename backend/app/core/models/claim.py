from __future__ import annotations

from sqlalchemy import Boolean, ForeignKey, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class Claim(Base, TimestampMixin):
    """An evidence-backed claim produced by the research pipeline."""

    __tablename__ = "claims"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("student_projects.id", ondelete="CASCADE")
    )
    idea_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("ideas.id", ondelete="SET NULL"), nullable=True
    )
    run_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("experiment_runs.id", ondelete="SET NULL"), nullable=True
    )
    text: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(64), default="quantitative")  # quantitative|qualitative|assumption
    evidence_refs: Mapped[list] = mapped_column(JSON, default=list)  # list of {type, id, note}
    quantitative: Mapped[bool] = mapped_column(Boolean, default=False)
    value: Mapped[str | None] = mapped_column(String(255), nullable=True)
    supersedes_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("claims.id", ondelete="SET NULL"), nullable=True
    )
    mock: Mapped[bool] = mapped_column(Boolean, default=False)


class FigureSpec(Base, TimestampMixin):
    __tablename__ = "figure_specs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("student_projects.id", ondelete="CASCADE")
    )
    title: Mapped[str] = mapped_column(String(255))
    caption: Mapped[str | None] = mapped_column(Text, nullable=True)
    artifact_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("artifacts.id", ondelete="SET NULL"), nullable=True
    )
    evidence_refs: Mapped[list] = mapped_column(JSON, default=list)


class TableSpec(Base, TimestampMixin):
    __tablename__ = "table_specs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("student_projects.id", ondelete="CASCADE")
    )
    title: Mapped[str] = mapped_column(String(255))
    caption: Mapped[str | None] = mapped_column(Text, nullable=True)
    columns: Mapped[list] = mapped_column(JSON, default=list)
    rows: Mapped[list] = mapped_column(JSON, default=list)
    evidence_refs: Mapped[list] = mapped_column(JSON, default=list)
