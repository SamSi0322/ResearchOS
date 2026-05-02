from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ContextBundleOut(BaseModel):
    id: str
    project_id: str
    filename: str
    content_hash: str
    size_bytes: int
    extraction_status: str
    extraction_error: str | None = None
    manifest: list[dict[str, Any]] = Field(default_factory=list)
    selected_snippets: list[dict[str, Any]] = Field(default_factory=list)
    total_text_chars: int = 0
    text_file_count: int = 0
    storage_path: str
    extracted_path: str | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ContextBundleSummary(BaseModel):
    id: str
    filename: str
    size_bytes: int
    extraction_status: str
    text_file_count: int
    total_text_chars: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
