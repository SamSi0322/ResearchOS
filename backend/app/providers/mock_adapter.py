"""Deterministic mock provider.

Used in two scenarios:
    1. No real credentials have been configured yet.
    2. The operator has explicitly chosen a ``mock`` credential.

Responses are shaped to match the routed task (ideas, screening, spec, code,
analysis, draft, review) so the end-to-end pipeline can be exercised without
burning tokens. Every mock output carries the literal string ``MOCK`` and
returns ``mock=True`` on the ``CompletionResult`` so downstream code can tag
artifacts accordingly.
"""

from __future__ import annotations

import hashlib
import json
import random
import time
from typing import Any

from .base import BaseProvider, CompletionRequest, CompletionResult


_MOCK_IDEA_TEMPLATES = [
    (
        "Attention dropout schedule tuned by per-head gradient norm",
        "Vary attention dropout per head based on measured gradient norm during warmup.",
        "Per-head adaptive dropout reduces overfitting in small transformers.",
        "Test set calibration ECE",
    ),
    (
        "Cross-layer representation similarity as an early-stopping signal",
        "Monitor CKA across consecutive layers and stop when similarity plateaus.",
        "CKA plateau is a robust early-stop criterion on short-context tasks.",
        "Validation loss at stop",
    ),
    (
        "Contrastive label smoothing for binary classification",
        "Replace static label smoothing with a contrastive, margin-aware variant.",
        "Contrastive smoothing improves minority-class recall at fixed precision.",
        "Minority-class recall @ precision=0.9",
    ),
    (
        "Mixture-of-tokenizers for low-resource text",
        "Train separate tokenizers per register and ensemble at embedding layer.",
        "Register-specific tokenization helps low-resource classification.",
        "Macro F1",
    ),
    (
        "Curriculum by example difficulty measured by loss variance",
        "Order minibatches by variance across a k-fold small surrogate model.",
        "Variance-based curriculum shortens convergence without hurting final loss.",
        "Epochs-to-convergence",
    ),
    (
        "Dropout on positional embeddings only",
        "Apply dropout exclusively to absolute positional embeddings.",
        "Positional-only dropout preserves content representation while regularising length bias.",
        "Length-generalisation accuracy",
    ),
]


def _deterministic_prefix(prompt: str) -> random.Random:
    h = hashlib.sha256(prompt.encode("utf-8")).digest()
    return random.Random(int.from_bytes(h[:8], "big"))


