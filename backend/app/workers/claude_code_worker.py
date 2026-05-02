"""Primary code builder worker (logical role: ``claude_code``).

Naming note: the class is called ``ClaudeCodeWorker`` because the builder
role was historically carried by Anthropic's Claude model family in the
production policy. It is NOT a wrapper around the Claude Code CLI / IDE and
does NOT require an interactive Claude Code session to be open anywhere.
At runtime this worker:

    1. asks ``resolve_model_policy(Phase.code_generation)`` for the phase
       config,
    2. routes through the standard ``ProviderRouter`` to a headless HTTP
       adapter under ``app/providers/``,
    3. parses the JSON response into a file tree.

Execution mode is always ``headless_api``. If no real provider is configured
the router returns the mock adapter, which still produces a runnable toy
experiment so the pipeline can exercise the full path.
"""

from __future__ import annotations

from dataclasses import replace
import re

from sqlalchemy.orm import Session

from app.config import Phase, get_settings, resolve_model_policy
from app.core.enums import TaskKind
from app.providers.base import CompletionRequest, apply_policy, apply_smoke_limits
from app.providers.router import get_provider_router
from app.services._prompts import dump_json_block, load_prompt, safe_json_object
from app.services.provider_call_ledger import complete_with_ledger
from app.utils import get_logger

from .base import BaseCodeWorker, CodeWorkerRequest, CodeWorkerResult

logger = get_logger(__name__)


