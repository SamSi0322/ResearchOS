from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class DraftSectionOut(BaseModel):
    id: str
    key: str
    title: str
    content: str
    order_index: int
    claim_refs: list[str] = Field(default_factory=list)
    evidence_refs: list[dict] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)


class DraftOut(BaseModel):
    id: str
    manuscript_id: str
    version: int
    status: str
    claim_ids: list[str] = Field(default_factory=list)
    meta: dict = Field(default_factory=dict)
    notes: str | None = None
    mock: bool = False
    sections: list[DraftSectionOut] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ManuscriptOut(BaseModel):
    id: str
    project_id: str
    title: str
    target_venue: str | None = None
    status: str
    drafts: list[DraftOut] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class DraftGenerateIn(BaseModel):
    manuscript_title: str | None = None
    target_venue: str | None = None
    include_run_ids: list[str] | None = None
    extra_instructions: str | None = None