class MockProvider(BaseProvider):
    name = "mock"

    def __init__(self, model: str = "mock-1") -> None:
        self.model = model

    async def complete(self, req: CompletionRequest) -> CompletionResult:
        start = time.time()
        rng = _deterministic_prefix(f"{req.task_kind}:{req.prompt}")
        task = req.task_kind

        payload: dict[str, Any]
        text: str

        if task == "idea_generation":
            count = int(req.extra.get("count", 50) or 50)
            ideas = []
            for i in range(count):
                base = _MOCK_IDEA_TEMPLATES[i % len(_MOCK_IDEA_TEMPLATES)]
                title, summary, novelty, metric = base
                ideas.append(
                    {
                        "title": f"{title} (mock-{i + 1})",
                        "summary": f"{summary} Includes a MOCK variation #{i + 1}.",
                        "hypothesis": f"Variant {i + 1} of: {novelty}",
                        "novelty_claim": novelty,
                        "target_metric": metric,
                        "cluster_tag": f"cluster-{(i % 8) + 1}",
                    }
                )
            payload = {"ideas": ideas, "mock": True}
            text = json.dumps(payload)
        elif task == "structured_screening":
            items = req.extra.get("items") or []
            scored = []
            for it in items:
                scored.append(
                    {
                        "id": it.get("id"),
                        "novelty": round(rng.uniform(1, 5), 2),
                        "feasibility": round(rng.uniform(1, 5), 2),
                        "rigor": round(rng.uniform(1, 5), 2),
                        "impact": round(rng.uniform(1, 5), 2),
                        "rationale": "MOCK structured screening rationale.",
                    }
                )
            payload = {"scorecards": scored, "mock": True}
            text = json.dumps(payload)
        elif task == "spec_generation":
            payload = {
                "hypothesis": f"MOCK hypothesis for: {req.extra.get('idea_title', 'idea')}",
                "problem_framing": "MOCK framing: small, well-scoped question suitable for a pilot run.",
                "target_metrics": ["accuracy", "ece"],
                "dataset_assumptions": "Use a small public classification dataset bundled with the repo.",
                "baseline": "Baseline = vanilla training with default dropout.",
                "experiment_plan": "Train baseline and variant for a fixed budget; compare metrics.",
                "constraints": "<=2 min runtime, CPU-only.",
                "success_criteria": [
                    "Variant beats baseline on target metric by >= 1% absolute.",
                    "Result is reproducible across two seeds.",
                ],
                "stop_criteria": [
                    "Wall-clock exceeds allowed budget.",
                    "Variant underperforms baseline on two consecutive seeds.",
                ],
                "budget_estimate_usd": 0.0,
                "mock": True,
            }
            text = json.dumps(payload)
        elif task == "code_generation":
            files = _mock_experiment_files(
                variant_name=req.extra.get("variant_name", "mock_variant"),
                seed=int(req.extra.get("seed", 0) or 0),
            )
            payload = {
                "files": files,
                "summary": "MOCK code generation produced a toy classifier + metrics.json writer.",
                "assumptions": ["Python 3.11+", "standard library only (no torch in mock mode)"],
                "warnings": ["This is a MOCK experiment, numbers are deterministic but synthetic."],
                "mock": True,
            }
            text = json.dumps(payload)
        elif task == "code_review":
            payload = {
                "issues": [
                    {
                        "severity": "P2",
                        "location": "train.py",
                        "description": "MOCK reviewer note: add a deterministic seed.",
                        "suggestion": "Set random.seed() and numpy seed for reproducibility.",
                    }
                ],
                "patches": [],
                "summary": "MOCK code review: minor suggestions, no blocking issues.",
                "mock": True,
            }
            text = json.dumps(payload)
        elif task == "result_analysis":
            # `metrics` is the full metrics.json blob, which contains nested
            # dicts like baseline / variant / delta. Iterating its top-level
            # keys with float() would crash on those nested dicts, so we
            # ONLY work with the scalar baseline + variant sub-maps.
            metrics = req.extra.get("metrics") or {}
            baseline = (
                metrics.get("baseline")
                if isinstance(metrics.get("baseline"), dict)
                else req.extra.get("baseline")
            ) or {}
            variant = (
                metrics.get("variant")
                if isinstance(metrics.get("variant"), dict)
                else req.extra.get("variant")
            ) or {}

            def _scalar_items(d: dict) -> dict:
                out: dict[str, float] = {}
                for k, v in d.items():
                    if isinstance(v, bool) or isinstance(v, dict) or isinstance(v, list):
                        continue
                    try:
                        out[k] = float(v)
                    except (TypeError, ValueError):
                        continue
                return out

            baseline_s = _scalar_items(baseline)
            variant_s = _scalar_items(variant)

            # If metrics.json already ships a valid delta dict of scalars,
            # reuse it verbatim. Otherwise derive delta = variant - baseline
            # on shared scalar keys.
            existing_delta = metrics.get("delta")
            if isinstance(existing_delta, dict):
                delta = _scalar_items(existing_delta)
            else:
                delta = {
                    k: round(variant_s.get(k, 0.0) - baseline_s.get(k, 0.0), 4)
                    for k in set(baseline_s) | set(variant_s)
                }

            if delta:
                best = max(delta.values())
                worst = min(delta.values())
                if best > 0.005:
                    verdict = "promising"
                elif worst < -0.005:
                    verdict = "rejected"
                else:
                    verdict = "inconclusive"
            else:
                verdict = "inconclusive"

            claims: list[dict[str, Any]] = []
            for k, v in delta.items():
                claims.append(
                    {
                        "text": (
                            f"MOCK claim: variant changes {k} by {v:+.4f} vs baseline "
                            "on the local toy task."
                        ),
                        "kind": "quantitative",
                        "value": f"{v:+.4f}",
                        "quantitative": True,
                    }
                )
            if not claims:
                claims.append(
                    {
                        "text": "MOCK claim: no scalar baseline/variant metrics were found to compare.",
                        "kind": "qualitative",
                        "value": None,
                        "quantitative": False,
                    }
                )

            payload = {
                "verdict": verdict,
                "delta": delta,
                "claims": claims,
                "mock": True,
            }
            text = json.dumps(payload)
        elif task == "draft_generation":
            payload = {
                "sections": _mock_draft_sections(req.extra),
                "mock": True,
            }
            text = json.dumps(payload)
        elif task == "review":
            payload = {
                "issues": _mock_review_issues(req.extra),
                "mock": True,
            }
            text = json.dumps(payload)
        else:
            text = "MOCK: OK"
            payload = {"text": text, "mock": True}

        latency_ms = max(5, int((time.time() - start) * 1000))
        from app.config.model_alias import estimate_call_cost

        requested_model = (req.extra or {}).get("requested_model", req.model or self.model)
        alias_status_val = (req.extra or {}).get("alias_status")
        # Mock mode: zero real cost, zero split cost. Keep the estimator
        # symbolic in case a future operator wires a non-zero mock price
        # table for budget experiments.
        prompt_tokens = len(req.prompt) // 4
        cost = estimate_call_cost(prompt_tokens, self.model)
        return CompletionResult(
            provider=self.name,
            model=self.model,
            text=text,
            usage={"mock": True, "prompt_tokens": prompt_tokens},
            latency_ms=latency_ms,
            mock=True,
            reasoning_effort=req.reasoning_effort,
            thinking_mode=req.thinking_mode,
            policy_label=req.policy_label or "mock",
            requested_model=requested_model,
            actual_model=self.model,
            requested_reasoning_effort=req.reasoning_effort,
            actual_reasoning_effort=None,
            estimated_cost=cost,
            alias_status=alias_status_val,
            estimated_cost_usd=cost,
            raw=payload if isinstance(payload, dict) else {},
        )


