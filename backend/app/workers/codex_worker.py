"""Reviewer / patch suggester / test augmenter worker (logical role: ``codex``).

Naming note: the class is called ``CodexWorker`` because the reviewer role
is historically carried by OpenAI's code-focused models in the production
policy. It is NOT a wrapper around the Codex CLI and does NOT require an
interactive Codex terminal session. Like every other runtime component it
talks to the provider via the ``app/providers/`` HTTP adapters
(``execution_mode = "headless_api"``). Development tooling may use
``codex exec`` or ``claude -p`` outside the runtime; ResearchOS itself does
not depend on either.

This worker runs AFTER ClaudeCodeWorker has produced initial files. It acts
as an *adversarial second pass* — an independent reviewer, not another
builder. Concretely it:

* scans the existing file set for correctness, reproducibility, and metrics
  hygiene using the dedicated review prompt (``prompts/code_review.md``);
* may return patches (full file rewrites) that override specific files;
* may add tests (e.g. ``tests/test_metrics.py``) that the experiment runner
  can optionally execute.

Importantly this worker does NOT load the builder prompt — it would nudge the
model back into "produce new code" mode instead of reviewing what is in
front of it.
"""

from __future__ import annotations

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


class CodexWorker(BaseCodeWorker):
    name = "codex"

    def __init__(self, db: Session) -> None:
        self.db = db

    async def run(self, req: CodeWorkerRequest) -> CodeWorkerResult:
        policy = resolve_model_policy(Phase.code_review)
        router = get_provider_router(self.db)
        resolved = router.resolve_with_policy(
            policy, credential_id=req.provider_credential_id
        )

        prior = req.previous_files or []
        system = (
            "You are an INDEPENDENT adversarial reviewer for a tiny research "
            "experiment. A primary builder worker has already produced the files "
            "shown to you. You MUST act as a reviewer, NOT as a second builder. "
            "You MUST output JSON only. Focus on: correctness, reproducibility, "
            "metrics integrity, output-contract compliance, missing tests, and "
            "suspicious assumptions. Return full-file rewrites in `patches[]` "
            "for any file you want changed. Do not remove metrics.json "
            "generation. Do not introduce third-party deps unless the spec "
            "explicitly lists them."
        )
        review_guidance = load_prompt("code_review.md") or ""
        prompt = (
            review_guidance
            + dump_json_block("Prior files (produced by the builder)", prior)
            + dump_json_block(
                "Experiment spec",
                {
                    "hypothesis": req.hypothesis,
                    "experiment_plan": req.experiment_plan,
                    "target_metrics": req.target_metrics,
                    "success_criteria": req.success_criteria,
                    "stop_criteria": req.stop_criteria,
                    "seed": req.seed,
                    "variant_name": req.variant_name,
                    "dependency_constraints": req.dependency_constraints,
                },
            )
            + dump_json_block(
                "Output schema",
                {
                    "summary": "one paragraph describing overall code quality",
                    "issues": [
                        {
                            "severity": "P0|P1|P2|P3",
                            "location": "file:line",
                            "description": "what is wrong",
                            "suggestion": "what to do",
                        }
                    ],
                    "patches": [
                        {"path": "train.py", "content": "full rewritten file"}
                    ],
                    "warnings": ["string"],
                },
            )
        )

        completion_req = CompletionRequest(
            system=system,
            prompt=prompt,
            temperature=0.2,
            max_tokens=4500,
            json_mode=True,
            task_kind=TaskKind.code_review.value,
            extra={"variant_name": req.variant_name, "seed": req.seed},
        )
        completion_req = apply_policy(completion_req, policy)
        completion_req = apply_smoke_limits(completion_req, get_settings())
        result = await complete_with_ledger(
            self.db,
            project_id=req.project_id,
            adapter=resolved.adapter,
            req=completion_req,
            reference=f"code_review:{req.spec_id}",
            meta={"spec_id": req.spec_id, "idea_id": req.idea_id},
        )
        parsed = safe_json_object(result.text)
        patches = parsed.get("patches") or []
        summary = parsed.get("summary") or "codex review returned no summary"

        cleaned_patches = []
        for f in patches:
            path = str(f.get("path", "")).lstrip("/").strip()
            content = str(f.get("content", ""))
            if not path or not content:
                continue
            cleaned_patches.append({"path": path, "content": content})

        by_path = {f["path"]: f for f in prior}
        for patch in cleaned_patches:
            by_path[patch["path"]] = patch

        return CodeWorkerResult(
            files=list(by_path.values()),
            summary=summary,
            warnings=list(parsed.get("warnings") or []),
            assumptions=list(parsed.get("issues") or []),
            patches=cleaned_patches,
            provider=result.provider,
            model=result.model,
            mock=result.mock,
            latency_ms=result.latency_ms,
            estimated_cost_usd=float(
                getattr(result, "estimated_cost_usd", 0.0)
                or getattr(result, "estimated_cost", 0.0)
                or 0.0
            ),
        )
