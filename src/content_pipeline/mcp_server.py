from __future__ import annotations

import json
import logging
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from uuid import uuid4

from mcp.server import FastMCP

from .api.ui_schema import validate_task_for_ui
from .models import JobSnapshot, JobStatus, PublishTarget, TaskInput
from .orchestrator import Orchestrator
from .photo_process_debug import open_photo_process_debug as open_photo_process_debug_browser
from .profiles import load_profile
from .settings import load_settings
from .tools.gemini_client import call_gemini_skill
from .tools.mpt_client import call_mpt
from .tools.photo_process_client import photo_process_contract

__all__ = [
    "generate_images",
    "get_external_tool_contracts",
    "get_job_events",
    "get_status",
    "list_capabilities",
    "list_jobs",
    "list_profiles",
    "main",
    "open_photo_process_debug",
    "process_ai_art",
    "process_ai_art_async",
    "process_japanese_images",
    "render_video",
    "run_finance_video_async",
    "run_task_async",
    "run_task_sync",
    "start_task",
    "submit_task",
]

mcp = FastMCP("ai-pipeline")

settings = load_settings()
orchestrator = Orchestrator(settings=settings)
executor = ThreadPoolExecutor(max_workers=2)


def _publish_targets(publish_targets: list[dict[str, Any]] | None) -> list[PublishTarget]:
    return [PublishTarget(**target) for target in (publish_targets or [])]


def _make_task(
    *,
    description: str,
    content_type: str | None = None,
    topic: str | None = None,
    publish: bool = False,
    publish_targets: list[dict[str, Any]] | None = None,
    params: dict[str, Any] | None = None,
) -> TaskInput:
    return TaskInput(
        description=description,
        content_type=content_type,
        topic=topic,
        publish=publish,
        publish_targets=_publish_targets(publish_targets),
        params=params or {},
    )


def _normalize_task(task: TaskInput) -> TaskInput:
    if task.content_type is None:
        return task
    normalized, _warnings = validate_task_for_ui(task)
    return normalized


def _submit_and_start(task: TaskInput) -> dict[str, str]:
    task_id = orchestrator.submit(_normalize_task(task))
    executor.submit(orchestrator.run, task_id)
    return {"task_id": task_id, "status": "queued"}


def _bounded_limit(value: int, *, maximum: int) -> int:
    if value < 1:
        raise ValueError("limit must be a positive integer")
    return min(value, maximum)