class ClaudeCodeWorker(BaseCodeWorker):
    name = "claude_code"

    def __init__(self, db: Session) -> None:
        self.db = db

    async def run(self, req: CodeWorkerRequest) -> CodeWorkerResult:
        policy = resolve_model_policy(Phase.code_generation)
        router = get_provider_router(self.db)
        resolved = router.resolve_with_policy(
            policy, credential_id=req.provider_credential_id
        )

        system = (
            "You are a careful research engineer producing a small, deterministic, "
            "runnable Python experiment. You MUST output JSON only. The code must: "
            "(a) prefer Python stdlib, but if you use any third-party package you "
            "MUST include a requirements.txt and an ENVIRONMENT.md explaining it, "
            "(b) be deterministic given a seed, (c) write metrics.json and any "
            "predictions to ../outputs/ relative to train.py (that is the run "
            "workspace output directory), (d) exit 0 whenever the experiment ran "
            "to completion and wrote metrics, even if the hypothesis is rejected; "
            "reserve non-zero exit codes for infrastructure/runtime errors only."
        )
        prompt = (
            (load_prompt("code_generation.md") or "")
            + dump_json_block(
                "Experiment spec",
                {
                    "hypothesis": req.hypothesis,
                    "experiment_plan": req.experiment_plan,
                    "target_metrics": req.target_metrics,
                    "baseline": req.baseline,
                    "dataset_assumptions": req.dataset_assumptions,
                    "constraints": req.constraints,
                    "success_criteria": req.success_criteria,
                    "stop_criteria": req.stop_criteria,
                    "seed": req.seed,
                    "variant_name": req.variant_name,
                    "dependency_constraints": req.dependency_constraints,
                    "extra_instructions": req.extra_instructions or "",
                },
            )
            + dump_json_block(
                "Output schema",
                {
                    "files": [
                        {"path": "train.py", "content": "..."},
                        {"path": "requirements.txt", "content": "numpy==..."},
                        {"path": "ENVIRONMENT.md", "content": "# Runtime setup"},
                    ],
                    "summary": "one paragraph",
                    "assumptions": ["string"],
                    "warnings": ["string"],
                },
            )
            + "\nReturn only JSON."
        )

        completion_req = CompletionRequest(
            system=system,
            prompt=prompt,
            temperature=0.2,
            max_tokens=4500,
            json_mode=True,
            task_kind=TaskKind.code_generation.value,
            extra={"variant_name": req.variant_name, "seed": req.seed},
        )
        completion_req = apply_policy(completion_req, policy)
        completion_req = apply_smoke_limits(completion_req, get_settings())
        result = await complete_with_ledger(
            self.db,
            project_id=req.project_id,
            adapter=resolved.adapter,
            req=completion_req,
            reference=f"code_generation:{req.spec_id}",
            meta={"spec_id": req.spec_id, "idea_id": req.idea_id, "repair": False},
        )
        estimated_cost_usd = float(
            getattr(result, "estimated_cost_usd", 0.0)
            or getattr(result, "estimated_cost", 0.0)
            or 0.0
        )
        diagnostics = _completion_diagnostics(result)
        parsed, cleaned = _extract_response_payload(result.text)
        files = cleaned
        summary = parsed.get("summary") or "code generation returned no summary"
        warnings = list(parsed.get("warnings") or [])
        assumptions = list(parsed.get("assumptions") or [])
        used_fallback = False
        if not files:
            if result.mock:
                logger.warning(
                    "claude_code_worker: no files returned, falling back to trivial experiment"
                )
                files = _fallback_files(req)
                summary = (
                    "fallback minimal experiment used because provider returned no files"
                )
                used_fallback = True
            else:
                logger.warning(
                    "claude_code_worker: provider returned no files for real run"
                )
                repaired = await self._repair_empty_response(
                    resolved=resolved,
                    req=req,
                    extra_prompt=prompt,
                    policy=policy,
                    initial_result=result,
                )
                if repaired is not None:
                    repaired_result, repaired_parsed, repaired_cleaned = repaired
                    estimated_cost_usd += float(
                        getattr(repaired_result, "estimated_cost_usd", 0.0)
                        or getattr(repaired_result, "estimated_cost", 0.0)
                        or 0.0
                    )
                    result = repaired_result
                    diagnostics = _completion_diagnostics(
                        repaired_result, initial_result=diagnostics
                    )
                    parsed = repaired_parsed
                    files = repaired_cleaned
                    warnings = list(parsed.get("warnings") or [])
                    assumptions = list(parsed.get("assumptions") or [])
                    warnings.append("builder response repaired after empty initial output")
                    summary = parsed.get("summary") or summary

        if not any(c["path"].endswith(".py") for c in files):
            if result.mock:
                files.extend(_fallback_files(req))
                used_fallback = True
            else:
                logger.warning(
                    "claude_code_worker: provider output had no runnable Python files"
                )

        return CodeWorkerResult(
            files=files,
            summary=summary,
            warnings=warnings,
            assumptions=assumptions,
            provider=result.provider,
            model=result.model,
            mock=result.mock,
            latency_ms=result.latency_ms,
            used_fallback=used_fallback,
            estimated_cost_usd=estimated_cost_usd,
            diagnostics=diagnostics,
        )

    async def _repair_empty_response(
        self, *, resolved, req, extra_prompt: str, policy, initial_result
    ):
        if _is_reasoning_only_empty(initial_result):
            lead = (
                "Your previous response spent tokens on internal reasoning and "
                "returned no final JSON. Do not spend tokens thinking. Return a "
                "compact JSON object immediately."
            )
        else:
            lead = "Your previous response did not contain a usable file tree."
        repair_prompt = (
            f"{lead} Repair it now. Return JSON only with at least one runnable Python file "
            "named train.py. If you use any third-party dependency, include "
            "requirements.txt and ENVIRONMENT.md.\n\n"
            + extra_prompt
        )
        repair_req = CompletionRequest(
            system=(
                "You are repairing a malformed code-generation response. "
                "Return only valid JSON matching the requested schema."
            ),
            prompt=repair_prompt,
            temperature=0.0,
            max_tokens=5000,
            json_mode=True,
            task_kind=TaskKind.code_generation.value,
            extra={"variant_name": req.variant_name, "seed": req.seed, "repair": True},
        )
        repair_req = apply_policy(repair_req, policy)
        # gpt-5 builder calls can occasionally consume all output tokens as
        # reasoning and return 200 OK with an empty final answer. For repair
        # attempts, force a cheaper/shallower reasoning path so we maximize
        # the chance of getting an actual file tree back.
        if getattr(initial_result, "provider", "") == "openai":
            repair_req = replace(
                repair_req,
                reasoning_effort=(
                    None if _is_reasoning_only_empty(initial_result) else "low"
                ),
            )
        repair_req = apply_smoke_limits(repair_req, get_settings())
        repaired = await complete_with_ledger(
            self.db,
            project_id=req.project_id,
            adapter=resolved.adapter,
            req=repair_req,
            reference=f"code_generation_repair:{req.spec_id}",
            meta={"spec_id": req.spec_id, "idea_id": req.idea_id, "repair": True},
        )
        parsed, files = _extract_response_payload(repaired.text)
        if files:
            return repaired, parsed, files
        return None


