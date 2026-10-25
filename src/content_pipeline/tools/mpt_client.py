from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from content_pipeline.errors import ExternalToolError
from content_pipeline.profiles import VideoGenProfile
from content_pipeline.rendering import render_template
from content_pipeline.settings import Settings
from content_pipeline.tools.common import _mpt_temporary_environment, mpt_task_id, run_command

__all__ = ["call_mpt"]


def call_mpt(
    *,
    task_id: str,
    profile: VideoGenProfile,
    topic: str,
    params: dict[str, Any],
    images: list[Path],
    output_dir: Path,
    settings: Settings,
    custom_audio_file: Path | None = None,
    video_clip_duration: int | None = None,
    video_concat_mode: str | None = None,
    disable_bgm: bool = False,
    subtitle_enabled: bool | None = None,
    allow_cross_post: bool = False,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(output_dir.glob("*.mp4"))
    if existing:
        return existing[0]

    if params.get("dry_run"):
        video = output_dir / "dry-run.mp4"
        video.write_bytes(b"DRY RUN MP4 PLACEHOLDER")
        return video

    script = render_template(profile.script_template, topic, params)
    subject = render_template(profile.subject_template, topic, params)
    local_materials = _stage_local_materials(task_id, images, settings.mpt_dir)
    command = [
        str(settings.mpt_python),
        "cli.py",
        f"--video-subject={subject}",
        f"--video-script={script}",
        "--video-source",
        "local",
        "--video-materials",
        ",".join(str(path) for path in local_materials),
        "--video-count",
        "1",
        "--video-aspect",
        profile.aspect,
        "--task-id",
        mpt_task_id(f"ai-pipeline-{task_id}"),
    ]
    if custom_audio_file:
        command.extend(["--custom-audio-file", str(custom_audio_file.resolve())])
    if video_clip_duration is not None:
        command.extend(["--video-clip-duration", str(video_clip_duration)])
    if video_concat_mode:
        command.extend(["--video-concat-mode", video_concat_mode])
    if disable_bgm:
        command.extend(["--bgm-type", "none"])
    if profile.voice_name and not custom_audio_file:
        command.extend(["--voice-name", profile.voice_name])
    subtitles = profile.subtitle_enabled if subtitle_enabled is None else subtitle_enabled
    if not subtitles:
        command.append("--no-subtitle-enabled")
    if allow_cross_post:
        command.append("--allow-cross-post")

    with _mpt_temporary_environment() as env:
        result = run_command(command, cwd=settings.mpt_dir, timeout=1800, retries=2, env=env)
    video = _extract_video_path(result.stdout, settings.mpt_dir)
    if not video:
        raise ExternalToolError("MPT completed but no mp4 path was found in output")
    video = _guard_generated_video(video, settings.mpt_dir)
    target = output_dir / video.name
    if video.resolve() != target.resolve():
        target.write_bytes(video.read_bytes())
    return target


def _stage_local_materials(task_id: str, images: list[Path], mpt_dir: Path) -> list[Path]:
    staging_dir = (mpt_dir / "storage" / "local_videos" / f"ai-pipeline-{task_id}").resolve()
    staging_dir.mkdir(parents=True, exist_ok=True)
    staged: list[Path] = []
    for index, image in enumerate(images, start=1):
        source = image.resolve()
        target = staging_dir / f"{index:02d}{source.suffix.lower()}"
        if not target.is_file() or target.stat().st_size != source.stat().st_size:
            shutil.copy2(source, target)
        staged.append(target)
    return staged


def _guard_generated_video(video: Path, mpt_dir: Path) -> Path:
    resolved_video = video.resolve()
    resolved_mpt_dir = mpt_dir.resolve()
    try:
        resolved_video.relative_to(resolved_mpt_dir)
    except ValueError as exc:
        raise ExternalToolError(f"MPT reported a video outside mpt_dir: {video}") from exc
    if video.is_symlink() or not resolved_video.is_file() or resolved_video.suffix.lower() != ".mp4":
        raise ExternalToolError(f"MPT reported an invalid video path: {video}")
    return resolved_video


def _extract_video_path(stdout: str, base_dir: Path) -> Path | None:
    candidates: list[Path] = []
    try:
        payload = json.loads(stdout.strip().splitlines()[-1])
        candidates.extend(_walk_for_mp4(payload))
    except Exception:
        pass
    for line in stdout.splitlines():
        if ".mp4" in line.lower():
            token = line.strip().strip('"')
            candidates.append(Path(token))
    for candidate in candidates:
        path = candidate if candidate.is_absolute() else base_dir / candidate
        if path.is_file() and path.suffix.lower() == ".mp4":
            return path
    return None


def _walk_for_mp4(value: Any) -> list[Path]:
    if isinstance(value, str) and value.lower().endswith(".mp4"):
        return [Path(value)]
    if isinstance(value, list):
        paths: list[Path] = []
        for item in value:
            paths.extend(_walk_for_mp4(item))
        return paths
    if isinstance(value, dict):
        paths: list[Path] = []
        for item in value.values():
            paths.extend(_walk_for_mp4(item))
        return paths
    return []
