from __future__ import annotations

import logging
import re
import sys

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s :: %(message)s"

# Patterns we aggressively scrub before logging. Provider keys follow predictable
# shapes so we can redact them even if something accidentally passes one in.
_REDACT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"sk-[A-Za-z0-9_\-]{20,}"), "sk-***REDACTED***"),
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"), "sk-ant-***REDACTED***"),
    (re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]{20,}", re.IGNORECASE), "Bearer ***REDACTED***"),
]


class _RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D401
        msg = record.getMessage()
        for pat, repl in _REDACT_PATTERNS:
            msg = pat.sub(repl, msg)
        record.msg = msg
        record.args = ()
        return True


_CONFIGURED = False


def _configure_root() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    handler.addFilter(_RedactingFilter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    _configure_root()
    logger = logging.getLogger(name)
    return logger


def mask_secret(s: str, keep_head: int = 3, keep_tail: int = 4) -> str:
    """Build a masked preview for UI display, never for logs."""
    if not s:
        return ""
    if len(s) <= keep_head + keep_tail:
        return "*" * len(s)
    return f"{s[:keep_head]}{'*' * max(6, len(s) - keep_head - keep_tail)}{s[-keep_tail:]}"
