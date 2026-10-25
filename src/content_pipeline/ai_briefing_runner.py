from __future__ import annotations

import argparse
import json
import sys

from pydantic import ValidationError

from .job_store import JobStore
from .models import PublishTarget, TaskInput
from .orchestrator import Orchestrator
from .settings import load_settings
from .task_validation import validate_task_params

__all__ = ["main"]


def _handoff_wait_seconds(value: str) -> int:
    parsed = int(value)
    if parsed < 0 or parsed > 3600:
        raise argparse.ArgumentTypeError("must be between 0 and 3600 seconds")
    return parsed


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
        sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
    except (AttributeError, OSError):
        pass
    parser = argparse.ArgumentParser(description="Run the daily AI briefing video pipeline")
    parser.add_argument("--date", default="auto")
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--skip-publish", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force-mpt", "--force-regenerate", dest="force_regenerate", action="store_true")
    parser.add_argument("--force-publish", action="store_true")
    parser.add_argument("--handoff-wait-seconds", type=_handoff_wait_seconds, default=600)
    args = parser.parse_args()
    if args.publish and args.skip_publish:
        parser.error("--publish and --skip-publish cannot be used together")
    if args.force_publish:
        parser.error("--force-publish is disabled; publish-history deduplication cannot be bypassed")

    publish = args.publish and not args.skip_publish
    targets = (
        [
            PublishTarget(platform="douyin", account="硅基思维TARS"),
            PublishTarget(platform="kuaishou", account="硅基思维TARS"),
        ]
        if publish
        else []
    )
    task = TaskInput(
        description="生成每日 AI 资讯视频",
        content_type="ai_briefing",
        publish=publish,
        publish_targets=targets,
        params={
            "date": args.date,
            "script_mode": "90_seconds",
            "handoff_wait_seconds": args.handoff_wait_seconds,
            "dry_run": args.dry_run,
            "force_regenerate": args.force_regenerate,
        },
    )
    try:
        task = validate_task_params(task)
    except (ValidationError, ValueError) as exc:
        raise SystemExit(f"invalid ai briefing task: {exc}") from exc

    settings = load_settings()
    orchestrator = Orchestrator(settings=settings, store=JobStore(settings.data_dir))
    task_id = orchestrator.submit(task)
    orchestrator.run(task_id)
    result = orchestrator.store.get(task_id).model_dump(mode="json")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] in {"succeeded", "partial"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
