from __future__ import annotations

from pathlib import Path

from ._pipeline_common import _group_signature, _group_title, _partial_reasons
from .errors import ConfigError
from .grouping import group_by_prefix
from .media_validation import validate_images, validate_video
from .models import AiArtGroupArtifact, AiArtSourceResult, GroupedAnimeParams, JobStatus, PipelineStep
from .pipelines.registry import PipelineContext, PipelineMeta, register
from .profiles import load_profile
from .publish_policy import resolve_publish_plan
from .publishing import publish_grouped_videos
from .tools.audio_client import prepare_music_track
from .tools.mpt_client import call_mpt
from .tools.photo_process_client import archive_source, scan_source_images
from .tools.slideshow_client import choose_bgm

__all__ = ["MPT_VISUAL_ONLY_PLACEHOLDER", "run_grouped_anime_pipeline"]

MPT_VISUAL_ONLY_PLACEHOLDER = "Grouped anime visual showcase"


@register(
    "grouped_anime",
    meta=PipelineMeta(
        content_type="grouped_anime",
        description="Source images grouped by filename prefix -> per-group MPT video -> optional publish",
        required_params=["source_dir"],
        external_tools=["MoneyPrinterTurbo", "ffmpeg"],
        publish_targets=["douyin", "bilibili"],
    ),
)
def run_grouped_anime_pipeline(ctx: PipelineContext) -> None:
    task_id = ctx.task_id
    snapshot = ctx.snapshot
    artifacts = ctx.artifacts
    store = ctx.store
    settings = ctx.settings
    params = GroupedAnimeParams.model_validate(ctx.route.params)
    source_dir = params.source_dir.expanduser().resolve()
    archive_dir = (params.archive_dir or (source_dir / "已处理")).expanduser().resolve()
    failed_dir = params.failed_dir.expanduser().resolve() if params.failed_dir else None
    job_dir = store.job_dir(task_id)
    anime_profile = load_profile("anime", settings.profiles_dir)

    store.mark_running(task_id, PipelineStep.SOURCE_SCAN)
    if not artifacts.source_results:
        sources = scan_source_images(source_dir, params.source_files)
        if not sources:
            raise ConfigError(f"Grouped anime source directory contains no supported images: {source_dir}")
        artifacts.source_results = [AiArtSourceResult(source_path=path) for path in sources]
        store.set_artifacts(task_id, artifacts)

    # Archive successful source images, move failed ones
    for record in artifacts.source_results:
        try:
            if record.archived_path is None:
                candidates = [Path(record.source_path)]
                if record.failed_path:
                    candidates.append(Path(record.failed_path))
                source = next((candidate for candidate in candidates if candidate.exists()), None)
                if source is not None:
                    record.archived_path = archive_source(source, archive_dir)
                    record.failed_path = None
            record.error = None
        except Exception as exc:
            record.error = str(exc)
            if record.failed_path is None and failed_dir:
                source = Path(record.source_path)
                if source.exists():
                    try:
                        record.failed_path = archive_source(source, failed_dir)
                    except Exception as move_exc:
                        record.error = f"{record.error}; failed to move source to failed_dir: {move_exc}"
        store.set_artifacts(task_id, artifacts)

    successful = [
        record
        for record in artifacts.source_results
        if not record.error and record.archived_path and Path(record.archived_path).is_file()
    ]
    if not successful:
        raise ConfigError("Source scan did not produce any usable images")

    artifacts.images = [Path(record.archived_path) for record in successful]
    artifacts.validation.images = validate_images(artifacts.images, len(artifacts.images))

    # Group archived images by prefix
    store.mark_running(task_id, PipelineStep.IMAGE)
    prefix_groups = group_by_prefix(artifacts.images)
    if not prefix_groups:
        raise ConfigError("No image groups could be formed from source images")

    # Render each prefix group through MoneyPrinterTurbo.
    store.mark_running(task_id, PipelineStep.VIDEO)
    previous_groups = {group.index: group for group in artifacts.groups}
    groups: list[AiArtGroupArtifact] = []

    for group_index, (prefix, group_images) in enumerate(prefix_groups.items(), start=1):
        # Filter to only successful images
        valid_images = [img for img in group_images if img in artifacts.images]
        if not valid_images:
            group = AiArtGroupArtifact(index=group_index, error=f"No valid images for prefix: {prefix}")
            groups.append(group)
            artifacts.groups = groups
            store.set_artifacts(task_id, artifacts)
            continue

        previous = previous_groups.get(group_index)
        group = (
            previous
            if previous and [Path(path) for path in previous.processed_images] == valid_images
            else AiArtGroupArtifact(index=group_index)
        )
        group.source_images = valid_images
        group.processed_images = valid_images
        source_bgm = choose_bgm(settings.ai_art_bgm_dir, task_id, group_index)
        signature = _group_signature(valid_images)
        group_dir = job_dir / "groups" / f"{group_index:02d}"
        mpt_task_id = f"{task_id}-g{group_index:02d}"
        group.mpt_task_dir = (settings.mpt_dir / "storage" / "tasks" / f"ai-pipeline-{mpt_task_id}").resolve()

        try:
            group.bgm = prepare_music_track(
                source=source_bgm,
                output=group_dir / f"music-{signature}-{params.seconds_per_image}s.mp3",
                duration_seconds=len(valid_images) * params.seconds_per_image,
            )
            mpt_params = dict(snapshot.task.params)
            # MPT requires non-empty subject/script values even when custom audio skips TTS.
            # These fixed placeholders are internal only and are never rendered into the video.
            mpt_params["script"] = MPT_VISUAL_ONLY_PLACEHOLDER
            group.video = call_mpt(
                task_id=mpt_task_id,
                profile=anime_profile.video_gen,
                topic=MPT_VISUAL_ONLY_PLACEHOLDER,
                params=mpt_params,
                images=valid_images,
                output_dir=group_dir / f"mpt-{signature}-{params.seconds_per_image}s",
                settings=settings,
                custom_audio_file=group.bgm,
                video_clip_duration=params.seconds_per_image,
                video_concat_mode="sequential",
                disable_bgm=True,
                subtitle_enabled=False,
                allow_cross_post=False,
            )
            group.validation = validate_video(group.video, expected_aspect="9:16", require_audio=True)
            group.error = None
        except Exception as exc:
            group.error = str(exc)
            group.video = None
            group.validation = None

        groups.append(group)
        artifacts.groups = groups
        store.set_artifacts(task_id, artifacts)

    valid_groups = [group for group in groups if group.video and group.validation and not group.error]
    if not valid_groups:
        raise ConfigError("No grouped anime video was generated successfully")
    artifacts.video = valid_groups[0].video
    artifacts.validation.video = valid_groups[0].validation

    # Publish step
    plan = resolve_publish_plan("grouped_anime", snapshot.task, settings=settings)
    publish_title = params.title or snapshot.task.topic or "动漫作品展示"
    publish_grouped_videos(
        snapshot=snapshot,
        store=store,
        artifacts=artifacts,
        settings=settings,
        groups=valid_groups,
        requested=plan["requested"],
        targets=plan["targets"],
        dry_run=bool(snapshot.task.params.get("dry_run")),
        title_for=lambda index, total: _group_title(publish_title, index, total),
        description=params.description or snapshot.task.description,
        tags=params.tags,
    )

    store.set_artifacts(task_id, artifacts)
    partial_reasons = _partial_reasons(artifacts, snapshot.task.publish)
    if partial_reasons:
        store.finish(task_id, JobStatus.PARTIAL, "; ".join(partial_reasons))
    else:
        store.finish(task_id, JobStatus.SUCCEEDED)
