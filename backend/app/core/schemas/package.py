from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class PackageCreateIn(BaseModel):
    manuscript_id: str | None = None
    include_mock: bool = True
    allow_with_waived_p2: bool = False
    notes: str | None = None


class PackageOut(BaseModel):
    id: str
    project_id: str
    version: int
    status: str
    zip_path: str | None = None
    manifest_path: str | None = None
    sha256: str | None = None
    size_bytes: int = 0
    summary: str | None = None
    notes: str | None = None
    supersedes_id: str | None = None
    included_ids: dict = Field(default_factory=dict)
    mock: bool = False
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
