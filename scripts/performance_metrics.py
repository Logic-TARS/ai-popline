#!/usr/bin/env python
"""Import platform metrics and print an experiment summary.

Examples:
    uv run python scripts/performance_metrics.py import --input metrics.xlsx
    uv run python scripts/performance_metrics.py summary
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from content_pipeline.analytics import import_metrics, summarize_metrics


def main() -> int:
    parser = argparse.ArgumentParser(description="Import and summarize short-video performance metrics")
    parser.add_argument("command", choices=("import", "summary", "report"))
    parser.add_argument("--input", type=Path)
    parser.add_argument("--database", type=Path, default=ROOT / "output" / "analytics" / "metrics.sqlite3")
    parser.add_argument("--start", dest="start_date")
    parser.add_argument("--end", dest="end_date")
    parser.add_argument("--experiment", dest="experiment_id")
    parser.add_argument("--platform")
    parser.add_argument("--content-line", dest="content_line")
    args = parser.parse_args()
    if args.command == "import":
        if not args.input:
            parser.error("import requires --input")
        result = import_metrics(args.input, args.database)
    else:
        result = summarize_metrics(args.database)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