def _completion_diagnostics(result, *, initial_result: dict | None = None) -> dict:
    usage = dict(getattr(result, "usage", {}) or {})
    raw = dict(getattr(result, "raw", {}) or {})
    data = {
        "provider": getattr(result, "provider", ""),
        "model": getattr(result, "model", ""),
        "latency_ms": int(getattr(result, "latency_ms", 0) or 0),
        "text_len": len(getattr(result, "text", "") or ""),
        "usage": usage,
        "raw": raw,
        "reasoning_only_empty": _is_reasoning_only_empty(result),
    }
    if initial_result:
        data["initial_attempt"] = initial_result
    return data


def _is_reasoning_only_empty(result) -> bool:
    text = getattr(result, "text", "") or ""
    if text.strip():
        return False
    usage = dict(getattr(result, "usage", {}) or {})
    output_tokens = int(usage.get("output_tokens") or 0)
    reasoning_tokens = int(
        ((usage.get("output_tokens_details") or {}) or {}).get("reasoning_tokens") or 0
    )
    if output_tokens <= 0 or reasoning_tokens <= 0:
        return False
    return reasoning_tokens >= output_tokens


def _extract_response_payload(text: str) -> tuple[dict, list[dict[str, str]]]:
    parsed = safe_json_object(text)
    files = _clean_files(parsed.get("files") or [])
    if files:
        return parsed, files
    salvaged = _salvage_python_files(text)
    if salvaged:
        parsed = dict(parsed)
        parsed.setdefault("summary", "recovered train.py from fenced code block")
        return parsed, salvaged
    return parsed, []


def _clean_files(files: list[dict]) -> list[dict[str, str]]:
    cleaned = []
    for f in files:
        path = str(f.get("path", "")).lstrip("/").strip()
        content = str(f.get("content", ""))
        if not path or not content:
            continue
        cleaned.append({"path": path, "content": content})
    return cleaned


def _salvage_python_files(text: str) -> list[dict[str, str]]:
    if not text:
        return []
    patterns = [
        r"```python\s*(.*?)```",
        r"```py\s*(.*?)```",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
        if not match:
            continue
        content = match.group(1).strip()
        if not content or content.startswith("{"):
            continue
        return [{"path": "train.py", "content": content}]
    return []


def _fallback_files(req: CodeWorkerRequest) -> list[dict[str, str]]:
    """A minimal runnable experiment used if the worker response is empty.

    Purely local, stdlib-only. Clearly tagged FALLBACK in metrics.json.
    """
    train = f'''"""FALLBACK experiment (no provider output). Deterministic toy.

ResearchOS code worker could not produce files, so we run a built-in fallback.
"""
from __future__ import annotations
import json, math, random, sys
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "outputs"
OUT.mkdir(parents=True, exist_ok=True)


def main() -> int:
    rng = random.Random({req.seed})
    xs = [(rng.gauss(0, 1), rng.gauss(0, 1)) for _ in range(400)]
    ys = [1 if x[0] > 0 else 0 for x in xs]
    correct = sum(1 for (x, y) in zip(xs, ys) if (x[0] > 0) == bool(y))
    acc = correct / len(ys)
    metrics = {{
        "variant_name": "{req.variant_name}",
        "seed": {req.seed},
        "baseline": {{"accuracy": acc * 0.97}},
        "variant": {{"accuracy": acc}},
        "delta": {{"accuracy": acc - acc * 0.97}},
        "fallback": True,
    }}
    (OUT / "metrics.json").write_text(json.dumps(metrics, indent=2))
    print(json.dumps(metrics, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''
    return [{"path": "train.py", "content": train}]
