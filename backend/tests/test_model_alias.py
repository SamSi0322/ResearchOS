"""Alias layer + cost-awareness tests.

Covers the five surgical patches:

* test_model_alias_resolution       - alias map + pass-through
* test_requested_vs_actual_model    - CompletionResult carries both
* test_reasoning_effort_mapping     - xhigh label stays; wire is "high"
* test_anthropic_temperature_stable - temp=0.3 with thinking=adaptive
* test_cost_estimation_present      - estimate_call_cost populates result
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


def test_model_alias_resolution():
    from app.config.model_alias import alias_was_applied, resolve_model_alias

    # Current OpenAI production ids are sent directly; no alias should hide Pro usage.
    assert resolve_model_alias("gpt-5.4") == "gpt-5.4"
    assert resolve_model_alias("gpt-5.4-pro") == "gpt-5.4-pro"
    assert resolve_model_alias("claude-opus-4-7") == "claude-opus-4-5"
    assert not alias_was_applied("gpt-5.4")
    assert not alias_was_applied("gpt-5.4-pro")
    assert alias_was_applied("claude-opus-4-7")

    # Passthrough (no alias).
    assert resolve_model_alias("gpt-4.1-mini") == "gpt-4.1-mini"
    assert resolve_model_alias("mock-1") == "mock-1"
    assert resolve_model_alias("") == ""
    assert not alias_was_applied("gpt-4.1-mini")


def test_policy_keeps_requested_id_router_resolves_actual(fresh_db):
    """The policy keeps the requested id on the config; the router applies
    the alias only at adapter-build time. This test prevents the regression
    where policy.model silently mutated."""
    from app.config import Phase, RunMode, resolve_model_policy
    from app.config.model_alias import resolve_model_alias
    from app.core.enums import ProviderName
    from app.core.schemas import ProviderCredentialIn
    from app.db.session import SessionLocal
    from app.providers.router import get_provider_router
    from app.services import ProviderSecretService

    with SessionLocal() as db:
        ProviderSecretService(db).add(
            ProviderCredentialIn(
                provider=ProviderName.openai.value,
                label="policy-router",
                api_key="sk-proj-test-alias",
                default_model="gpt-4.1-mini",
            )
        )

    policy = resolve_model_policy(Phase.code_generation, mode=RunMode.production)
    assert policy.model == "gpt-5.4-pro"  # requested id survives on policy

    with SessionLocal() as db:
        resolved = get_provider_router(db).resolve_with_policy(policy)
    # Adapter gets the current Pro model directly.
    assert resolved.model == resolve_model_alias("gpt-5.4-pro")
    assert resolved.model == "gpt-5.4-pro"


@pytest.mark.asyncio
async def test_requested_vs_actual_model_on_completion_result():
    """The OpenAI adapter echoes both the requested and actual model on
    ``CompletionResult``."""
    from app.providers.base import CompletionRequest
    from app.providers.openai_adapter import OpenAIProvider

    class _FakeResp:
        status_code = 200
        text = "{}"

        def json(self):
            return {
                "id": "resp_alias",
                "output": [
                    {"type": "message", "content": [{"type": "output_text", "text": "ok"}]}
                ],
                "usage": {"total_tokens": 800},
                "status": "completed",
            }

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, headers=None, json=None):
            return _FakeResp()

    adapter = OpenAIProvider(api_key="sk-proj-alias-test", model="gpt-4.1")
    req = CompletionRequest(
        prompt="hi",
        reasoning_effort="xhigh",
        phase="code_review",
        extra={"requested_model": "gpt-5.4", "actual_model": "gpt-4.1"},
    )
    with patch("app.providers.openai_adapter.httpx.AsyncClient", return_value=_FakeClient()):
        result = await adapter.complete(req)
    assert result.requested_model == "gpt-5.4"
    assert result.actual_model == "gpt-4.1"
    assert result.model == "gpt-4.1"  # what was sent


@pytest.mark.asyncio
async def test_reasoning_effort_mapping_xhigh_to_high_on_wire():
    """``xhigh`` stays on the requested / policy label but goes out as
    ``high`` on the wire and is echoed on the result."""
    from app.providers.base import CompletionRequest
    from app.providers.openai_adapter import OpenAIProvider

    captured = {}

    class _FakeResp:
        status_code = 200
        text = "{}"

        def json(self):
            return {"id": "rx", "output": [], "usage": {"total_tokens": 1}}

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, headers=None, json=None):
            captured["body"] = json
            return _FakeResp()

    adapter = OpenAIProvider(api_key="sk-proj-effort", model="gpt-5.4")
    with patch("app.providers.openai_adapter.httpx.AsyncClient", return_value=_FakeClient()):
        result = await adapter.complete(
            CompletionRequest(prompt="x", reasoning_effort="xhigh")
        )
    assert captured["body"]["reasoning"]["effort"] == "high"
    assert result.requested_reasoning_effort == "xhigh"
    assert result.actual_reasoning_effort == "high"


@pytest.mark.asyncio
async def test_anthropic_temperature_stable_under_adaptive_thinking():
    """Anthropic adapter uses the stable temperature=0.3 (not 1.0) when
    thinking_mode == adaptive. This is the surgical fix."""
    from app.providers.anthropic_adapter import AnthropicProvider
    from app.providers.base import CompletionRequest

    captured = {}

    class _FakeResp:
        status_code = 200
        text = "{}"

        def json(self):
            return {
                "id": "msg_t",
                "content": [{"type": "text", "text": "ok"}],
                "usage": {"input_tokens": 5, "output_tokens": 7},
                "stop_reason": "end_turn",
            }

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, headers=None, json=None):
            captured["body"] = json
            return _FakeResp()

    # Use a thinking-capable model id so the adapter actually sends the
    # thinking block. With a non-capable model (e.g. claude-3-opus) the
    # adapter silently drops thinking; that branch is covered separately.
    adapter = AnthropicProvider(api_key="sk-ant-test", model="claude-opus-4-7")
    with patch("app.providers.anthropic_adapter.httpx.AsyncClient", return_value=_FakeClient()):
        await adapter.complete(
            CompletionRequest(
                prompt="x",
                max_tokens=2000,
                thinking_mode="adaptive",
            )
        )
    body = captured["body"]
    # Anthropic requires temperature=1 when thinking is enabled.
    assert body["temperature"] == pytest.approx(1.0)
    assert body["thinking"]["type"] == "enabled"
    assert body["thinking"]["budget_tokens"] < body["max_tokens"]


@pytest.mark.asyncio
async def test_cost_estimation_present():
    """Every ``CompletionResult`` carries an ``estimated_cost`` derived from
    ``MODEL_COST_PER_1K``."""
    from app.providers.base import CompletionRequest
    from app.providers.openai_adapter import OpenAIProvider

    class _FakeResp:
        status_code = 200
        text = "{}"

        def json(self):
            # Return usage so the adapter has real token counts to multiply.
            return {
                "id": "r",
                "output": [
                    {"type": "message", "content": [{"type": "output_text", "text": "x"}]}
                ],
                "usage": {"total_tokens": 2000, "input_tokens": 1500, "output_tokens": 500},
                "status": "completed",
            }

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, headers=None, json=None):
            return _FakeResp()

    adapter = OpenAIProvider(api_key="sk-proj-cost", model="gpt-4.1")
    with patch("app.providers.openai_adapter.httpx.AsyncClient", return_value=_FakeClient()):
        result = await adapter.complete(CompletionRequest(prompt="hi"))
    # 2000 tokens * $0.0025/1k = $0.005
    assert result.estimated_cost == pytest.approx(0.005)


def test_cost_estimation_helper_rounds_and_handles_unknown_models():
    from app.config.model_alias import estimate_call_cost

    # Known model: exact calculation, rounded to 6 decimals.
    assert estimate_call_cost(2500, "gpt-4.1") == pytest.approx(0.00625)
    # Unknown: 0.0 (never raises).
    assert estimate_call_cost(10_000, "not-a-real-model") == 0.0
    # Zero / negative tokens: 0.0.
    assert estimate_call_cost(0, "gpt-4.1") == 0.0
    assert estimate_call_cost(-50, "gpt-4.1") == 0.0
