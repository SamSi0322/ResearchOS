from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ClaimOut(BaseModel):
    id: str
    project_id: str
    idea_id: str | None = None
    run_id: str | None = None
    text: str
    kind: str
    evidence_refs: list[dict] = Field(default_factory=list)
    quantitative: bool = False
    value: str | None = None
    mock: bool = False
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class FigureSpecOut(BaseModel):
    id: str
    title: str
    caption: str | None = None
    artifact_id: str | None = None
    evidence_refs: list[dict] = Field(default_factory=list)
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class TableSpecOut(BaseModel):
    id: str
    title: str
    caption: str | None = None
    columns: list[str] = Field(default_factory=list)
    rows: list[list] = Field(default_factory=list)
    evidence_refs: list[dict] = Field(default_factory=list)
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