@mcp.tool()
async def submit_task(
    description: str,
    content_type: str | None = None,
    topic: str | None = None,
    publish: bool = False,
    publish_targets: list[dict[str, Any]] | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Create a queued content pipeline task without starting execution."""
    task = _make_task(
        description=description,
        content_type=content_type,
        topic=topic,
        publish=publish,
        publish_targets=publish_targets,
        params=params or {},
    )
    task_id = orchestrator.submit(_normalize_task(task))
    return {"task_id": task_id, "status": "queued"}


@mcp.tool()
async def run_task_async(
    description: str,
    content_type: str | None = None,
    topic: str | None = None,
    publish: bool = False,
    publish_targets: list[dict[str, Any]] | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Submit and start a long-running task; poll get_status with the returned task_id."""
    task = _make_task(
        description=description,
        content_type=content_type,
        topic=topic,
        publish=publish,
        publish_targets=publish_targets,
        params=params or {},
    )
    return _submit_and_start(task)


@mcp.tool()
async def start_task(task_id: str) -> dict[str, str]:
    """Start a queued task by id; use get_status to observe progress."""
    snapshot = orchestrator.store.get(task_id)
    if snapshot.status != JobStatus.QUEUED:
        return {"task_id": task_id, "status": snapshot.status.value}
    executor.submit(orchestrator.run, task_id)
    return {"task_id": task_id, "status": "queued"}


@mcp.tool()
async def get_status(task_id: str) -> dict[str, Any]:
    """Get the status of a content pipeline task."""
    try:
        return orchestrator.store.get(task_id).model_dump(mode="json")
    except FileNotFoundError as exc:
        raise ValueError("task not found") from exc


@mcp.tool()
async def get_job_events(task_id: str, limit: int = 50) -> dict[str, Any]:
    """Return recent append-only events for a task."""
    resolved_limit = _bounded_limit(limit, maximum=500)
    try:
        events = orchestrator.store.read_events(task_id, limit=resolved_limit)
    except FileNotFoundError as exc:
        raise ValueError("task not found") from exc
    except OSError as exc:
        raise ValueError("job events cannot be read") from exc
    except json.JSONDecodeError as exc:
        raise ValueError("job events are invalid") from exc
    return {"task_id": task_id, "events": events}


@mcp.tool()
async def list_capabilities() -> dict[str, Any]:
    """List agent-facing workflows, required parameters, and safety defaults."""
    return {
        "workflows": [
            {
                "name": "generic_content_task",
                "tools": ["run_task_async", "run_task_sync", "submit_task", "start_task"],
                "content_types": [
                    "anime",
                    "finance",
                    "ai_briefing",
                    "ai_art",
                    "grouped_anime",
                    "japanese",
                    "script_video",
                ],
                "default_publish": False,
                "notes": "Use run_task_async for long browser, video, or publishing jobs.",
            },
            {
                "name": "photo_process_image_folder",
                "tools": ["process_ai_art_async", "process_ai_art", "process_japanese_images"],
                "lower_tool": photo_process_contract(settings),
                "required_params": ["source_dir", "process_name"],
                "safety": "Stage or copy source images before calling Photo-Process when originals must be preserved.",
            },
            {
                "name": "finance_video",
                "tools": ["run_finance_video_async"],
                "content_type": "finance",
                "default_publish": False,
                "publishing": ("Douyin/Kuaishou publishing with selectable visibility is supported for Finance."),
            },
        ],
        "status_tools": ["get_status", "get_job_events", "list_jobs"],
        "diagnostic_tools": ["open_photo_process_debug"],
    }


@mcp.tool()
async def get_external_tool_contracts() -> dict[str, Any]:
    """Return stable adapter contracts for lower-level local tools."""
    return {"tools": [photo_process_contract(settings)]}


@mcp.tool()
async def open_photo_process_debug(
    url: str | None = None,
    mode: str = "automation",
    desktop_helper_url: str = "http://127.0.0.1:8767",
) -> dict[str, Any]:
    """Open Photo-Process' own foreground Gemini debug browser for human inspection."""
    return open_photo_process_debug_browser(
        settings=settings,
        url=url,
        mode=mode,  # type: ignore[arg-type]
        desktop_helper_url=desktop_helper_url,
    ).model_dump(mode="json")


@mcp.tool()
async def run_task_sync(
    description: str,
    content_type: str | None = None,
    topic: str | None = None,
    publish: bool = False,
    publish_targets: list[dict[str, Any]] | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Submit and run a content pipeline task synchronously, returning the final snapshot."""
    try:
        task = _make_task(
            description=description,
            content_type=content_type,
            topic=topic,
            publish=publish,
            publish_targets=publish_targets,
            params=params or {},
        )
        task_id = orchestrator.submit(_normalize_task(task))
        orchestrator.run(task_id)
        return orchestrator.store.get(task_id).model_dump(mode="json")
    except Exception as exc:
        return {"status": "failed", "error": str(exc)}


@mcp.tool()
async def generate_images(
    prompt: str,
    content_type: str,
    count: int = 4,
    output_dir: str | None = None,
    params: dict[str, Any] | None = None,
) -> list[str]:
    """Generate images using the Gemini skill for a given content type."""
    profile = load_profile(content_type, settings.profiles_dir)
    profile.image_gen.count = count
    out = Path(output_dir) if output_dir else settings.data_dir / "mcp_images" / prompt[:32]
    images = call_gemini_skill(
        profile=profile.image_gen,
        topic=prompt,
        params=params or {},
        output_dir=out,
        settings=settings,
    )
    return [str(p) for p in images]


@mcp.tool()
async def render_video(
    images: list[str],
    content_type: str,
    topic: str,
    params: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Generate a video from images using MoneyPrinterTurbo."""
    try:
        profile = load_profile(content_type, settings.profiles_dir)
        task_id = f"mcp-{uuid4().hex[:8]}"
        output_dir = settings.data_dir / "mcp_videos"
        output_dir.mkdir(parents=True, exist_ok=True)
        video_path = call_mpt(
            task_id=task_id,
            profile=profile.video_gen,
            topic=topic,
            params=params or {"dry_run": True},
            images=[Path(p) for p in images],
            output_dir=output_dir,
            settings=settings,
        )
        return {"video": str(video_path)}
    except Exception as exc:
        return {"status": "failed", "error": str(exc)}


@mcp.tool()
async def process_ai_art(
    source_dir: str,
    process_name: str,
    title: str,
    source_files: list[str] | None = None,
    archive_dir: str | None = None,
    failed_dir: str | None = None,
    group_size: int = 4,
    description: str = "",
    tags: list[str] | None = None,
    publish: bool = False,
    publish_targets: list[dict[str, Any]] | None = None,
    params: dict[str, Any] | None = None,
    image_prompt: str = "",
) -> dict[str, Any]:
    """Process a folder of images through the AI art pipeline: edit, group, build videos, and optionally publish."""
    try:
        task_params = {
            "source_dir": source_dir,
            "process_name": process_name,
            "image_prompt": image_prompt,
            "title": title,
            "source_files": source_files or [],
            "description": description,
            "tags": tags or [],
            "group_size": group_size,
        }
        if archive_dir is not None:
            task_params["archive_dir"] = archive_dir
        if failed_dir is not None:
            task_params["failed_dir"] = failed_dir
        if params:
            task_params.update(params)

        task = TaskInput(
            description=f"AI Art: {title}",
            content_type="ai_art",
            topic=title,
            publish=publish,
            publish_targets=_publish_targets(publish_targets),
            params=task_params,
        )
        task_id = orchestrator.submit(_normalize_task(task))
        orchestrator.run(task_id)
        return orchestrator.store.get(task_id).model_dump(mode="json")
    except Exception as exc:
        return {"status": "failed", "error": str(exc)}


@mcp.tool()
async def process_ai_art_async(
    source_dir: str,
    process_name: str,
    title: str,
    source_files: list[str] | None = None,
    archive_dir: str | None = None,
    failed_dir: str | None = None,
    group_size: int = 4,
    description: str = "",
    tags: list[str] | None = None,
    publish: bool = False,
    publish_targets: list[dict[str, Any]] | None = None,
    params: dict[str, Any] | None = None,
    image_prompt: str = "",
) -> dict[str, str]:
    """Submit and start the AI-art Photo-Process folder pipeline asynchronously."""
    task_params = {
        "source_dir": source_dir,
        "process_name": process_name,
        "image_prompt": image_prompt,
        "title": title,
        "source_files": source_files or [],
        "description": description,
        "tags": tags or [],
        "group_size": group_size,
    }
    if archive_dir is not None:
        task_params["archive_dir"] = archive_dir
    if failed_dir is not None:
        task_params["failed_dir"] = failed_dir
    if params:
        task_params.update(params)

    return _submit_and_start(
        TaskInput(
            description=f"AI Art: {title}",
            content_type="ai_art",
            topic=title,
            publish=publish,
            publish_targets=_publish_targets(publish_targets),
            params=task_params,
        )
    )


@mcp.tool()
async def process_japanese_images(
    source_dir: str,
    process_name: str = "日语视觉化",
    image_prompt: str = "",
    output_dir: str | None = None,
    source_files: list[str] | None = None,
    target_gem_url: str | None = None,
    dry_run: bool = False,
) -> dict[str, str]:
    """Submit and start the Japanese local image pipeline backed by Photo-Process."""
    params: dict[str, Any] = {
        "source_dir": source_dir,
        "process_name": process_name,
        "image_prompt": image_prompt,
        "source_files": source_files or [],
        "dry_run": dry_run,
    }
    if output_dir is not None:
        params["output_dir"] = output_dir
    if target_gem_url is not None:
        params["target_gem_url"] = target_gem_url
    return _submit_and_start(
        TaskInput(
            description="Japanese local image processing",
            content_type="japanese",
            topic="Japanese local image processing",
            publish=False,
            params=params,
        )
    )


@mcp.tool()
async def run_finance_video_async(
    date: str = "auto",
    dry_run: bool = False,
    publish: bool = False,
    title: str | None = None,
) -> dict[str, str]:
    """Submit and start the dedicated Finance Markdown-to-video pipeline."""
    params: dict[str, Any] = {
        "date": date,
        "dry_run": dry_run,
    }
    if title is not None:
        params["title"] = title
    return _submit_and_start(
        TaskInput(
            description="Finance daily video",
            content_type="finance",
            topic="Finance daily video",
            publish=publish,
            params=params,
        )
    )


@mcp.tool()
async def list_profiles() -> list[dict[str, str]]:
    """List all available content profiles."""
    return [{"name": path.stem, "path": str(path)} for path in sorted(settings.profiles_dir.glob("*.yaml"))]


@mcp.tool()
async def list_jobs(limit: int = 10) -> dict[str, Any]:
    """List the most recent job summaries, sorted by creation time descending."""
    try:
        resolved_limit = _bounded_limit(limit, maximum=200)
        jobs_dir = settings.data_dir / "jobs"
        if not jobs_dir.is_dir():
            return {"jobs": []}

        results = []
        for job_dir in jobs_dir.iterdir():
            if not job_dir.is_dir():
                continue
            status_file = job_dir / "status.json"
            if not status_file.exists():
                continue
            raw = status_file.read_text(encoding="utf-8")
            snapshot = JobSnapshot.model_validate_json(raw)
            results.append(
                {
                    "task_id": snapshot.task_id,
                    "status": snapshot.status.value,
                    "description": snapshot.task.description,
                    "created_at": status_file.stat().st_mtime,
                }
            )

        results.sort(key=lambda j: j["created_at"], reverse=True)
        return {"jobs": results[:resolved_limit]}
    except Exception as exc:
        return {"status": "failed", "error": str(exc)}


def main() -> int:
    """Run the MCP server over stdio."""
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    mcp.run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
