"""Parser unit tests for ``app.config.api_keys_file``.

We DO NOT exercise real provider keys here - synthetic test strings only.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def test_parse_kv_format(tmp_path: Path):
    from app.config.api_keys_file import parse_api_keys_file

    p = tmp_path / "keys.txt"
    p.write_text(
        """
# comment
OPENAI_API_KEY=sk-proj-abcdef1234567890TESTsentinel
ANTHROPIC_API_KEY="sk-ant-api03-TESTSENTINEL1234"
OPENAI_SMOKE_MODEL=gpt-4.1-mini
; extraneous ignored line
NOT_A_VALID_LINE_WITHOUT_EQUALS
        """,
        encoding="utf-8",
    )
    parsed = parse_api_keys_file(p)
    assert parsed.source_path == p
    assert parsed.pairs["OPENAI_API_KEY"].startswith("sk-proj-")
    assert parsed.pairs["ANTHROPIC_API_KEY"].startswith("sk-ant-")
    assert parsed.pairs["OPENAI_SMOKE_MODEL"] == "gpt-4.1-mini"
    assert "NOT_A_VALID_LINE_WITHOUT_EQUALS" not in parsed.pairs


def test_parse_labelled_checkbox_format(tmp_path: Path):
    """Accept the `- [ ] Provider N: <key>` format the user ships with."""
    from app.config.api_keys_file import parse_api_keys_file

    p = tmp_path / "keys.txt"
    p.write_text(
        """
- [ ] GPT 1: sk-proj-TESTSENTINELOPENAI1111
- [ ] GPT 2: sk-proj-TESTSENTINELOPENAI2222
- [ ] Anthropic 1: sk-ant-api03-TESTSENTINELANT1
Anthropic 2: sk-ant-api03-TESTSENTINELANT2
""",
        encoding="utf-8",
    )
    parsed = parse_api_keys_file(p)
    # First occurrence wins
    assert parsed.pairs["OPENAI_API_KEY"] == "sk-proj-TESTSENTINELOPENAI1111"
    assert parsed.pairs["ANTHROPIC_API_KEY"] == "sk-ant-api03-TESTSENTINELANT1"


def test_parse_prefix_only_fallback(tmp_path: Path):
    """Even unlabelled keys with a known prefix are classified."""
    from app.config.api_keys_file import parse_api_keys_file

    p = tmp_path / "keys.txt"
    p.write_text(
        "sk-proj-ONLYPREFIXTEST0000\nsk-ant-api03-ONLYPREFIXTEST1111\n",
        encoding="utf-8",
    )
    parsed = parse_api_keys_file(p)
    assert parsed.pairs["OPENAI_API_KEY"].startswith("sk-proj-")
    assert parsed.pairs["ANTHROPIC_API_KEY"].startswith("sk-ant-")


def test_parse_missing_file_returns_empty(tmp_path: Path):
    from app.config.api_keys_file import parse_api_keys_file

    parsed = parse_api_keys_file(tmp_path / "nope.txt")
    assert parsed.source_path is None
    assert parsed.pairs == {}


def test_resolve_prefers_override(tmp_path: Path, monkeypatch):
    from app.config.api_keys_file import resolve_api_keys_file

    override = tmp_path / "override.txt"
    override.write_text("OPENAI_API_KEY=sk-proj-OVERRIDE\n", encoding="utf-8")
    candidate = tmp_path / "candidate.txt"
    candidate.write_text("OPENAI_API_KEY=sk-proj-CANDIDATE\n", encoding="utf-8")

    parsed = resolve_api_keys_file(override=str(override), candidates=[candidate])
    assert parsed.pairs["OPENAI_API_KEY"] == "sk-proj-OVERRIDE"

    parsed = resolve_api_keys_file(override=None, candidates=[candidate, override])
    assert parsed.pairs["OPENAI_API_KEY"] == "sk-proj-CANDIDATE"
