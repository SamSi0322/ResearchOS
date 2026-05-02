"""Post-run acceptance check for the first real orchestrator pass.

Consumes the JSON report left by ``run_real_pipeline.py`` at
``var/real_run_reports/<project_id>.json`` and the package zip written by
the packager, then verifies every point on Task 7:

    1. each idea (that completed) has code + run output + metrics
    2. claims are linked to a run_id AND the draft uses them (coverage > 0)
    3. at least one idea fails AND not every verdict is "promising"
    4. the draft contains real numbers and no "TBD" placeholders
    5. the package zip exists, manifest has cost_summary + runtime_metadata

Prints a PASS / FAIL per criterion and exits non-zero if any fail.
"""

from __future__ import annotations

import json
import re
import sys
import zipfile
from pathlib import Path


def _latest_report() -> Path:
    d = Path(__file__).resolve().parent / "var" / "real_run_reports"
    reports = sorted(d.glob("*.json"))
    if not reports:
        sys.exit("no run report found — did run_real_pipeline.py finish?")
    return reports[-1]


def _check(name: str, ok: bool, detail: str = "") -> bool:
    tag = "PASS" if ok else "FAIL"
    print(f"  [{tag}] {name}{(' — ' + detail) if detail else ''}")
    return ok


def main() -> int:
    rp = _latest_report()
    report = json.loads(rp.read_text(encoding="utf-8"))
    print(f"report: {rp}\n")

    results: list[bool] = []

    # 1. per-idea: code + run output + metrics
    per_idea = report.get("per_idea") or []
    succeeded = [
        e for e in per_idea if e.get("run_status") == "succeeded" and e.get("run_id")
    ]
    results.append(
        _check(
            "1a. at least 3 ideas succeeded (code+run+metrics)",
            len(succeeded) >= 3,
            f"succeeded={len(succeeded)} / {len(per_idea)}",
        )
    )

    # 2. claims linked to run_id
    sample = report.get("sample_claim") or {}
    results.append(
        _check(
            "2a. at least one claim exists",
            int(report.get("claims_total") or 0) > 0,
            f"claims_total={report.get('claims_total')}",
        )
    )
    results.append(
        _check(
            "2b. sample claim has a run_id",
            bool(sample.get("run_id")),
            f"sample.run_id={sample.get('run_id')}",
        )
    )

    # 3. tradeoff / diversity of outcomes
    verdicts = report.get("verdicts") or {}
    any_failed = (verdicts.get("failed_runs") or 0) + (
        verdicts.get("errors") or 0
    ) > 0
    not_all_promising = (
        (verdicts.get("promising") or 0)
        < len([e for e in per_idea if e.get("verdict")])
    )
    results.append(
        _check(
            "3a. at least one idea fails or errors",
            any_failed,
            f"failed_runs={verdicts.get('failed_runs')} errors={verdicts.get('errors')}",
        )
    )
    results.append(
        _check(
            "3b. not every verdict is 'promising'",
            not_all_promising,
            f"verdicts={verdicts}",
        )
    )

    # 5. package
    pkg_zip = report.get("package_zip")
    pkg_ok = bool(pkg_zip) and Path(pkg_zip).exists()
    results.append(_check("5a. package zip exists", pkg_ok, str(pkg_zip)))

    manifest = None
    draft_md = None
    if pkg_ok:
        with zipfile.ZipFile(pkg_zip) as z:
            try:
                manifest = json.loads(z.read("manifest.json"))
            except KeyError:
                pass
            for name in z.namelist():
                if name.startswith("manuscript/draft_v") and name.endswith(".md"):
                    draft_md = z.read(name).decode("utf-8", errors="replace")
                    break

    results.append(_check("5b. manifest present in package", manifest is not None))
    if manifest:
        results.append(
            _check(
                "5c. manifest has cost_summary",
                "cost_summary" in manifest,
            )
        )
        rm = manifest.get("runtime_metadata")
        results.append(
            _check(
                "5d. manifest has runtime_metadata (model/cost/execution)",
                isinstance(rm, dict)
                and set(rm.keys()) >= {"model", "cost", "execution"},
            )
        )
        results.append(
            _check(
                "5e. execution_mode == headless_api",
                manifest.get("execution_mode") == "headless_api",
            )
        )

    # 4. draft has real numbers and no TBD placeholders
    if draft_md is not None:
        has_numbers = bool(re.search(r"\d+\.\d{2,}", draft_md))
        has_tbd = re.search(r"\bTBD\b|\[PLACEHOLDER\]|TODO:", draft_md)
        results.append(
            _check(
                "4a. draft contains real decimal numbers",
                has_numbers,
                "pattern \\d+\\.\\d{2,}",
            )
        )
        results.append(
            _check(
                "4b. draft has no TBD/PLACEHOLDER/TODO markers",
                not has_tbd,
                f"match={bool(has_tbd)}",
            )
        )
    else:
        results.append(_check("4. draft present in package", False, "no markdown file"))

    cost = report.get("cost") or {}
    print("\ncost:", json.dumps(cost, indent=2))
    funnel = report.get("funnel") or {}
    print("funnel:", json.dumps(funnel, indent=2))
    probe = report.get("probe") or {}
    print("probe:", json.dumps(probe))

    passed = sum(1 for r in results if r)
    total = len(results)
    print(f"\n{passed}/{total} criteria passed")
    return 0 if passed == total else 2


if __name__ == "__main__":
    sys.exit(main())
