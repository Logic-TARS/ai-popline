from __future__ import annotations

import re

from content_pipeline.errors import ConfigError
from content_pipeline.media_validation import validate_video
from content_pipeline.models import PipelineStep, PublishTarget, ScriptVideoParams
from content_pipeline.pipelines.registry import PipelineContext, PipelineMeta, register
from content_pipeline.publish_policy import resolve_publish_plan
from content_pipeline.publishing import publish_generated_video
from content_pipeline.tools.narrated_mpt_client import (
    call_narrated_mpt,
    looks_like_file_reference,
    validate_spoken_subtitle,
)

__all__ = ["run_script_video_pipeline"]


@register(
    "script_video",
    meta=PipelineMeta(
        content_type="script_video",
        description="Reviewed narration script -> narrated 9:16 MPT video -> optional private upload",
        required_params=["title", "script"],
        external_tools=["MoneyPrinterTurbo", "social-auto-upload"],
        publish_targets=["douyin", "kuaishou"],
    ),
)
def run_script_video_pipeline(ctx: PipelineContext) -> None:
    params = ScriptVideoParams.model_validate(ctx.route.params)
    script = params.script.strip()
    if looks_like_file_reference(script) or re.search(r"(?:[A-Za-z]:[\\/]|/home/).+\.(?:md|txt|json)", script):
        raise ConfigError("script video narration cannot contain a local file reference")

    job_dir = ctx.store.job_dir(ctx.task_id)
    ctx.store.mark_running(ctx.task_id, PipelineStep.SOURCE_SCAN)
    ctx.store.set_progress(
        ctx.task_id,
        percent=5,
        phase="保存口播稿",
        message="正在保存已审核的口播稿",
    )
    script_path = job_dir / "narration_script.txt"
    script_path.write_text(script, encoding="utf-8")
    ctx.artifacts.narration_script = script
    ctx.artifacts.script_path = script_path
    ctx.store.set_artifacts(ctx.task_id, ctx.artifacts)
    ctx.store.set_progress(
        ctx.task_id,
        percent=10,
        phase="保存口播稿",
        message="口播稿已保存，准备生成视频",
    )

    ctx.store.mark_running(ctx.task_id, PipelineStep.VIDEO)

    def report_progress(percent: int, phase: str, message: str, is_estimate: bool) -> None:
        ctx.store.set_progress(
            ctx.task_id,
            percent=percent,
            phase=phase,
            message=message,
            is_estimate=is_estimate,
        )

    result = call_narrated_mpt(
        task_name=f"content-{ctx.task_id}",
        title=params.title,
        script=script,
        output_dir=job_dir / "video",
        settings=ctx.settings,
        dry_run=params.dry_run,
        force_regenerate=params.force_regenerate,
        voice_rate=params.voice_rate,
        on_progress=report_progress,
    )
    ctx.artifacts.video = result.video
    ctx.artifacts.subtitle = result.subtitle
    ctx.artifacts.mpt_task_dir = result.task_dir
    ctx.artifacts.manifest_path = result.manifest
    ctx.store.set_progress(
        ctx.task_id,
        percent=90,
        phase="媒体校验",
        message="正在校验字幕、视频比例和音频轨道",
    )
    validate_spoken_subtitle(result.subtitle)
    if not params.dry_run:
        ctx.artifacts.validation.video = validate_video(result.video, expected_aspect="9:16", require_audio=True)
    plan = resolve_publish_plan("script_video", ctx.snapshot.task, settings=ctx.settings)
    if plan["requested"]:
        ctx.store.set_progress(ctx.task_id, percent=95, phase="发布", message="正在提交发布任务")
    default_targets = [
        PublishTarget(platform="douyin", account=params.douyin_account),
        PublishTarget(platform="kuaishou", account=params.kuaishou_account),
    ]
    final, error = publish_generated_video(
        snapshot=ctx.snapshot,
        store=ctx.store,
        artifacts=ctx.artifacts,
        settings=ctx.settings,
        video=result.video.resolve(),
        title=params.title,
        description=params.description,
        tags=params.tags,
        requested=plan["requested"],
        targets=plan["targets"] or default_targets,
        dry_run=params.dry_run,
    )
    if not plan["requested"]:
        ctx.store.set_progress(ctx.task_id, percent=95, phase="跳过发布", message="仅本地生成，已跳过发布")
        ctx.store.set_progress(ctx.task_id, percent=100, phase="完成", message="口播视频已生成并通过校验")
    elif error:
        ctx.store.set_progress(ctx.task_id, percent=100, phase="完成", message="视频已生成，部分发布目标失败")
    else:
        ctx.store.set_progress(ctx.task_id, percent=100, phase="完成", message="视频已生成并发布完成")
    ctx.store.finish(ctx.task_id, final, error)
