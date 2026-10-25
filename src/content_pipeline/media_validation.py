from __future__ import annotations

from pathlib import Path

import av
from PIL import Image, UnidentifiedImageError

from .errors import MediaValidationError
from .models import ImageFileValidation, ImageValidation, VideoValidation

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}

__all__ = ["validate_images", "validate_video"]


def validate_images(paths: list[Path], expected_count: int) -> ImageValidation:
    if len(paths) != expected_count:
        raise MediaValidationError(f"expected {expected_count} images, got {len(paths)}")
    if len({path.resolve() for path in paths}) != len(paths):
        raise MediaValidationError("generated image paths must be unique")

    files: list[ImageFileValidation] = []
    for path in paths:
        if not path.is_file():
            raise MediaValidationError(f"generated image does not exist: {path}")
        if path.suffix.lower() not in IMAGE_SUFFIXES:
            raise MediaValidationError(f"unsupported generated image format: {path}")
        try:
            with Image.open(path) as image:
                image.verify()
            with Image.open(path) as image:
                width, height = image.size
                image_format = image.format or path.suffix.lstrip(".")
        except (OSError, UnidentifiedImageError) as exc:
            raise MediaValidationError(f"generated image is not decodable: {path}: {exc}") from exc
        if width <= 0 or height <= 0:
            raise MediaValidationError(f"generated image has invalid dimensions: {path}")
        files.append(
            ImageFileValidation(
                path=path,
                format=image_format.upper(),
                width=width,
                height=height,
            )
        )
    return ImageValidation(count=len(files), files=files)


def validate_video(path: Path, expected_aspect: str, require_audio: bool, allowed_duration: tuple[float, float] | None = None) -> VideoValidation:
    if not path.is_file() or path.stat().st_size == 0:
        raise MediaValidationError(f"generated video is missing or empty: {path}")

    try:
        with av.open(str(path)) as container:
            video_stream = next(iter(container.streams.video), None)
            audio_stream = next(iter(container.streams.audio), None)
            if video_stream is None:
                raise MediaValidationError(f"generated video has no video stream: {path}")
            width = int(video_stream.codec_context.width or 0)
            height = int(video_stream.codec_context.height or 0)
            video_codec = video_stream.codec_context.name or "unknown"
            audio_codec = audio_stream.codec_context.name if audio_stream else None
            duration = _duration_seconds(container.duration, video_stream.duration, video_stream.time_base)

        with av.open(str(path)) as container:
            decoded_video_frames = sum(1 for _ in container.decode(video=0))
        decoded_audio_frames = 0
        if audio_stream is not None:
            with av.open(str(path)) as container:
                decoded_audio_frames = sum(1 for _ in container.decode(audio=0))
    except MediaValidationError:
        raise
    except Exception as exc:
        raise MediaValidationError(f"generated video is not decodable: {path}: {exc}") from exc

    if duration <= 0:
        raise MediaValidationError(f"generated video has invalid duration: {path}")
    if width <= 0 or height <= 0 or decoded_video_frames == 0:
        raise MediaValidationError(f"generated video has no decodable frames: {path}")
    if require_audio and (audio_codec is None or decoded_audio_frames == 0):
        raise MediaValidationError(f"generated video requires a decodable audio track: {path}")

    actual_aspect = width / height
    target_aspect = _parse_aspect(expected_aspect)
    if abs(actual_aspect - target_aspect) > 0.02:
        raise MediaValidationError(f"generated video aspect ratio {width}:{height} does not match {expected_aspect}")
    return VideoValidation(
        duration_seconds=round(duration, 3),
        width=width,
        height=height,
        aspect_ratio=round(actual_aspect, 4),
        video_codec=video_codec,
        audio_codec=audio_codec,
        decoded_video_frames=decoded_video_frames,
        decoded_audio_frames=decoded_audio_frames,
    )


def _duration_seconds(container_duration: int | None, stream_duration: int | None, time_base: object) -> float:
    if container_duration:
        return float(container_duration / av.time_base)
    if stream_duration is not None and time_base is not None:
        return float(stream_duration * time_base)
    return 0.0


def _parse_aspect(value: str) -> float:
    try:
        width, height = (float(part) for part in value.split(":", 1))
        if width <= 0 or height <= 0:
            raise ValueError
        return width / height
    except (TypeError, ValueError) as exc:
        raise MediaValidationError(f"invalid expected video aspect ratio: {value}") from exc
