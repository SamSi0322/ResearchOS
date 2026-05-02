"""Email delivery with an always-safe local fallback.

Two modes:

* **SMTP** - when ``smtp_host`` (and optionally user / password) is set, the
  service sends via ``smtplib`` inside an ``asyncio.to_thread`` wrapper so
  FastAPI routes never block.
* **File outbox** - when no SMTP is configured, the email is written as an
  RFC-822 ``.eml`` file under ``var/outbox/`` with a structured JSON sidecar.
  This is how internal operators iterate on approval flows without setting
  up an SMTP relay, and it's the path tests use.

Security:
* The EmailService logs only metadata. Message bodies are rendered into the
  outbox file on disk; we do not log the body.
* If an outbound message happens to include an API-key shape, the redaction
  filter in ``app/utils/logger`` strips it before the handler writes.
"""

from __future__ import annotations

import asyncio
import json
import re
import smtplib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.utils import get_logger

logger = get_logger(__name__)


_KEY_PATTERNS = [
    re.compile(r"sk-proj-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"sk-ant-api03-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"sk-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]{20,}", re.IGNORECASE),
]


def _redact(body: str) -> str:
    redacted = body
    for pat in _KEY_PATTERNS:
        redacted = pat.sub("***REDACTED***", redacted)
    return redacted


@dataclass
class SendResult:
    ok: bool
    transport: str  # "smtp" | "outbox"
    outbox_path: str | None = None
    error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class EmailService:
    """Send emails; fall back to a file outbox when SMTP is absent."""

    def __init__(self) -> None:
        self.settings = get_settings()

    def _configured(self) -> bool:
        return bool(self.settings.smtp_host and self.settings.smtp_host.strip())

    async def send(
        self,
        *,
        to: str,
        cc: list[str] | None = None,
        subject: str,
        body_text: str,
        body_html: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> SendResult:
        cc = cc or []
        body_text_clean = _redact(body_text)
        body_html_clean = _redact(body_html) if body_html else None

        msg = EmailMessage()
        msg["From"] = self.settings.smtp_sender
        msg["To"] = to
        if cc:
            msg["Cc"] = ", ".join(cc)
        msg["Subject"] = subject
        for k, v in (headers or {}).items():
            msg[k] = v
        msg.set_content(body_text_clean)
        if body_html_clean:
            msg.add_alternative(body_html_clean, subtype="html")

        if self._configured():
            try:
                await asyncio.to_thread(self._send_smtp, msg, cc)
                logger.info(
                    "email sent via smtp",
                    extra={"to": to, "cc": cc, "subject": subject},
                )
                return SendResult(ok=True, transport="smtp")
            except Exception as e:  # noqa: BLE001
                # Do NOT fail the caller; fall back to outbox so the workflow
                # can still progress and the operator sees the message.
                logger.warning(
                    "smtp send failed, falling back to outbox: %s", e
                )

        path = self._write_outbox(msg, to=to, cc=cc, subject=subject, body=body_text_clean)
        logger.info(
            "email recorded to outbox",
            extra={"to": to, "cc": cc, "subject": subject, "path": str(path)},
        )
        return SendResult(ok=True, transport="outbox", outbox_path=str(path))

    # --- internals ------------------------------------------------------

    def _send_smtp(self, msg: EmailMessage, cc: list[str]) -> None:
        s = self.settings
        recipients = [msg["To"], *cc]
        if s.smtp_use_tls:
            with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=30) as client:
                client.ehlo()
                client.starttls()
                client.ehlo()
                if s.smtp_user and s.smtp_password:
                    client.login(s.smtp_user, s.smtp_password)
                client.send_message(msg, from_addr=s.smtp_sender, to_addrs=recipients)
        else:
            with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=30) as client:
                if s.smtp_user and s.smtp_password:
                    client.login(s.smtp_user, s.smtp_password)
                client.send_message(msg, from_addr=s.smtp_sender, to_addrs=recipients)

    def _write_outbox(
        self,
        msg: EmailMessage,
        *,
        to: str,
        cc: list[str],
        subject: str,
        body: str,
    ) -> Path:
        s = self.settings
        root = s.resolve_path(s.outbox_dir)
        root.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        safe_to = re.sub(r"[^A-Za-z0-9_.@-]+", "_", to)[:40]
        base = root / f"{ts}_{safe_to}"
        eml_path = base.with_suffix(".eml")
        meta_path = base.with_suffix(".json")
        eml_path.write_bytes(msg.as_bytes())
        meta_path.write_text(
            json.dumps(
                {
                    "to": to,
                    "cc": cc,
                    "subject": subject,
                    "sender": s.smtp_sender,
                    "written_at": ts,
                    "body_preview": body[:1000],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return eml_path


_instance: EmailService | None = None


def get_email_service() -> EmailService:
    global _instance
    if _instance is None:
        _instance = EmailService()
    return _instance


def reset_email_service_cache() -> None:
    """Tests only."""
    global _instance
    _instance = None
