"""Local artifact store.

Artifacts are plain files on disk. The store exposes a single root
(``var/artifacts``) and gives you nested directories per project/run so the
filesystem itself stays organised. For each write we compute a sha256 so the
caller can persist that alongside the ORM record for tamper detection.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.config import get_settings
from app.utils import sha256_bytes, sha256_file


@dataclass
class StoredArtifact:
    path: Path
    size_bytes: int
    sha256: str


class ArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def project_root(self, project_id: str) -> Path:
        p = self.root / project_id
        p.mkdir(parents=True, exist_ok=True)
        return p

    def run_root(self, project_id: str, run_id: str) -> Path:
        p = self.project_root(project_id) / "runs" / run_id
        p.mkdir(parents=True, exist_ok=True)
        return p

    def write_bytes(
        self, project_id: str, relative_path: str | Path, data: bytes
    ) -> StoredArtifact:
        target = self.project_root(project_id) / Path(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return StoredArtifact(path=target, size_bytes=len(data), sha256=sha256_bytes(data))

    def write_text(
        self, project_id: str, relative_path: str | Path, text: str
    ) -> StoredArtifact:
        return self.write_bytes(project_id, relative_path, text.encode("utf-8"))

    def copy_in(
        self, project_id: str, relative_path: str | Path, source: Path
    ) -> StoredArtifact:
        target = self.project_root(project_id) / Path(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(Path(source).read_bytes())
        return StoredArtifact(
            path=target, size_bytes=target.stat().st_size, sha256=sha256_file(target)
        )


_instance: ArtifactStore | None = None


def get_artifact_store() -> ArtifactStore:
    global _instance
    if _instance is None:
        settings = get_settings()
        _instance = ArtifactStore(settings.resolve_path(settings.artifacts_dir))
    return _instance
