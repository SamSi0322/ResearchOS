"""Orchestrates the code worker subsystem.

Given a spec, this service runs the builder (default: ClaudeCodeWorker), then
optionally runs a reviewer pass (CodexWorker) on the produced files, then
materialises the resulting file tree into the run's workspace and registers
artifacts. It does NOT execute the code - that is the runner's job.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import PurePosixPath

from sqlalchemy.orm import Session

from app.core.enums import AuditKind, CodeWorkerKind
from app.core.models import Artifact, ExperimentSpec, Idea
from app.services.audit_service import AuditService
from app.storage import get_artifact_store, get_workspace_manager
from app.utils import get_logger, new_id, sha256_bytes
from app.workers import ClaudeCodeWorker, CodeWorkerRequest, CodexWorker

logger = get_logger(__name__)

_MODULE_TO_PACKAGE = {
    "PIL": "Pillow",
    "bs4": "beautifulsoup4",
    "cv2": "opencv-python",
    "sklearn": "scikit-learn",
    "yaml": "PyYAML",
}
_REQ_LINE_RE = re.compile(
    r"^[A-Za-z0-9_.-]+(?:\[[A-Za-z0-9_,.-]+\])?(?:\s*(?:==|>=|<=|~=|!=|>|<)\s*[A-Za-z0-9*_.+-]+)?$"
)
_MIN_COMPAT_REQUIREMENTS = {
    "numpy": {(3, 13): "2.1"},
}


class CodeWorkerService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.audit = AuditService(db)

    def _build_request(self, spec: ExperimentSpec, idea: Idea, *, seed: int, extra: str | None, cred_id: str | None) -> CodeWorkerRequest:
        return CodeWorkerRequest(
            spec_id=spec.id,
            project_id=spec.project_id,
            idea_id=idea.id,
            hypothesis=spec.hypothesis,
            experiment_plan=spec.experiment_plan,
            target_metrics=list(spec.target_metrics or []),
            baseline=spec.baseline,
            constraints=spec.constraints,
            dataset_assumptions=spec.dataset_assumptions,
            success_criteria=list(spec.success_criteria or []),
            stop_criteria=list(spec.stop_criteria or []),
            seed=seed,
            variant_name=f"{idea.title[:32].replace(' ', '_')}_v{spec.version}",
            extra_instructions=extra,
            dependency_constraints=[],
            provider_credential_id=cred_id,
        )

    async def generate_code(
        self,
        *,
        spec: ExperimentSpec,
        idea: Idea,
        run_id: str,
        worker: str,
        seed: int,
        extra_instructions: str | None,
        provider_credential_id: str | None,
    ) -> dict:
        wsm = get_workspace_manager()
        ws = wsm.create(spec.project_id, run_id)

        cw_req = self._build_request(
            spec, idea, seed=seed, extra=extra_instructions, cred_id=provider_credential_id
        )

        primary = ClaudeCodeWorker(self.db)
        result = await primary.run(cw_req)
        if not result.files and not result.mock:
            diagnostic_reason = _builder_failure_reason(result.diagnostics)
            raise RuntimeError(f"builder returned no files ({diagnostic_reason})")

        estimated_cost_usd = float(getattr(result, "estimated_cost_usd", 0.0) or 0.0)
        if worker == "two_step":
            review_req = CodeWorkerRequest(
                **{**cw_req.__dict__, "previous_files": result.files}
            )
            reviewer = CodexWorker(self.db)
            review_result = await reviewer.run(review_req)
            merged_files = {f["path"]: f for f in result.files}
            for patch in review_result.patches:
                merged_files[patch["path"]] = patch
            for f in review_result.files:
                merged_files.setdefault(f["path"], f)
            combined_summary = (
                f"BUILDER: {result.summary}\nREVIEWER: {review_result.summary}"
            )
            warnings = list(result.warnings) + list(review_result.warnings)
            assumptions = list(result.assumptions) + list(review_result.assumptions)
            final_files = list(merged_files.values())
            provider = f"{result.provider}+{review_result.provider}"
            model = f"{result.model}+{review_result.model}"
            mock = result.mock or review_result.mock
            estimated_cost_usd += float(
                getattr(review_result, "estimated_cost_usd", 0.0) or 0.0
            )
        elif worker == CodeWorkerKind.codex.value:
            reviewer = CodexWorker(self.db)
            review_only = await reviewer.run(cw_req)
            final_files = review_only.files or result.files
            combined_summary = review_only.summary
            warnings = list(review_only.warnings)
            assumptions = list(review_only.assumptions)
            provider = review_only.provider
            model = review_only.model
            mock = review_only.mock
            # Reviewer-only: the builder pass above did run for its file tree,
            # but its cost stays captured in estimated_cost_usd; add reviewer on top.
            estimated_cost_usd += float(
                getattr(review_only, "estimated_cost_usd", 0.0) or 0.0
            )
        else:
            final_files = result.files
            combined_summary = result.summary
            warnings = list(result.warnings)
            assumptions = list(result.assumptions)
            provider = result.provider
            model = result.model
            mock = result.mock

        if not any((f.get("path") or "").endswith(".py") for f in final_files):
            raise RuntimeError("no runnable Python files were produced")

        # Write files into the workspace.  Worker-supplied paths are
        # untrusted, so we validate every one before touching the filesystem.
        safe_files = _sanitize_files(final_files, ws.code_dir)
        dependencies = _infer_runtime_dependencies(safe_files)
        safe_files = _ensure_runtime_support_files(
            safe_files,
            dependencies=dependencies,
            variant_name=cw_req.variant_name,
        )
        written_paths: list[str] = []
        for f in safe_files:
            ws.write_code_file(f["path"], f["content"])
            written_paths.append(f["path"])

        # Record artifacts for the generated code.
        artifact_store = get_artifact_store()
        code_hash_parts: list[bytes] = []
        for f in safe_files:
            data = f["content"].encode("utf-8")
            code_hash_parts.append(data)
            stored = artifact_store.copy_in(
                spec.project_id,
                f"runs/{run_id}/code/{f['path']}",
                ws.code_dir / f["path"],
            )
            self.db.add(
                Artifact(
                    id=new_id("art"),
                    project_id=spec.project_id,
                    run_id=run_id,
                    kind="code",
                    name=f["path"],
                    path=str(stored.path),
                    sha256=stored.sha256,
                    size_bytes=stored.size_bytes,
                    mock=mock,
                    meta={"worker": worker, "provider": provider, "model": model},
                )
            )
        code_hash = sha256_bytes(b"\n".join(code_hash_parts))

        # Store a summary file as an artifact too.
        summary_blob = {
            "worker": worker,
            "provider": provider,
            "model": model,
            "summary": combined_summary,
            "warnings": warnings,
            "assumptions": assumptions,
            "files": written_paths,
            "dependencies": dependencies,
            "dependency_install_required": bool(dependencies),
            "builder_used_fallback": bool(result.used_fallback),
            "builder_diagnostics": dict(result.diagnostics or {}),
            "mock": mock,
        }
        stored = artifact_store.write_text(
            spec.project_id,
            f"runs/{run_id}/code_summary.json",
            _dumps(summary_blob),
        )
        self.db.add(
            Artifact(
                id=new_id("art"),
                project_id=spec.project_id,
                run_id=run_id,
                kind="code_summary",
                name="code_summary.json",
                path=str(stored.path),
                sha256=stored.sha256,
                size_bytes=stored.size_bytes,
                mock=mock,
                meta={"worker": worker, "provider": provider, "model": model},
            )
        )

        self.audit.log(
            project_id=spec.project_id,
            kind=AuditKind.code_generated,
            message=f"Code worker {worker} produced {len(final_files)} files",
            subject_kind="run",
            subject_id=run_id,
            payload={
                "files": written_paths,
                "worker": worker,
                "provider": provider,
                "model": model,
                "mock": mock,
            },
        )
        self.db.commit()
        return {
            "workspace_path": str(ws.path),
            "code_hash": code_hash,
            "provider": provider,
            "model": model,
            "mock": mock,
            "worker": worker,
            "files": written_paths,
            "summary": combined_summary,
            "warnings": warnings,
            "assumptions": assumptions,
            "dependencies": dependencies,
            "used_fallback": bool(result.used_fallback),
            "estimated_cost_usd": round(estimated_cost_usd, 6),
            "diagnostics": dict(result.diagnostics or {}),
        }


def _dumps(obj) -> str:
    import json

    return json.dumps(obj, indent=2, default=str)


def _builder_failure_reason(diagnostics: dict | None) -> str:
    if not diagnostics:
        return "empty_response"
    if diagnostics.get("reasoning_only_empty"):
        return "reasoning_only_empty_response"
    raw = diagnostics.get("raw") or {}
    if raw.get("status"):
        return str(raw["status"])
    return "empty_response"


def _sanitize_files(files: list[dict[str, str]], code_root) -> list[dict[str, str]]:
    """Drop any worker-supplied file whose target path escapes the workspace.

    * No absolute paths.
    * No NT drive letters (``c:\\x``).
    * No ``..`` segments.
    * Resolved path must stay under the workspace code root.

    The workers are not allowed to write outside the per-run code directory;
    if they try, we skip the file with a structured log entry rather than
    silently allowing it.
    """
    from pathlib import PurePosixPath
    from app.utils import get_logger

    log = get_logger(__name__)
    root = code_root.resolve()
    clean: list[dict[str, str]] = []
    for f in files:
        raw = (f.get("path") or "").strip()
        if not raw:
            continue
        # Reject absolute paths early (POSIX / Windows both).
        if raw.startswith(("/", "\\")) or (len(raw) > 2 and raw[1] == ":"):
            log.warning("rejecting absolute worker path", extra={"path": raw})
            continue
        p = PurePosixPath(raw.replace("\\", "/"))
        if ".." in p.parts or any(part == "" for part in p.parts[:-1]):
            log.warning("rejecting traversal worker path", extra={"path": raw})
            continue
        try:
            resolved = (root / p.as_posix()).resolve()
            resolved.relative_to(root)
        except (ValueError, OSError):
            log.warning("rejecting out-of-workspace worker path", extra={"path": raw})
            continue
        clean.append({"path": p.as_posix(), "content": str(f.get("content", ""))})
    return clean


def _infer_runtime_dependencies(files: list[dict[str, str]]) -> list[str]:
    local_modules = {
        PurePosixPath(f["path"]).stem
        for f in files
        if (f.get("path") or "").endswith(".py")
    }
    deps: set[str] = set()
    stdlib = getattr(sys, "stdlib_module_names", set())
    for f in files:
        if not (f.get("path") or "").endswith(".py"):
            continue
        try:
            tree = ast.parse(str(f.get("content") or ""))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            module_name = None
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = (alias.name or "").split(".", 1)[0]
                    if top:
                        module_name = top
                        _add_dep_if_needed(deps, module_name, stdlib, local_modules)
                continue
            if isinstance(node, ast.ImportFrom) and node.module:
                module_name = node.module.split(".", 1)[0]
            if module_name:
                _add_dep_if_needed(deps, module_name, stdlib, local_modules)
    return sorted(deps)


def _add_dep_if_needed(
    deps: set[str], module_name: str, stdlib: set[str], local_modules: set[str]
) -> None:
    if not module_name or module_name in {"__future__"}:
        return
    if module_name in stdlib or module_name in local_modules:
        return
    deps.add(_MODULE_TO_PACKAGE.get(module_name, module_name))


def _ensure_runtime_support_files(
    files: list[dict[str, str]], *, dependencies: list[str], variant_name: str
) -> list[dict[str, str]]:
    by_path = {f["path"]: f["content"] for f in files}
    merged_reqs = _merge_requirements(by_path.get("requirements.txt"), dependencies)
    if merged_reqs:
        by_path["requirements.txt"] = "\n".join(merged_reqs) + "\n"
    if "ENVIRONMENT.md" not in by_path:
        by_path["ENVIRONMENT.md"] = _render_environment_doc(
            dependencies=merged_reqs, variant_name=variant_name
        )
    return [{"path": path, "content": content} for path, content in by_path.items()]


def _merge_requirements(existing: str | None, inferred: list[str]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for line in (existing or "").splitlines():
        cleaned = line.strip()
        if not cleaned or cleaned.startswith("#"):
            continue
        if not _REQ_LINE_RE.match(cleaned):
            logger.warning("dropping unsupported requirements.txt entry", extra={"line": cleaned})
            continue
        cleaned = _normalize_requirement(cleaned)
        package_key = re.split(r"[<>=!~\[]", cleaned, 1)[0].strip().lower()
        if package_key in seen:
            continue
        ordered.append(cleaned)
        seen.add(package_key)
    for package in inferred:
        package_key = package.lower()
        if package_key in seen:
            continue
        ordered.append(package)
        seen.add(package_key)
    return ordered


def _render_environment_doc(*, dependencies: list[str], variant_name: str) -> str:
    deps_block = "\n".join(f"- `{dep}`" for dep in dependencies) or "- stdlib only"
    return (
        "# Environment\n\n"
        "This file is generated by the ResearchOS runtime to make execution "
        "dependencies explicit.\n\n"
        f"- Variant: `{variant_name}`\n"
        "- Entry point: `python train.py`\n"
        "- Interpreter: per-run isolated virtual environment when requirements are present\n"
        "- Install command: `python -m pip install -r requirements.txt`\n\n"
        "## Dependencies\n\n"
        f"{deps_block}\n"
    )


def _normalize_requirement(line: str) -> str:
    package_key = re.split(r"[<>=!~\[]", line, 1)[0].strip().lower()
    minimum = _min_compatible_version(package_key)
    if not minimum:
        return line
    match = re.search(r"(==|>=|<=|~=|!=|>|<)\s*([A-Za-z0-9*_.+-]+)", line)
    if match is None:
        return line
    op, version = match.groups()
    if op in {"==", "<", "<="}:
        if _version_tuple(version) < _version_tuple(minimum):
            return f"{package_key}>={minimum}"
        return line
    if op in {">=", "~="} and _version_tuple(version) < _version_tuple(minimum):
        return f"{package_key}>={minimum}"
    return line


def _min_compatible_version(package_key: str) -> str | None:
    constraints = _MIN_COMPAT_REQUIREMENTS.get(package_key)
    if not constraints:
        return None
    for py_version, minimum in sorted(constraints.items(), reverse=True):
        if sys.version_info >= py_version:
            return minimum
    return None


def _version_tuple(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for piece in re.split(r"[._-]", version):
        if not piece.isdigit():
            break
        parts.append(int(piece))
    return tuple(parts)
