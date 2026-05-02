"""Lightweight helpers for assembling prompts used by services.

Keeps the prompt-assembly logic in one place so services stay focused on
orchestration, not string concatenation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent.parent / "prompts"


def load_prompt(name: str) -> str:
    p = _HERE / name
    if p.exists():
        return p.read_text(encoding="utf-8")
    return ""


def dump_json_block(label: str, obj: Any) -> str:
    return f"\n### {label}\n```json\n{json.dumps(obj, indent=2, default=str)}\n```\n"


def safe_json_loads(text: str) -> Any:
    """Parse JSON even if the model wraps it in ```json ... ``` fences."""
    if not text:
        return None
    stripped = text.strip()
    if stripped.startswith("```"):
        # strip leading fence + optional language tag
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else stripped[3:]
        if stripped.endswith("```"):
            stripped = stripped[:-3]
    # find outermost JSON object/array
    for opener, closer in (("{", "}"), ("[", "]")):
        i = stripped.find(opener)
        if i == -1:
            continue
        depth = 0
        for j in range(i, len(stripped)):
            c = stripped[j]
            if c == opener:
                depth += 1
            elif c == closer:
                depth -= 1
                if depth == 0:
                    candidate = stripped[i : j + 1]
                    try:
                        return json.loads(candidate)
                    except Exception:  # noqa: BLE001
                        break
    try:
        return json.loads(stripped)
    except Exception:  # noqa: BLE001
        return None


def safe_json_object(text: str) -> dict[str, Any]:
    """Like ``safe_json_loads`` but guarantees a ``dict`` return.

    Providers occasionally return a bare JSON array even when we asked for an
    object, or they embed the answer as a single-element list. Rather than
    sprinkling ``isinstance(parsed, dict)`` checks through every service, this
    helper normalises:

    * ``dict`` → returned as-is
    * ``list`` of dicts with length 1 → inner dict is returned
    * ``list`` of dicts with length > 1 → wrapped as ``{"items": [...]}``
    * anything else (including ``None``, scalars, top-level non-object list
      of primitives) → empty ``dict``
    """
    parsed = safe_json_loads(text)
    if isinstance(parsed, dict):
        return parsed
    if isinstance(parsed, list):
        if len(parsed) == 1 and isinstance(parsed[0], dict):
            return parsed[0]
        if all(isinstance(item, dict) for item in parsed):
            return {"items": parsed}
    return {}


def salvage_object_list(text: str, *anchor_keys: str) -> list[dict[str, Any]]:
    """Recover complete dict items from a truncated JSON array.

    Models occasionally emit a valid prefix like ``{"ideas": [{...}, {...},``
    and then hit ``max_tokens`` before the closing brackets. ``json.loads``
    rightly rejects that, but we can still recover the fully-formed objects at
    the front of the array without fabricating anything.
    """
    if not text:
        return []
    stripped = text.strip()
    starts: list[int] = []
    if stripped.startswith("["):
        starts.append(0)
    for key in anchor_keys:
        idx = stripped.find(f'"{key}"')
        if idx == -1:
            continue
        arr_idx = stripped.find("[", idx)
        if arr_idx != -1:
            starts.append(arr_idx)
    decoder = json.JSONDecoder()
    for start in starts:
        i = start + 1 if stripped[start] == "[" else start
        recovered: list[dict[str, Any]] = []
        while i < len(stripped):
            while i < len(stripped) and stripped[i] in " \r\n\t,":
                i += 1
            if i >= len(stripped) or stripped[i] == "]":
                break
            try:
                obj, end = decoder.raw_decode(stripped[i:])
            except json.JSONDecodeError:
                break
            if not isinstance(obj, dict):
                break
            recovered.append(obj)
            i += end
        if recovered:
            return recovered
    return []
