from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path


def build_zip(
    out_path: str | Path,
    files: list[tuple[str, str | Path]],
    virtual_files: dict[str, str] | None = None,
) -> Path:
    """Build a zip from a list of (arcname, source_path) plus virtual files.

    `virtual_files` maps arcname -> string content (utf-8).
    """
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for arcname, src in files:
            p = Path(src)
            if p.exists() and p.is_file():
                zf.write(p, arcname=arcname)
        if virtual_files:
            for arcname, content in virtual_files.items():
                info = zipfile.ZipInfo(
                    filename=arcname, date_time=datetime.now(timezone.utc).timetuple()[:6]
                )
                info.compress_type = zipfile.ZIP_DEFLATED
                zf.writestr(info, content)
    return out


def dump_json(obj, indent: int = 2) -> str:
    return json.dumps(obj, indent=indent, default=str, sort_keys=False)
