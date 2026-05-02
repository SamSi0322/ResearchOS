from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import PackageStatus
from app.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from .project import StudentProject


class DeliveryPackage(Base, TimestampMixin):
    __tablename__ = "delivery_packages"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("student_projects.id", ondelete="CASCADE")
    )
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16), default=PackageStatus.draft.value)
    zip_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    manifest_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(128), nullable=True)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    supersedes_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("delivery_packages.id", ondelete="SET NULL"), nullable=True
    )
    included_ids: Mapped[dict] = mapped_column(JSON, default=dict)
    mock: Mapped[bool] = mapped_column(default=False)

    project: Mapped["StudentProject"] = relationship(back_populates="packages")
