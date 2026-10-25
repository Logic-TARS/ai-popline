"""Unified in-pipeline publishing for generated videos.

Pipelines generate the artifact; this module owns the publish loop so each
pipeline does not carry its own copy. The publish decision itself comes from
``publish_policy.resolve_publish_plan`` — pipelines pass the resolved values in.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from content_pipeline.errors import ConfigError, PrivateVisibilityUnsupportedError
from content_pipeline.job_store import JobStore
from content_pipeline.models import ArtifactSet, JobSnapshot, JobStatus, PipelineStep, PublishTarget
from content_pipeline.settings import Settings
from content_pipeline.tools.sau_client import call_sau_target

__all__ = ["DEFAULT_SUPPORTED_PLATFORMS", "publish_generated_video", "publish_grouped_videos"]

DEFAULT_SUPPORTED_PLATFORMS = frozenset({"douyin", "kuaishou"})


def publish_generated_video(
    *,
    snapshot: JobSnapshot,
    store: JobStore,
    artifacts: ArtifactSet,
    settings: Settings,
    video: Path,
    title: str,
    description: str,
    tags: list[str],
    requested: bool,
    targets: Iterable[PublishTarget],
    dry_run: bool,
    supported_platforms: Iterable[str] | None = None,
    on_target_finished: Callable[[PublishTarget, dict[str, Any]], None] | None = None,
) -> tuple[JobStatus, str | None]:
    """Publish one generated video to the resolved targets. Returns (final status, error)."""
    task_id = snapshot.task_id
    if not requested:
        artifacts.upload_result = {
            **(artifacts.upload_result or {}),
            "skipped": True,
            "reason": "publish_not_requested",
        }
        store.set_artifacts(task_id, artifacts)
        store.event(task_id, "upload_skipped", {"reason": "publish_not_requested"})
        return JobStatus.SUCCEEDED, None

    supported = set(supported_platforms or DEFAULT_SUPPORTED_PLATFORMS)
    store.mark_running(task_id, PipelineStep.UPLOAD)
    for target in targets:
        if target.platform not in supported:
            result = {
                "success": False,
                "error": f"publishing to {target.platform} is not supported for this pipeline",
            }
        else:
            try:
                result = call_sau_target(
                    target=target,
                    video=video,
                    title=title,
                    desc=description,
                    tags=tags,
                    settings=settings,
                    dry_run=dry_run,
                )
            except Exception as exc:
                result = {"success": False, "error": str(exc)}
        artifacts.publish_results[target.platform] = result
        store.set_artifacts(task_id, artifacts)
        store.event(task_id, "publish_target_finished", {"platform": target.platform, "result": result})
        if on_target_finished is not None:
            on_target_finished(target, result)

    delivery_states = _delivery_states(artifacts.publish_results)
    states = set(delivery_states.values())
    artifacts.upload_result = {
        **(artifacts.upload_result or {}),
        "targets": artifacts.publish_results,
        "delivery_states": delivery_states,
        "visibility": next(iter(states)) if len(states) == 1 else "mixed",
    }
    store.set_artifacts(task_id, artifacts)

    failures = [platform for platform, result in artifacts.publish_results.items() if not result.get("success")]
    if failures:
        return JobStatus.PARTIAL, "publish_failed: " + ", ".join(failures)
    return JobStatus.SUCCEEDED, None


def publish_grouped_videos(
    *,
    snapshot: JobSnapshot,
    store: JobStore,
    artifacts: ArtifactSet,
    settings: Settings,
    groups: list[Any],
    requested: bool,
    targets: Iterable[PublishTarget],
    dry_run: bool,
    title_for: Callable[[int, int], str],
    description: str,
    tags: list[str],
) -> None:
    """Publish every valid group video to the resolved targets (AI art / grouped anime shape)."""
    task_id = snapshot.task_id
    if not requested:
        artifacts.upload_result = {"skipped": True, "reason": "publish_not_requested"}
        store.set_artifacts(task_id, artifacts)
        store.event(task_id, "upload_skipped", {"reason": "publish_not_requested"})
        return

    targets = list(targets)
    if not targets:
        raise ConfigError("publish=true requires at least one publish target or a configured publish policy")
    store.mark_running(task_id, PipelineStep.UPLOAD)
    total_groups = len(groups)
    for published_index, group in enumerate(groups, start=1):
        title = title_for(published_index, total_groups)
        for target in targets:
            key = f"{target.platform}:{target.account}"
            try:
                result = call_sau_target(
                    target=target,
                    video=Path(group.video).resolve(),
                    title=title,
                    desc=description,
                    tags=tags,
                    settings=settings,
                    dry_run=dry_run,
                )
                group.publish_results[key] = {"status": "succeeded", **result}
            except PrivateVisibilityUnsupportedError as exc:
                group.publish_results[key] = {"status": "blocked", "error": str(exc)}
            except Exception as exc:
                group.publish_results[key] = {"status": "failed", "error": str(exc)}
            store.set_artifacts(task_id, artifacts)

    delivery_states = {
        f"{group.index}:{target_key}": (
            result.get("delivery_status")
            or result.get("visibility")
            or ("failed" if result.get("status") != "succeeded" else "unknown")
        )
        for group in groups
        for target_key, result in group.publish_results.items()
    }
    states = set(delivery_states.values())
    artifacts.upload_result = {
        "groups": {str(group.index): group.publish_results for group in groups},
        "delivery_states": delivery_states,
        "visibility": next(iter(states)) if len(states) == 1 else "mixed",
    }


def _delivery_states(results: dict[str, dict[str, Any]]) -> dict[str, str]:
    return {
        platform: (
            result.get("delivery_status")
            or result.get("visibility")
            or ("failed" if not result.get("success") else "unknown")
        )
        for platform, result in results.items()
    }
