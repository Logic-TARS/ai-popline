from __future__ import annotations

from pathlib import Path

from ._pipeline_common import _group_signature, _group_title, _partial_reasons
from .errors import ConfigError
from .media_validation import validate_images, validate_video
from .models import AiArtGroupArtifact, AiArtParams, AiArtSourceResult, JobStatus, PipelineStep
from .pipelines.registry import PipelineContext, PipelineMeta, register
from .publish_policy import resolve_publish_plan
from .publishing import publish_grouped_videos
from .tools.photo_process_client import archive_source, run_photo_process_adapter, scan_source_images
from .tools.slideshow_client import choose_bgm, render_slideshow

__all__ = ["run_ai_art_pipeline"]


@register(
    "ai_art",
    meta=PipelineMeta(
        content_type="ai_art",
        description="Photo-Process folder editing -> FFmpeg slideshow groups -> optional private publish",
        required_params=["source_dir", "process_name", "title"],
        external_tools=["Photo-Process", "ffmpeg"],
        publish_targets=["douyin", "bilibili"],
    ),
)
def run_ai_art_pipeline(ctx: PipelineContext) -> None:
    task_id = ctx.task_id
    snapshot = ctx.snapshot
    artifacts = ctx.artifacts
    store = ctx.store
    settings = ctx.settings
    params = AiArtParams.model_validate(ctx.route.params)
    source_dir = params.source_dir.expanduser().resolve()
    archive_dir = (params.archive_dir or (source_dir / "已处理")).expanduser().resolve()
    failed_dir = params.failed_dir.expanduser().resolve() if params.failed_dir else None
    job_dir = store.job_dir(task_id)

    store.mark_running(task_id, PipelineStep.SOURCE_SCAN)
    if not artifacts.source_results:
        sources = scan_source_images(source_dir, params.source_files)
        if not sources:
            raise ConfigError(f"AI art source directory contains no supported images: {source_dir}")
        artifacts.source_results = [AiArtSourceResult(source_path=path) for path in sources]
        store.set_artifacts(task_id, artifacts)

    store.mark_running(task_id, PipelineStep.IMAGE)
    for index, record in enumerate(artifacts.source_results, start=1):
        try:
            if record.processed_path and record.processed_path.is_file():
                validate_images([record.processed_path], 1)
            else:
                source = _available_source(record)
                adapter_result = run_photo_process_adapter(
                    source=source,
                    output_path=job_dir / "processed" / f"{index:04d}.png",
                    settings=settings,
                    prompt=params.image_prompt,
                    target_gem_name=params.process_name,
                )
                record.adapter_result = adapter_result
                if not adapter_result.ok:
                    raise RuntimeError(adapter_result.message or adapter_result.code.value)
                record.processed_path = Path(str(adapter_result.artifacts["processed_path"]))
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
            if record.processed_path is None and record.failed_path is None and failed_dir:
                source = Path(record.source_path)
                if source.exists():
                    try:
                        record.failed_path = archive_source(source, failed_dir)
                    except Exception as move_exc:
                        record.error = f"{record.error}; failed to move source to failed_dir: {move_exc}"
        store.set_artifacts(task_id, artifacts)

    successful = [
        record for record in artifacts.source_results if record.processed_path and record.processed_path.is_file()
    ]
    if not successful:
        raise ConfigError("Photo-Process did not produce any usable images")
    artifacts.images = [Path(record.processed_path) for record in successful if record.processed_path]
    artifacts.validation.images = validate_images(artifacts.images, len(artifacts.images))

    store.mark_running(task_id, PipelineStep.VIDEO)
    previous_groups = {group.index: group for group in artifacts.groups}
    groups: list[AiArtGroupArtifact] = []
    for start in range(0, len(successful), params.group_size):
        records = successful[start : start + params.group_size]
        group_index = len(groups) + 1
        processed = [Path(record.processed_path) for record in records if record.processed_path]
        previous = previous_groups.get(group_index)
        group = (
            previous
            if previous and [Path(path) for path in previous.processed_images] == processed
            else AiArtGroupArtifact(index=group_index)
        )
        group.source_images = [record.source_path for record in records]
        group.processed_images = processed
        group.bgm = choose_bgm(settings.ai_art_bgm_dir, task_id, group_index)
        try:
            group.video = render_slideshow(
                images=processed,
                bgm=group.bgm,
                output=job_dir / "groups" / f"{group_index:02d}" / f"video-{_group_signature(processed)}.mp4",
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
        raise ConfigError("no AI art video group was generated successfully")
    artifacts.video = valid_groups[0].video
    artifacts.validation.video = valid_groups[0].validation

    plan = resolve_publish_plan("ai_art", snapshot.task, settings=settings)
    publish_grouped_videos(
        snapshot=snapshot,
        store=store,
        artifacts=artifacts,
        settings=settings,
        groups=valid_groups,
        requested=plan["requested"],
        targets=plan["targets"],
        dry_run=bool(snapshot.task.params.get("dry_run")),
        title_for=lambda index, total: _group_title(params.title, index, total),
        description=params.description or snapshot.task.description,
        tags=params.tags,
    )

    store.set_artifacts(task_id, artifacts)
    partial_reasons = _partial_reasons(artifacts, snapshot.task.publish)
    if partial_reasons:
        store.finish(task_id, JobStatus.PARTIAL, "; ".join(partial_reasons))
    else:
        store.finish(task_id, JobStatus.SUCCEEDED)


def _available_source(record: AiArtSourceResult) -> Path:
    for candidate in (record.source_path, record.archived_path, record.failed_path):
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    raise ConfigError(f"source image is unavailable: {record.source_path}")