def _mock_experiment_files(*, variant_name: str, seed: int) -> list[dict[str, str]]:
    train_py = f'''"""MOCK experiment generated by ResearchOS.

Runs a deterministic toy classifier comparing a vanilla baseline against a
variant called {variant_name!r}. Produces ``outputs/metrics.json`` and a tiny
``outputs/predictions.json`` so the rest of the pipeline has real evidence to
chew on.
"""
from __future__ import annotations

import json
import math
import random
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "outputs"
OUT.mkdir(parents=True, exist_ok=True)


def toy_dataset(n=512, seed=0):
    rng = random.Random(seed)
    xs = [(rng.gauss(0, 1), rng.gauss(0, 1)) for _ in range(n)]
    ys = [1 if (x[0] + 0.6 * x[1] + rng.gauss(0, 0.3)) > 0 else 0 for x in xs]
    return xs, ys


def train_and_eval(xs_train, ys_train, xs_test, ys_test, *, dropout=0.0, seed=0, variant=False):
    rng = random.Random(seed)
    # Very simple logistic-regression-ish toy, not a real model.
    w = [rng.uniform(-0.5, 0.5) for _ in range(2)]
    b = 0.0
    lr = 0.1 if not variant else 0.12
    steps = 120
    for _ in range(steps):
        for (x1, x2), y in zip(xs_train, ys_train):
            z = w[0] * x1 + w[1] * x2 + b
            p = 1.0 / (1.0 + math.exp(-z))
            err = p - y
            if variant and rng.random() < dropout:
                continue  # simulate dropout of a training example
            w[0] -= lr * err * x1
            w[1] -= lr * err * x2
            b -= lr * err

    correct = 0
    preds = []
    for (x1, x2), y in zip(xs_test, ys_test):
        z = w[0] * x1 + w[1] * x2 + b
        p = 1.0 / (1.0 + math.exp(-z))
        yhat = 1 if p >= 0.5 else 0
        correct += int(yhat == y)
        preds.append({{"x": [x1, x2], "y": y, "p": p, "yhat": yhat}})
    acc = correct / max(1, len(ys_test))
    brier = sum((p["p"] - p["y"]) ** 2 for p in preds) / len(preds)
    return {{"accuracy": acc, "brier": brier, "weights": w, "bias": b}}, preds


def main():
    seed = {seed}
    xs, ys = toy_dataset(n=1024, seed=seed)
    split = int(0.8 * len(xs))
    baseline, _ = train_and_eval(xs[:split], ys[:split], xs[split:], ys[split:], dropout=0.0, seed=seed)
    variant, preds = train_and_eval(
        xs[:split], ys[:split], xs[split:], ys[split:], dropout=0.05, seed=seed, variant=True
    )
    metrics = {{
        "variant_name": "{variant_name}",
        "seed": seed,
        "baseline": {{"accuracy": baseline["accuracy"], "brier": baseline["brier"]}},
        "variant": {{"accuracy": variant["accuracy"], "brier": variant["brier"]}},
        "delta": {{
            "accuracy": variant["accuracy"] - baseline["accuracy"],
            "brier": variant["brier"] - baseline["brier"],
        }},
        "mock": True,
        "generated_at": time.time(),
    }}
    (OUT / "metrics.json").write_text(json.dumps(metrics, indent=2))
    (OUT / "predictions.json").write_text(json.dumps(preds[:50], indent=2))
    print(json.dumps(metrics, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''
    readme = f"""# MOCK experiment: {variant_name}

Deterministic seed={seed}. No network calls, stdlib only. Produces
`outputs/metrics.json` and `outputs/predictions.json` which the analysis
service reads to produce claims.

This code was generated in ResearchOS mock mode; swap to a real provider
credential to produce a real experiment.
"""
    return [
        {"path": "train.py", "content": train_py},
        {"path": "README.md", "content": readme},
    ]


def _mock_draft_sections(extra: dict[str, Any]) -> list[dict[str, Any]]:
    claims = extra.get("claims") or []
    project_title = extra.get("project_title", "ResearchOS experiment")
    target_metric = extra.get("target_metric", "accuracy")
    bullets = "\n".join(f"- {c.get('text', '')} [evidence: {c.get('evidence_refs', [])}]" for c in claims)
    return [
        {
            "key": "abstract",
            "title": "Abstract",
            "content": (
                f"MOCK draft abstract for {project_title}. This draft was generated by "
                f"ResearchOS mock mode and must not be treated as a real manuscript."
            ),
            "claim_refs": [c.get("id") for c in claims[:3]],
        },
        {
            "key": "introduction",
            "title": "Introduction",
            "content": (
                f"We study a small-scale question in which we measure {target_metric} "
                "against a simple baseline. This is a MOCK draft."
            ),
            "claim_refs": [],
        },
        {
            "key": "method",
            "title": "Method",
            "content": (
                "The method is outlined in the experiment specification. The MOCK "
                "variant uses a stochastic training-example dropout scheme."
            ),
            "claim_refs": [],
        },
        {
            "key": "experiments",
            "title": "Experiments",
            "content": f"Seed 0. Small toy dataset. See evidence:\n{bullets}",
            "claim_refs": [c.get("id") for c in claims],
        },
        {
            "key": "results",
            "title": "Results",
            "content": (
                "MOCK results are drawn from stdlib code; deltas are real with respect to the "
                "toy baseline but must not be reported externally as scientific findings."
            ),
            "claim_refs": [c.get("id") for c in claims],
        },
        {
            "key": "limitations",
            "title": "Limitations",
            "content": (
                "Generated under mock mode with a toy dataset and a tiny model. Intended "
                "for pipeline validation, not for publication."
            ),
            "claim_refs": [],
        },
        {
            "key": "conclusion",
            "title": "Conclusion",
            "content": "MOCK draft — swap to a real provider to generate a substantive manuscript.",
            "claim_refs": [],
        },
    ]


def _mock_review_issues(extra: dict[str, Any]) -> list[dict[str, Any]]:
    reviewer_classes = extra.get("reviewer_classes") or [
        "methodology",
        "statistics",
        "novelty",
        "reproducibility",
        "manuscript",
    ]
    issues = []
    for i, rc in enumerate(reviewer_classes):
        severity = "P2" if i % 2 == 0 else "P3"
        issues.append(
            {
                "reviewer_class": rc,
                "severity": severity,
                "description": f"MOCK {rc} reviewer: consider adding more seeds.",
                "evidence": "derived from mock experiment metrics",
                "suggested_remediation": f"Rerun with 3 seeds and aggregate {rc} metrics.",
            }
        )
    return issues
