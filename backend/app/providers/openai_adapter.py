"""OpenAI provider adapter.

Uses the Responses API (``POST /v1/responses``). We deliberately call HTTP
directly via ``httpx`` instead of depending on the ``openai`` SDK — this keeps
our runtime free of third-party client lock-in and makes the adapter easy to
swap or proxy. JSON-mode responses are requested via ``text.format`` when the
caller asks for structured output; otherwise we request plain text.
"""

from __future__ import annotations

import time
import asyncio
import os
from typing import Any

import httpx

from app.utils import get_logger

from .base import BaseProvider, CompletionRequest, CompletionResult, ProviderError

logger = get_logger(__name__)

_DEFAULT_MODEL = "gpt-4.1-mini"


def _safe_error_summary(r: httpx.Response) -> str:
    """Return a short, non-sensitive error summary.

    We intentionally never propagate raw provider error bodies upstream because
    they may echo parts of the prompt or other sensitive fragments. Instead we
    extract the structured ``error.type`` / ``error.code`` / ``error.message``
    when available, and fall back to a bare HTTP phrase otherwise.
    """
    try:
        data = r.json()
    except Exception:  # noqa: BLE001
        return f"upstream returned {len(r.text or '')} bytes of non-JSON"
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        t = err.get("type") or err.get("code") or "error"
        msg = err.get("message") or ""
        # Cap length to avoid accidental prompt echo in the message itself.
        return f"{t}: {msg[:120]}"
    return "upstream error (no structured detail)"


def _extract_text(data: dict[str, Any]) -> str:
    """Flatten the Responses API output to a single string.

    The shape is ``{"output": [{"type": "message", "content": [{"type":"output_text","text":"..."}]}]}``
    but we tolerate the older ``output_text`` convenience field as well.
    """
    if isinstance(data.get("output_text"), str):
        return data["output_text"]
    chunks: list[str] = []
    for item in data.get("output", []) or []:
        if item.get("type") == "message":
            for part in item.get("content", []) or []:
                if part.get("type") in ("output_text", "text"):
                    t = part.get("text")
                    if isinstance(t, str):
                        chunks.append(t)
        elif item.get("type") in ("output_text", "text"):
            t = item.get("text")
            if isinstance(t, str):
                chunks.append(t)
    return "".join(chunks)


def _output_item_types(data: dict[str, Any]) -> list[str]:
    types: list[str] = []
    for item in data.get("output", []) or []:
        item_type = item.get("type")
        if isinstance(item_type, str):
            types.append(item_type)
    return types


