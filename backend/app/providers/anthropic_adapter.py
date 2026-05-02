"""Anthropic (Claude) provider adapter using the Messages API."""

from __future__ import annotations

import time
from typing import Any

import httpx

from app.utils import get_logger

from .base import BaseProvider, CompletionRequest, CompletionResult, ProviderError

logger = get_logger(__name__)

_DEFAULT_MODEL = "claude-sonnet-4-6"
_API_VERSION = "2023-06-01"


def _safe_error_summary(r: httpx.Response) -> str:
    """See OpenAI adapter's equivalent; same rationale.

    Anthropic returns ``{"error": {"type": "...", "message": "..."}}`` on
    failure. We pull out structured fields only and cap length to avoid
    echoing prompt content in ProviderError payloads.
    """
    try:
        data = r.json()
    except Exception:  # noqa: BLE001
        return f"upstream returned {len(r.text or '')} bytes of non-JSON"
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        t = err.get("type") or "error"
        msg = err.get("message") or ""
        return f"{t}: {msg[:120]}"
    return "upstream error (no structured detail)"


def _extract_text(data: dict[str, Any]) -> str:
    parts: list[str] = []
    for block in data.get("content", []) or []:
        if block.get("type") == "text":
            t = block.get("text")
            if isinstance(t, str):
                parts.append(t)
    return "".join(parts)


class AnthropicProvider(BaseProvider):
    name = "anthropic"

    def __init__(
        self,
        api_key: str,
        *,
        model: str | None = None,
        base_url: str | None = None,
        timeout: float = 60.0,
    ) -> None:
        if not api_key:
            raise ValueError("Anthropic api_key is required")
        self._api_key = api_key
        self.model = model or _DEFAULT_MODEL
        self._base_url = (base_url or "https://api.anthropic.com").rstrip("/")
        self._timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._api_key,
            "anthropic-version": _API_VERSION,
            "content-type": "application/json",
        }

    async def complete(self, req: CompletionRequest) -> CompletionResult:
        model = req.model or self.model
        messages = [{"role": "user", "content": req.prompt}]
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": req.max_tokens,
            "messages": messages,
        }
        # Adaptive extended thinking only fires when the *actual* model
        # (post-alias) is one that accepts the ``thinking`` parameter. The
        # alias layer can point a production phase at an older Claude that
        # does not support thinking; in that case we silently drop the block
        # instead of 400-ing.
        from app.config.model_alias import supports_thinking

        if req.thinking_mode == "adaptive" and supports_thinking(model):
            import os as _os

            try:
                budget = int(_os.environ.get("RESEARCHOS_ANTHROPIC_THINKING_BUDGET", "8192"))
            except ValueError:
                budget = 8192
            if budget >= req.max_tokens:
                budget = max(1024, req.max_tokens // 2)
            body["thinking"] = {"type": "enabled", "budget_tokens": budget}
            # Anthropic enforces temperature=1 whenever thinking is enabled.
            # The live API 400s on any other value, so we always obey the
            # constraint here regardless of req.temperature. Callers that
            # need deterministic output should turn thinking off instead.
            body["temperature"] = 1.0
        else:
            if req.thinking_mode == "adaptive":
                logger.info(
                    "anthropic thinking skipped (model does not support it)",
                    extra={"model": model},
                )
            body["temperature"] = req.temperature
        if req.system:
            body["system"] = req.system
        if req.json_mode:
            # No native json-mode param yet; we add an instruction to the system
            # prompt and the calling service is responsible for json.loads.
            body["system"] = (
                (body.get("system") or "") + "\nRespond only with valid JSON, no prose."
            ).strip()

        url = f"{self._base_url}/v1/messages"
        started = time.time()
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                r = await client.post(url, headers=self._headers(), json=body)
        except httpx.HTTPError as e:
            raise ProviderError(self.name, f"network error: {e!s}") from e

        latency_ms = int((time.time() - started) * 1000)
        if r.status_code >= 400:
            detail = _safe_error_summary(r)
            logger.warning(
                "anthropic messages error",
                extra={"status": r.status_code, "detail": detail, "body_len": len(r.text or "")},
            )
            raise ProviderError(
                self.name, f"http {r.status_code}: {detail}", status=r.status_code
            )

        data = r.json()
        text = _extract_text(data)
        usage = data.get("usage", {}) or {}
        from app.config.model_alias import estimate_call_cost, estimate_split_cost

        requested_model = (req.extra or {}).get("requested_model", req.model or model)
        alias_status_val = (req.extra or {}).get("alias_status")
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        total_tokens = (input_tokens + output_tokens) or int(req.max_tokens)
        est_cost = estimate_call_cost(total_tokens, model)
        est_cost_usd = estimate_split_cost(input_tokens, output_tokens, model)
        return CompletionResult(
            provider=self.name,
            model=model,
            text=text,
            usage=usage,
            latency_ms=latency_ms,
            mock=False,
            reasoning_effort=req.reasoning_effort,
            thinking_mode=req.thinking_mode,
            policy_label=req.policy_label,
            requested_model=requested_model,
            actual_model=model,
            requested_reasoning_effort=req.reasoning_effort,
            actual_reasoning_effort=None,
            estimated_cost=est_cost,
            alias_status=alias_status_val,
            estimated_cost_usd=est_cost_usd or est_cost,
            raw={"id": data.get("id"), "stop_reason": data.get("stop_reason")},
        )
