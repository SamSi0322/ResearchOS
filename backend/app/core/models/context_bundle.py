from __future__ import annotations

from sqlalchemy import ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import ContextBundleStatus
from app.db.base import Base, TimestampMixin


class ContextBundle(Base, TimestampMixin):
    """A ZIP of background material uploaded at project intake.

    Streamed to ``var/artifacts/<project>/context/<hash>.zip``, then safely
    extracted under ``var/artifacts/<project>/context/<hash>/`` when
    extraction is enabled. Only non-sensitive metadata is persisted in SQL.

    The ``manifest`` JSON blob records every file that was written during
    extraction (or recorded at upload time for files the extractor skipped),
    along with per-file size + sha256. Services like
    ``IdeaGenerationService`` read ``manifest`` + ``selected_snippets`` to
    ground prompts.
    """

    __tablename__ = "context_bundles"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("student_projects.id", ondelete="CASCADE")
    )
    filename: Mapped[str] = mapped_column(String(512))
    content_hash: Mapped[str] = mapped_column(String(128))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    storage_path: Mapped[str] = mapped_column(String(1024))
    extracted_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    extraction_status: Mapped[str] = mapped_column(
        String(32), default=ContextBundleStatus.pending.value
    )
    extraction_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    manifest_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    manifest: Mapped[list] = mapped_column(JSON, default=list)
    selected_snippets: Mapped[list] = mapped_column(JSON, default=list)
    total_text_chars: Mapped[int] = mapped_column(Integer, default=0)
    text_file_count: Mapped[int] = mapped_column(Integer, default=0)