class OpenAIProvider(BaseProvider):
    name = "openai"

    def __init__(
        self,
        api_key: str,
        *,
        model: str | None = None,
        base_url: str | None = None,
        timeout: float = 60.0,
    ) -> None:
        if not api_key:
            raise ValueError("OpenAI api_key is required")
        self._api_key = api_key
        self.model = model or _DEFAULT_MODEL
        self._base_url = (base_url or "https://api.openai.com").rstrip("/")
        self._timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    async def complete(self, req: CompletionRequest) -> CompletionResult:
        model = req.model or self.model
        input_payload: list[dict[str, Any]] = []
        if req.system:
            input_payload.append({"role": "system", "content": req.system})
        input_payload.append({"role": "user", "content": req.prompt})

        body: dict[str, Any] = {
            "model": model,
            "input": input_payload,
            "max_output_tokens": req.max_tokens,
        }
        # gpt-5 / o-series reasoning models REJECT ``temperature``; gpt-4.x
        # accepts it. Send only when the target accepts.
        from app.config.model_alias import supports_temperature

        if supports_temperature(model):
            body["temperature"] = req.temperature
        if req.json_mode:
            body["text"] = {"format": {"type": "json_object"}}
        # Policy-driven reasoning effort. Only send it when the ACTUAL
        # model (post-alias) accepts the ``reasoning`` block - gpt-4.x
        # returns 400 on it. Our internal "xhigh" label maps to the API's
        # "high" string.
        if req.reasoning_effort:
            from app.config.model_alias import supports_reasoning_effort
            from app.config.model_policy import _OPENAI_EFFORT_WIRE

            if supports_reasoning_effort(model):
                wire = _OPENAI_EFFORT_WIRE.get(req.reasoning_effort, "high")
                body["reasoning"] = {"effort": wire}
            else:
                logger.info(
                    "openai reasoning.effort skipped (model does not support it)",
                    extra={"model": model, "requested_effort": req.reasoning_effort},
                )

        use_background = _should_use_background(model, req)
        if use_background:
            body["background"] = True
            body["store"] = True

        url = f"{self._base_url}/v1/responses"
        started = time.time()
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                r = await client.post(url, headers=self._headers(), json=body)
                if r.status_code < 400 and use_background:
                    data = r.json()
                    data = await self._poll_background_response(
                        client,
                        response_id=str(data.get("id") or ""),
                        started=started,
                    )
                    latency_ms = int((time.time() - started) * 1000)
                    return self._result_from_data(
                        data=data,
                        model=model,
                        req=req,
                        body=body,
                        latency_ms=latency_ms,
                        background=True,
                    )
        except httpx.HTTPError as e:
            raise ProviderError(self.name, f"network error: {e!s}") from e

        latency_ms = int((time.time() - started) * 1000)
        if r.status_code >= 400:
            # Provider error bodies can echo prompt content or even secrets.
            # Keep the full body for local logs (redacted by the logger filter)
            # but surface only the structured error code/type upstream.
            detail = _safe_error_summary(r)
            logger.warning(
                "openai responses error",
                extra={"status": r.status_code, "detail": detail, "body_len": len(r.text or "")},
            )
            raise ProviderError(
                self.name, f"http {r.status_code}: {detail}", status=r.status_code
            )

        data = r.json()
        return self._result_from_data(
            data=data,
            model=model,
            req=req,
            body=body,
            latency_ms=latency_ms,
            background=False,
        )

    async def _poll_background_response(
        self,
        client: httpx.AsyncClient,
        *,
        response_id: str,
        started: float,
    ) -> dict[str, Any]:
        if not response_id:
            raise ProviderError(self.name, "background response missing id")

        poll_interval = _env_float("RESEARCHOS_OPENAI_BACKGROUND_POLL_SECONDS", 10.0)
        max_wait = _env_float("RESEARCHOS_OPENAI_BACKGROUND_MAX_WAIT_SECONDS", self._timeout)
        transient_errors = 0
        max_transient_errors = int(
            _env_float("RESEARCHOS_OPENAI_BACKGROUND_MAX_POLL_ERRORS", 30.0)
        )
        url = f"{self._base_url}/v1/responses/{response_id}"
        while True:
            if time.time() - started > max_wait:
                raise ProviderError(
                    self.name,
                    f"background response timed out after {int(max_wait)}s: {response_id}",
                )
            await asyncio.sleep(max(1.0, poll_interval))
            try:
                r = await client.get(url, headers=self._headers())
            except httpx.HTTPError as e:
                transient_errors += 1
                if transient_errors > max_transient_errors:
                    raise ProviderError(
                        self.name,
                        f"background poll failed after {transient_errors} transient errors: {e!s}",
                    ) from e
                logger.warning(
                    "openai background poll transient error; continuing",
                    extra={
                        "response_id": response_id,
                        "error_count": transient_errors,
                        "error": str(e),
                    },
                )
                continue
            if r.status_code >= 400:
                detail = _safe_error_summary(r)
                raise ProviderError(
                    self.name,
                    f"background poll http {r.status_code}: {detail}",
                    status=r.status_code,
                )
            data = r.json()
            transient_errors = 0
            status = data.get("status")
            if status in {"queued", "in_progress"}:
                logger.info(
                    "openai background response still running",
                    extra={"response_id": response_id, "status": status},
                )
                continue
            if status == "completed":
                return data
            err = data.get("error") or data.get("incomplete_details") or {}
            raise ProviderError(
                self.name,
                f"background response ended with status={status}: {str(err)[:160]}",
            )

    def _result_from_data(
        self,
        *,
        data: dict[str, Any],
        model: str,
        req: CompletionRequest,
        body: dict[str, Any],
        latency_ms: int,
        background: bool,
    ) -> CompletionResult:
        text = _extract_text(data)
        usage = data.get("usage", {}) or {}
        from app.config.model_alias import estimate_call_cost, estimate_split_cost

        requested_model = (req.extra or {}).get("requested_model", req.model or model)
        alias_status_val = (req.extra or {}).get("alias_status")
        total_tokens = int(usage.get("total_tokens") or 0) or int(req.max_tokens)
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        # Fall back to splitting total 70/30 if the API didn't break it
        # down — closer to typical request shapes than 50/50.
        if not input_tokens and not output_tokens and total_tokens:
            input_tokens = int(total_tokens * 0.7)
            output_tokens = total_tokens - input_tokens
        est_cost = estimate_call_cost(total_tokens, model)
        est_cost_usd = estimate_split_cost(input_tokens, output_tokens, model)
        actual_effort = (
            body.get("reasoning", {}).get("effort")
            if isinstance(body.get("reasoning"), dict)
            else None
        )
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
            actual_reasoning_effort=actual_effort,
            estimated_cost=est_cost,
            alias_status=alias_status_val,
            estimated_cost_usd=est_cost_usd or est_cost,
            raw={
                "id": data.get("id"),
                "status": data.get("status"),
                "background": background,
                "incomplete_details": data.get("incomplete_details"),
                "output_item_types": _output_item_types(data),
                "output_text_len": len(text),
            },
        )


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _should_use_background(model: str, req: CompletionRequest) -> bool:
    extra = req.extra or {}
    if "background" in extra:
        return bool(extra.get("background"))
    if os.environ.get("RESEARCHOS_OPENAI_BACKGROUND", "").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }:
        return False
    if os.environ.get("RESEARCHOS_OPENAI_BACKGROUND", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return True
    return "pro" in (model or "").lower() and int(req.max_tokens or 0) >= 8000
