"""Headless code worker contract.

A code worker takes an ExperimentSpec-like payload and returns a file tree
(plus structured summary / warnings / optional tests). It does NOT execute
code - that is the experiment runner's job.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class CodeWorkerRequest:
    spec_id: str
    project_id: str
    idea_id: str
    hypothesis: str
    experiment_plan: str
    target_metrics: list[str]
    baseline: str
    constraints: str
    dataset_assumptions: str
    success_criteria: list[str]
    stop_criteria: list[str]
    seed: int = 0
    target_dir: str = "code/"
    variant_name: str = "variant"
    extra_instructions: str | None = None
    dependency_constraints: list[str] = field(default_factory=list)
    previous_files: list[dict[str, str]] | None = None  # for review/fix stage
    provider_credential_id: str | None = None


@dataclass
class CodeWorkerResult:
    files: list[dict[str, str]]  # [{path, content}]
    summary: str
    warnings: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    patches: list[dict[str, Any]] = field(default_factory=list)
    provider: str = "mock"
    model: str = ""
    mock: bool = False
    latency_ms: int = 0
    used_fallback: bool = False
    # Lightweight USD estimate for the provider call this worker made. The
    # runner sums these into ``ExperimentRun.total_estimated_cost``.
    estimated_cost_usd: float = 0.0
    diagnostics: dict[str, Any] = field(default_factory=dict)


class BaseCodeWorker:
    name: str = "base"

    async def run(self, req: CodeWorkerRequest) -> CodeWorkerResult:  # pragma: no cover
        raise NotImplementedError
