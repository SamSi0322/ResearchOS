"""Workspace manager for experiment runs.

Each run gets its own directory: ``var/workspaces/<project_id>/<run_id>/``.
The workspace holds generated code, inputs, stdout/stderr, metrics.json, and
any run-local artifacts. After the run completes the artifact subsystem copies
the outputs it cares about into ``var/artifacts/<project>/runs/<run_id>/``.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from app.config import get_settings


@dataclass
class Workspace:
    path: Path
    run_id: str
    project_id: str

    @property
    def code_dir(self) -> Path:
        d = self.path / "code"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def logs_dir(self) -> Path:
        d = self.path / "logs"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def outputs_dir(self) -> Path:
        d = self.path / "outputs"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def write_code_file(self, relative: str, content: str) -> Path:
        p = self.code_dir / relative
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return p


class WorkspaceManager:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def create(self, project_id: str, run_id: str) -> Workspace:
        path = self.root / project_id / run_id
        path.mkdir(parents=True, exist_ok=True)
        (path / "code").mkdir(parents=True, exist_ok=True)
        (path / "logs").mkdir(parents=True, exist_ok=True)
        (path / "outputs").mkdir(parents=True, exist_ok=True)
        return Workspace(path=path, run_id=run_id, project_id=project_id)

    def open(self, project_id: str, run_id: str) -> Workspace:
        return Workspace(
            path=self.root / project_id / run_id, run_id=run_id, project_id=project_id
        )

    def cleanup(self, project_id: str, run_id: str) -> None:
        p = self.root / project_id / run_id
        if p.exists():
            shutil.rmtree(p, ignore_errors=True)


_instance: WorkspaceManager | None = None


def get_workspace_manager() -> WorkspaceManager:
    global _instance
    if _instance is None:
        settings = get_settings()
        _instance = WorkspaceManager(settings.resolve_path(settings.workspaces_dir))
    return _instance
