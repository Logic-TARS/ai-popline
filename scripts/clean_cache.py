#!/usr/bin/env python
"""Remove regenerable cache artifacts from a project tree.

Thin CLI over ``content_pipeline.maintenance.cache_cleanup``, which also backs
the console maintenance tab and scripts/clean_cache.ps1. The default is a dry
run: nothing is deleted unless --apply is passed.

    python scripts/clean_cache.py                        # preview the project root
    python scripts/clean_cache.py --root /tmp/sandbox    # preview some other tree
    python scripts/clean_cache.py --apply                # delete the previewed items
    python scripts/clean_cache.py --apply --include-build

Version control data, virtualenvs, runtime data (output/, data/, logs/) and
agent state (.codegraph/) are never touched. Virtualenvs are recognised by
their pyvenv.cfg marker, so environments nested elsewhere (for example
dist/check-venv/) are skipped too; --extra only adds directory names that are
treated as cache.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
# Allow a plain `python scripts/clean_cache.py` from a checkout without the installed package.
if SOURCE_ROOT.is_dir() and str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from content_pipeline.maintenance.cache_cleanup import (  # noqa: E402
    Candidate,
    CleanupReport,
    cleanup_cache,
)


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _report_text(report: CleanupReport) -> str:
    verb = "removed" if report.applied else "to remove"
    grouped: dict[str, list[Candidate]] = {}
    for candidate in report.candidates:
        grouped.setdefault(candidate.category, []).append(candidate)
    lines = [
        f"root: {report.root}",
        f"mode: {'deleted' if report.applied else 'dry run (pass --apply to delete)'}",
        "",
    ]
    for category in sorted(grouped):
        entries = grouped[category]
        lines.append(f"[{category}] {len(entries)} item(s), {_human_size(sum(entry.size for entry in entries))} {verb}")
        lines.extend(
            f"  {'d' if entry.is_dir else 'f'} {entry.path.relative_to(report.root).as_posix()}" for entry in entries
        )
        lines.append("")
    for failure in report.warnings():
        lines.append(f"[FAILED] {failure['path']}: {failure['error']}")
    if report.failures:
        lines.append("")
    summary_verb = "removed" if report.applied else "would remove"
    lines.append(f"{summary_verb} {report.total} item(s), {_human_size(report.total_size)}")
    if not report.applied and report.candidates:
        lines.append("dry run only: no file was touched")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Remove regenerable cache artifacts (dry run unless --apply).")
    parser.add_argument(
        "--root", default=str(PROJECT_ROOT), help="tree to scan (default: this project's root directory)"
    )
    parser.add_argument("--apply", action="store_true", help="delete the listed items; without it nothing is touched")
    parser.add_argument(
        "--include-build", action="store_true", help="also remove build/, dist/, htmlcov/ and *.egg-info/"
    )
    parser.add_argument(
        "--extra",
        action="append",
        default=[],
        metavar="NAME",
        help="extra directory name to treat as cache (repeatable)",
    )
    parser.add_argument("--json", action="store_true", help="emit the result as JSON")
    args = parser.parse_args(argv)

    extra_dirs = frozenset(name.strip().strip("/\\") for name in args.extra if name.strip())
    try:
        report = cleanup_cache(args.root, apply=args.apply, include_build=args.include_build, extra_dirs=extra_dirs)
    except ValueError as exc:
        print(f"error: {exc}: {Path(args.root).expanduser()}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps({"root": str(report.root), **report.to_payload()}, ensure_ascii=False, indent=2))
    else:
        print(_report_text(report))
    return 1 if report.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
