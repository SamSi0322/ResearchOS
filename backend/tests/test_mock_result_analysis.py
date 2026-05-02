"""Regression tests for the mock adapter's ``result_analysis`` branch.

Earlier builds crashed with ``float() argument must be a string or a real
number, not 'dict'`` because the branch iterated the full metrics blob -
which contains nested ``baseline`` / ``variant`` / ``delta`` dicts - and
then called ``float()`` on those nested dict values.

These tests pin the corrected contract:

* ``delta`` is always a flat ``{metric_name: float}`` map.
* ``claims`` is a list of objects (NOT strings) matching the schema expected
  by ``ResultAnalysisService``.
* ``mock`` stays True.
"""

from __future__ import annotations

import json

import pytest


@pytest.mark.asyncio
async def test_mock_result_analysis_shapes_are_compatible():
    from app.providers.base import CompletionRequest
    from app.providers.mock_adapter import MockProvider

    metrics = {
        "variant_name": "demo_v1",
        "seed": 7,
        "baseline": {"accuracy": 0.90, "brier": 0.20},
        "variant": {"accuracy": 0.93, "brier": 0.18},
        "delta": {"accuracy": 0.03, "brier": -0.02},
        "mock": True,
        "generated_at": 1776870000.0,
    }

    res = await MockProvider().complete(
        CompletionRequest(
            prompt="analyze",
            task_kind="result_analysis",
            extra={"metrics": metrics, "baseline": metrics["baseline"]},
        )
    )
    data = json.loads(res.text)

    # Contract: delta is a flat scalar map.
    assert isinstance(data["delta"], dict)
    for k, v in data["delta"].items():
        assert isinstance(k, str)
        assert isinstance(v, (int, float)) and not isinstance(v, bool), (
            f"delta[{k}]={v!r} must be scalar, got {type(v).__name__}"
        )

    # Contract: claims is a list of objects, not strings.
    assert isinstance(data["claims"], list) and data["claims"], "claims must be non-empty list"
    for c in data["claims"]:
        assert isinstance(c, dict), f"claim must be object, got {type(c).__name__}"
        assert isinstance(c.get("text"), str) and c["text"]
        assert c.get("kind") in {"quantitative", "qualitative"}
        assert "value" in c
        assert isinstance(c.get("quantitative"), bool)

    assert data.get("mock") is True
    assert data.get("verdict") in {"promising", "inconclusive", "rejected"}


@pytest.mark.asyncio
async def test_mock_result_analysis_when_delta_needs_computing():
    """If the blob has baseline + variant but no delta, compute it from scalars only."""
    from app.providers.base import CompletionRequest
    from app.providers.mock_adapter import MockProvider

    metrics = {
        "baseline": {"accuracy": 0.80, "brier": 0.25, "notes": "skip me"},
        "variant": {"accuracy": 0.82, "brier": 0.20, "notes": "also skip"},
    }
    res = await MockProvider().complete(
        CompletionRequest(
            prompt="analyze",
            task_kind="result_analysis",
            extra={"metrics": metrics, "baseline": metrics["baseline"]},
        )
    )
    data = json.loads(res.text)
    delta = data["delta"]
    assert set(delta.keys()) == {"accuracy", "brier"}, (
        "only scalar keys may appear in delta"
    )
    assert abs(delta["accuracy"] - 0.02) < 1e-6
    assert abs(delta["brier"] - (-0.05)) < 1e-6


@pytest.mark.asyncio
async def test_mock_result_analysis_handles_missing_baseline():
    """Degenerate input must not crash; we return an inconclusive verdict."""
    from app.providers.base import CompletionRequest
    from app.providers.mock_adapter import MockProvider

    res = await MockProvider().complete(
        CompletionRequest(prompt="analyze", task_kind="result_analysis", extra={})
    )
    data = json.loads(res.text)
    assert data["verdict"] == "inconclusive"
    assert data["delta"] == {}
    assert data["claims"] and isinstance(data["claims"][0], dict)
