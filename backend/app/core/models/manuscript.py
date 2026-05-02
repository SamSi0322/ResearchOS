from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import DraftStatus
from app.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from .project import StudentProject


class Manuscript(Base, TimestampMixin):
    __tablename__ = "manuscripts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("student_projects.id", ondelete="CASCADE")
    )
    title: Mapped[str] = mapped_column(String(512))
    target_venue: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default=DraftStatus.drafting.value)

    project: Mapped["StudentProject"] = relationship(back_populates="manuscripts")
    drafts: Mapped[list["Draft"]] = relationship(
        back_populates="manuscript", cascade="all, delete-orphan", order_by="Draft.version"
    )


class Draft(Base, TimestampMixin):
    __tablename__ = "drafts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    manuscript_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("manuscripts.id", ondelete="CASCADE")
    )
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(32), default=DraftStatus.drafting.value)
    claim_ids: Mapped[list] = mapped_column(JSON, default=list)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    mock: Mapped[bool] = mapped_column(default=False)

    manuscript: Mapped["Manuscript"] = relationship(back_populates="drafts")
    sections: Mapped[list["DraftSection"]] = relationship(
        back_populates="draft", cascade="all, delete-orphan", order_by="DraftSection.order_index"
    )


class DraftSection(Base, TimestampMixin):
    __tablename__ = "draft_sections"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    draft_id: Mapped[str] = mapped_column(String(64), ForeignKey("drafts.id", ondelete="CASCADE"))
    key: Mapped[str] = mapped_column(String(64))  # abstract|intro|method|experiments|results|discussion|limitations|conclusion
    title: Mapped[str] = mapped_column(String(255))
    content: Mapped[str] = mapped_column(Text)
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    claim_refs: Mapped[list] = mapped_column(JSON, default=list)
    evidence_refs: Mapped[list] = mapped_column(JSON, default=list)

    draft: Mapped["Draft"] = relationship(back_populates="sections")
