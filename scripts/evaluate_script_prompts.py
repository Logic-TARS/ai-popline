from __future__ import annotations

import argparse
import json
from pathlib import Path

from content_pipeline.script_generation.quality import validate_script
from content_pipeline.script_generation.service import build_structured_facts

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "script_generation"
OUTPUT = ROOT / "output" / "prompt-evals"


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline script-prompt fixture evaluator")
    parser.add_argument("mode", choices=("validate", "generate", "compare"), nargs="?", default="validate")
    parser.add_argument("--output", type=Path, default=OUTPUT / "validation.json")
    args = parser.parse_args()
    if args.mode != "validate":
        parser.error(
            "generate and compare require an explicitly configured offline runner; this tool never calls an LLM"
        )
    results = []
    for content_type, filename in (("ai_briefing", "ai_cases.json"), ("finance", "finance_cases.json")):
        for case in json.loads((FIXTURES / filename).read_text(encoding="utf-8")):
            report = validate_script(
                case["script"], build_structured_facts(content_type, case["material"]), gate="strict"
            )
            issue_codes = [item.code for item in report.issues]
            expected = case.get("expected_issue_codes", [])
            results.append(
                {
                    "content_type": content_type,
                    "name": case["name"],
                    "passed": issue_codes == expected,
                    "issue_codes": issue_codes,
                    "expected_issue_codes": expected,
                }
            )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps({"mode": "validate", "results": results}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({"cases": len(results), "passed": sum(item["passed"] for item in results)}, ensure_ascii=False))
    return 0 if all(item["passed"] for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
