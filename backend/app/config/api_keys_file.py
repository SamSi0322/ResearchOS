"""Parser for the local-dev ``API_KEYS.txt`` fallback file.

Two formats are supported on purpose so the operator does not need to reformat
an existing key file:

    1. Canonical ``KEY=VALUE`` (preferred, documented format). Blank lines and
       anything starting with ``#`` or ``//`` are ignored.

    2. Free-form "provider name: <key>" lines (the variant we ship in
       ``docs/smoke-mode.md``). The provider name is matched case-insensitively.
       Common aliases are recognised: GPT / OpenAI -> OPENAI_API_KEY,
       Claude / Anthropic -> ANTHROPIC_API_KEY. First match per provider wins.

For both formats we also pattern-match known key prefixes (``sk-proj-``,
``sk-ant-``) as a last-resort fallback so a file of pasted keys still yields a
usable set.

This module intentionally does ONE job: return a ``dict[str, str]`` from a
file path. It never logs any value, never prints a value, and is not invoked
by any route - only the ``CredentialBootstrapService`` uses it at process
startup.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

# We route internal diagnostic info through a dedicated logger name so the
# global redaction filter still scrubs anything that slips past us.
logger = logging.getLogger("app.config.api_keys_file")


_KV_RE = re.compile(r"^\s*([A-Z][A-Z0-9_]+)\s*=\s*(.+?)\s*$")
_LINE_RE = re.compile(
    r"""^\s*
        (?:[-*]\s*)?                # optional list bullet
        (?:\[[ xX]?\]\s*)?          # optional markdown checkbox
        (?P<label>[A-Za-z][A-Za-z0-9 _\-]+?)\s*
        [:=]\s*
        (?P<value>\S.+?)\s*$
    """,
    re.VERBOSE,
)

_OPENAI_ALIASES = ("openai", "gpt", "chatgpt")
_ANTHROPIC_ALIASES = ("anthropic", "claude", "sonnet", "haiku", "opus")

# Prefixes we recognise as "almost certainly a provider key of kind X" when we
# can't rely on a label.
_PREFIX_HINTS = {
    "sk-proj-": "OPENAI_API_KEY",
    "sk-ant-": "ANTHROPIC_API_KEY",
}

# The env names we publish. Callers should treat these as stable.
OPENAI_ENV = "OPENAI_API_KEY"
ANTHROPIC_ENV = "ANTHROPIC_API_KEY"
OPENAI_SMOKE_MODEL_ENV = "OPENAI_SMOKE_MODEL"
ANTHROPIC_SMOKE_MODEL_ENV = "ANTHROPIC_SMOKE_MODEL"


@dataclass(frozen=True)
class ParsedKeys:
    source_path: Path | None
    pairs: dict[str, str]

    def get(self, name: str) -> str | None:
        return self.pairs.get(name)


def _normalize_label(label: str) -> str | None:
    low = label.strip().lower()
    for alias in _OPENAI_ALIASES:
        if low.startswith(alias) or alias in low.split():
            return OPENAI_ENV
    for alias in _ANTHROPIC_ALIASES:
        if low.startswith(alias) or alias in low.split():
            return ANTHROPIC_ENV
    # Preserve any already-uppercase env-style label (but restrict to safe
    # alphabet to avoid accidentally setting random env names).
    upper = label.strip().upper().replace(" ", "_")
    if re.fullmatch(r"[A-Z][A-Z0-9_]+", upper):
        return upper
    return None


def _prefix_bucket(value: str) -> str | None:
    for prefix, bucket in _PREFIX_HINTS.items():
        if value.startswith(prefix):
            return bucket
    return None


def parse_api_keys_file(path: Path) -> ParsedKeys:
    """Parse ``API_KEYS.txt``. Returns an empty ``ParsedKeys`` if the file is
    missing or unreadable - never raises in the hot path."""
    if not path.exists():
        return ParsedKeys(source_path=None, pairs={})
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        logger.warning("could not read api keys file", extra={"path": str(path), "err": str(e)})
        return ParsedKeys(source_path=path, pairs={})

    pairs: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line:
            continue
        stripped = line.lstrip()
        if stripped.startswith(("#", "//", ";")):
            continue

        # 1. KEY=VALUE format.
        m = _KV_RE.match(line)
        if m:
            name = m.group(1).strip().upper()
            value = m.group(2).strip().strip('"').strip("'")
            if value and name not in pairs:
                pairs[name] = value
            continue

        # 2. Labelled "provider: value" format.
        m = _LINE_RE.match(line)
        if m:
            label = m.group("label")
            value = m.group("value").strip().strip('"').strip("'").rstrip("=")
            if not value:
                continue
            mapped = _normalize_label(label)
            if mapped is None:
                # Try to bucket by prefix instead.
                mapped = _prefix_bucket(value)
            if mapped and mapped not in pairs:
                pairs[mapped] = value
            continue

        # 3. Last-resort: the line is just a key string we recognise.
        bucket = _prefix_bucket(stripped)
        if bucket and bucket not in pairs:
            pairs[bucket] = stripped

    logger.info(
        "api keys file parsed",
        extra={
            "path": str(path),
            "keys_found": sorted(pairs.keys()),
            "count": len(pairs),
        },
    )
    return ParsedKeys(source_path=path, pairs=pairs)


def resolve_api_keys_file(
    *, override: str | None, candidates: list[Path]
) -> ParsedKeys:
    """Resolve and parse the first API keys file that exists.

    ``override`` (from ``RESEARCHOS_API_KEYS_FILE``) wins if set; otherwise we
    walk ``candidates`` in order.
    """
    if override:
        p = Path(override).expanduser()
        return parse_api_keys_file(p)
    for c in candidates:
        if c.exists():
            return parse_api_keys_file(c)
    return ParsedKeys(source_path=None, pairs={})
