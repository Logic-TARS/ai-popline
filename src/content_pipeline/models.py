from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "AdapterResult",
    "AiArtGroupArtifact",
    "AiArtParams",
    "AiArtSourceResult",
    "AiBriefingParams",
    "ArtifactSet",
    "ContentType",
    "CoverParams",
    "DeferredPublishInput",
    "ErrorCode",
    "FinanceParams",
    "DurationVariant",
    "StrategyProfile",
    "GroupedAnimeParams",
    "ImageFileValidation",
    "ImageValidation",
    "JapaneseParams",
    "JobProgress",
    "JobSnapshot",
    "JobStatus",
    "MediaValidation",
    "MetadataAuditSummary",
    "MetadataStageAuditSummary",
    "PipelineStep",
    "PublicationAttempt",
    "PublicationStatus",
    "PublishTarget",
    "RouteResult",
    "ScriptVideoParams",
    "TaskInput",
    "VideoValidation",
    "XhsImageNoteParams",
    "deferred_publish_fingerprint",
    "task_fingerprint",
]

ContentType = Literal[
    "anime",
    "finance",
    "ai_briefing",
    "ai_art",
    "xhs_image_note",
    "grouped_anime",
    "japanese",
    "script_video",
    "unknown",
]

StrategyProfile = Literal["finance_conversion", "ai_reach"]
DurationVariant = Literal["legacy_90_seconds", "short_30_60", "long_1_3_minutes"]


def _normalize_required_text(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} cannot be blank")
    return normalized


def _normalize_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    return value.strip() or None


def _normalize_tags(values: list[str], *, error_prefix: str = "tags") -> list[str]:
    normalized_tags: list[str] = []
    for value in values:
        tag = value.strip().lstrip("#")
        if not tag:
            continue
        if len(tag) > 30:
            raise ValueError(f"{error_prefix} cannot exceed 30 characters")
        if tag not in normalized_tags:
            normalized_tags.append(tag)
    return normalized_tags


def _normalize_source_files(values: list[str]) -> list[str]:
    normalized_files: list[str] = []
    for value in values:
        filename = value.strip()
        if not filename:
            continue
        if Path(filename).name != filename:
            raise ValueError("source_files entries must be plain filenames")
        if filename not in normalized_files:
            normalized_files.append(filename)
    return normalized_files


class PipelineStep(StrEnum):
    ROUTE = "route"
    SOURCE_SCAN = "source_scan"
    IMAGE = "image"
    VIDEO = "video"
    ARCHIVE = "archive"
    UPLOAD = "upload"
    COMPLETE = "complete"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    PARTIAL = "partial"


class ErrorCode(StrEnum):
    OK = "OK"
    CONFIG_ERROR = "CONFIG_ERROR"
    INPUT_ERROR = "INPUT_ERROR"
    SOURCE_NOT_FOUND = "SOURCE_NOT_FOUND"
    SOURCE_SCAN_FAILED = "SOURCE_SCAN_FAILED"
    EXTERNAL_TOOL_FAILED = "EXTERNAL_TOOL_FAILED"
    NO_OUTPUT = "NO_OUTPUT"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    PRIVATE_VISIBILITY_UNSUPPORTED = "PRIVATE_VISIBILITY_UNSUPPORTED"
    PUBLISH_FAILED = "PUBLISH_FAILED"
    ALREADY_PUBLISHED = "ALREADY_PUBLISHED"
    DESKTOP_HELPER_UNAVAILABLE = "DESKTOP_HELPER_UNAVAILABLE"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    BROWSER_BUSY = "BROWSER_BUSY"
    GEM_ACCESS_FAILED = "GEM_ACCESS_FAILED"
    UI_CHANGED = "UI_CHANGED"
    TIMEOUT = "TIMEOUT"


class AdapterResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    tool: str = Field(min_length=1, max_length=120)
    code: ErrorCode
    message: str | None = Field(default=None, max_length=2000)
    artifacts: dict[str, Any] = Field(default_factory=dict)
    evidence: dict[str, Any] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict)


class PublishTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # "tencent" is retained only so historical job snapshots still validate.
    platform: Literal["douyin", "kuaishou", "bilibili", "tencent", "xiaohongshu"]
    account: str = Field(min_length=1, max_length=120)
    tid: int | None = Field(default=None, ge=1)
    visibility: Literal["private", "public"] = "private"

    @field_validator("account")
    @classmethod
    def normalize_account(cls, value: str) -> str:
        return _normalize_required_text(value, "account")

    @model_validator(mode="after")
    def require_bilibili_tid(self) -> PublishTarget:
        if self.platform == "bilibili" and self.tid is None:
            raise ValueError("tid is required for a Bilibili publish target")
        return self


class TaskInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=1, max_length=500)
    content_type: ContentType | None = None
    topic: str | None = Field(default=None, max_length=200)
    publish: bool | None = None
    publish_targets: list[PublishTarget] = Field(default_factory=list, max_length=4)
    params: dict[str, Any] = Field(default_factory=dict)
    origin: Literal["content_studio"] | None = None
    source_draft_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    source_draft_revision: int | None = Field(default=None, ge=1)

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value: str) -> str:
        return _normalize_required_text(value, "description")

    @field_validator("topic")
    @classmethod
    def normalize_topic(cls, value: str | None) -> str | None:
        return _normalize_optional_text(value)

    @model_validator(mode="after")
    def validate_task_consistency(self) -> TaskInput:
        platforms = [target.platform for target in self.publish_targets]
        if len(platforms) != len(set(platforms)):
            raise ValueError("each publish platform can appear only once")
        has_draft_reference = self.source_draft_id is not None or self.source_draft_revision is not None
        if has_draft_reference and self.origin != "content_studio":
            raise ValueError("source draft fields require origin='content_studio'")
        if self.origin == "content_studio" and (self.source_draft_id is None) != (self.source_draft_revision is None):
            raise ValueError("source_draft_id and source_draft_revision must be provided together")
        return self


class DeferredPublishInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    publish_targets: list[PublishTarget] = Field(min_length=1, max_length=4)
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    tags: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def normalize_publish_fields(self) -> DeferredPublishInput:
        platforms = [target.platform for target in self.publish_targets]
        if len(platforms) != len(set(platforms)):
            raise ValueError("each publish platform can appear only once")
        self.title = _normalize_required_text(self.title, "publish title")
        self.description = self.description.strip()
        self.tags = _normalize_tags(self.tags, error_prefix="publish tags")
        return self


PublicationStatus = Literal["queued", "running", "succeeded", "partial", "failed"]


class PublicationAttempt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attempt_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    status: PublicationStatus = "queued"
    request: DeferredPublishInput
    video_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    requested_at: str = Field(max_length=80)
    started_at: str | None = Field(default=None, max_length=80)
    finished_at: str | None = Field(default=None, max_length=80)
    results: dict[str, dict[str, Any]] = Field(default_factory=dict)
    error: str | None = Field(default=None, max_length=2000)


class AiArtParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_dir: Path
    source_files: list[str] = Field(default_factory=list)
    archive_dir: Path | None = None
    failed_dir: Path | None = None
    process_name: str = Field(min_length=1)
    # Photo-Process owns the real prompt for each process name. This optional
    # field remains only for legacy callers that still send extra text.
    image_prompt: str = ""
    title: str = Field(min_length=1)
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    group_size: int = Field(default=4, ge=1, le=20)

    @field_validator("source_files")
    @classmethod
    def normalize_source_files(cls, value: list[str]) -> list[str]:
        return _normalize_source_files(value)

    @field_validator("process_name", "title")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        return _normalize_required_text(value, "field")

    @model_validator(mode="after")
    def normalize_publish_fields(self) -> AiArtParams:
        self.description = self.description.strip()
        self.tags = _normalize_tags(self.tags)
        return self


class XhsImageNoteParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_dir: Path
    source_files: list[str] = Field(default_factory=list)
    archive_dir: Path | None = None
    failed_dir: Path | None = None
    process_name: str = Field(min_length=1)
    # Photo-Process owns the real prompt for each process name. This optional
    # field remains only for legacy callers that still send extra text.
    image_prompt: str = ""

    @field_validator("source_files")
    @classmethod
    def normalize_source_files(cls, value: list[str]) -> list[str]:
        return _normalize_source_files(value)

    @field_validator("process_name")
    @classmethod
    def normalize_process_name(cls, value: str) -> str:
        return _normalize_required_text(value, "process_name")


class JapaneseParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_dir: Path
    output_dir: Path | None = None
    source_files: list[str] = Field(default_factory=list)
    process_name: str = "日语视觉化"
    # The current 日语视觉化 process is image-only: upload the image and submit
    # without typing extra prompt text unless a caller explicitly supplies one.
    image_prompt: str = ""
    target_gem_url: str | None = None
    dry_run: bool = False

    @field_validator("source_files")
    @classmethod
    def normalize_source_files(cls, value: list[str]) -> list[str]:
        return _normalize_source_files(value)

    @field_validator("process_name")
    @classmethod
    def normalize_process_name(cls, value: str) -> str:
        return _normalize_required_text(value, "process_name")


class GroupedAnimeParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_dir: Path
    source_files: list[str] = Field(default_factory=list)
    archive_dir: Path | None = None
    failed_dir: Path | None = None
    title: str | None = None
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    group_size: int = Field(default=4, ge=1, le=20)
    seconds_per_image: int = Field(default=5, ge=1, le=60)

    @field_validator("source_files")
    @classmethod
    def normalize_source_files(cls, value: list[str]) -> list[str]:
        return _normalize_source_files(value)

    @model_validator(mode="after")
    def normalize_text_fields(self) -> GroupedAnimeParams:
        self.title = _normalize_optional_text(self.title)
        self.description = self.description.strip()
        self.tags = _normalize_tags(self.tags)
        return self


class FinanceParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_dir: Path | None = None
    date: str = "auto"
    script_mode: Literal["90_seconds", "short_30_60", "long_1_3_minutes"] = "90_seconds"
    strategy_profile: Literal["finance_conversion"] = "finance_conversion"
    experiment_id: str | None = Field(default=None, min_length=1, max_length=120)
    duration_variant: DurationVariant | None = None
    script_writer: Literal["rule", "llm"] = "rule"
    script_failure_policy: Literal["fail", "validated_rule_fallback"] = "fail"
    script_repair_attempts: int = Field(default=1, ge=0, le=2)
    title_writer: Literal["follow_script", "rule", "llm"] = "follow_script"
    description_writer: Literal["follow_script", "rule", "llm"] = "follow_script"
    metadata_failure_policy: Literal["fail", "validated_rule_fallback"] = "fail"
    metadata_repair_attempts: int = Field(default=1, ge=0, le=2)
    quality_gate: Literal["strict", "soft"] = "soft"
    douyin_account: str = "金融破壁人"
    kuaishou_account: str = "硅基思维TARS"
    title: str | None = None
    description: str = ""
    tags: list[str] = Field(default_factory=lambda: ["基金", "A股", "投资"])
    dry_run: bool = False

    @model_validator(mode="after")
    def normalize_text_fields(self) -> FinanceParams:
        self.douyin_account = _normalize_required_text(self.douyin_account, "publish account")
        self.kuaishou_account = _normalize_required_text(self.kuaishou_account, "publish account")
        self.title = _normalize_optional_text(self.title)
        self.experiment_id = _normalize_optional_text(self.experiment_id)
        self.description = self.description.strip()
        self.tags = _normalize_tags(self.tags)
        self.duration_variant = self.duration_variant or {
            "short_30_60": "short_30_60",
            "90_seconds": "legacy_90_seconds",
            "long_1_3_minutes": "long_1_3_minutes",
        }[self.script_mode]
        return self


class AiBriefingParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_dir: Path | None = None
    date: str = "auto"
    script_mode: Literal["90_seconds", "short_30_60", "long_1_3_minutes"] = "90_seconds"
    strategy_profile: Literal["ai_reach"] = "ai_reach"
    experiment_id: str | None = Field(default=None, min_length=1, max_length=120)
    duration_variant: DurationVariant | None = None
    script_writer: Literal["rule", "llm"] = "rule"
    script_failure_policy: Literal["fail", "validated_rule_fallback"] = "fail"
    script_repair_attempts: int = Field(default=1, ge=0, le=2)
    title_writer: Literal["follow_script", "rule", "llm"] = "follow_script"
    description_writer: Literal["follow_script", "rule", "llm"] = "follow_script"
    metadata_failure_policy: Literal["fail", "validated_rule_fallback"] = "fail"
    metadata_repair_attempts: int = Field(default=1, ge=0, le=2)
    quality_gate: Literal["strict", "soft"] = "soft"
    handoff_wait_seconds: int = Field(default=600, ge=0, le=3600)
    cover_size: Literal["landscape", "portrait", "story"] = "story"
    cover_template: Literal["default", "ai-poster"] = "ai-poster"
    cover_title_position: Literal["left", "center"] = "center"
    cover_background_image: Path | None = None
    cover_background_scale: float = Field(default=1.0, ge=0.25, le=3.0)
    cover_background_position_x: float = Field(default=50.0, ge=0.0, le=100.0)
    cover_background_position_y: float = Field(default=50.0, ge=0.0, le=100.0)
    douyin_account: str = "金融破壁人"
    kuaishou_account: str = "硅基思维TARS"
    title: str | None = None
    description: str = ""
    tags: list[str] = Field(default_factory=lambda: ["AI", "人工智能", "科技"])
    dry_run: bool = False
    force_regenerate: bool = False

    @model_validator(mode="after")
    def normalize_text_fields(self) -> AiBriefingParams:
        self.douyin_account = _normalize_required_text(self.douyin_account, "publish account")
        self.kuaishou_account = _normalize_required_text(self.kuaishou_account, "publish account")
        self.title = _normalize_optional_text(self.title)
        self.experiment_id = _normalize_optional_text(self.experiment_id)
        self.description = self.description.strip()
        self.tags = _normalize_tags(self.tags)
        self.duration_variant = self.duration_variant or {
            "short_30_60": "short_30_60",
            "90_seconds": "legacy_90_seconds",
            "long_1_3_minutes": "long_1_3_minutes",
        }[self.script_mode]
        return self


class ScriptVideoParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=120)
    script: str = Field(min_length=1, max_length=1200)
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    douyin_account: str = "金融破壁人"
    kuaishou_account: str = "硅基思维TARS"
    dry_run: bool = False
    force_regenerate: bool = False
    voice_rate: float | None = Field(default=None, ge=0.55, le=1.2)

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        return _normalize_required_text(value, "title")

    @field_validator("script")
    @classmethod
    def script_must_have_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("script cannot be blank")
        return value

    @model_validator(mode="after")
    def normalize_publish_fields(self) -> ScriptVideoParams:
        self.description = self.description.strip()
        self.tags = _normalize_tags(self.tags)
        self.douyin_account = _normalize_required_text(self.douyin_account, "publish account")
        self.kuaishou_account = _normalize_required_text(self.kuaishou_account, "publish account")
        return self


class CoverParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=80)
    size: Literal["landscape", "portrait", "story"] = "landscape"
    template: Literal["default", "ai-poster"] = "default"
    title_position: Literal["left", "center"] = "left"
    background_image: Path | None = None
    background_scale: float = Field(default=1.0, ge=0.25, le=3.0)
    background_position_x: float = Field(default=50.0, ge=0.0, le=100.0)
    background_position_y: float = Field(default=50.0, ge=0.0, le=100.0)

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        return _normalize_required_text(value, "title")


class RouteResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content_type: ContentType
    topic: str = Field(min_length=1, max_length=500)
    params: dict[str, Any] = Field(default_factory=dict)

    @field_validator("topic")
    @classmethod
    def normalize_topic(cls, value: str) -> str:
        return _normalize_required_text(value, "route topic")


class ImageFileValidation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: Path
    format: str = Field(min_length=1, max_length=40)
    width: int = Field(ge=1)
    height: int = Field(ge=1)


class ImageValidation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    count: int = Field(ge=0)
    files: list[ImageFileValidation] = Field(max_length=100)


class VideoValidation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    duration_seconds: float = Field(ge=0)
    width: int = Field(ge=1)
    height: int = Field(ge=1)
    aspect_ratio: float = Field(gt=0)
    video_codec: str = Field(min_length=1, max_length=80)
    audio_codec: str | None = Field(default=None, max_length=80)
    decoded_video_frames: int = Field(ge=0)
    decoded_audio_frames: int = Field(ge=0)


class MediaValidation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    images: ImageValidation | None = None
    video: VideoValidation | None = None


class JobProgress(BaseModel):
    model_config = ConfigDict(extra="forbid")

    percent: int = Field(ge=0, le=100)
    phase: str = Field(min_length=1, max_length=120)
    message: str = Field(min_length=1, max_length=500)
    updated_at: str = Field(max_length=80)
    is_estimate: bool = False


class AiArtSourceResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_path: Path
    processed_path: Path | None = None
    archived_path: Path | None = None
    failed_path: Path | None = None
    adapter_result: AdapterResult | None = None
    error: str | None = Field(default=None, max_length=2000)


class AiArtGroupArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int = Field(ge=0)
    source_images: list[Path] = Field(default_factory=list, max_length=100)
    processed_images: list[Path] = Field(default_factory=list, max_length=100)
    bgm: Path | None = None
    mpt_task_dir: Path | None = None
    video: Path | None = None
    validation: VideoValidation | None = None
    publish_results: dict[str, dict[str, Any]] = Field(default_factory=dict)
    error: str | None = Field(default=None, max_length=2000)


class MetadataStageAuditSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["not_started", "running", "succeeded", "failed"]
    generation_mode: str | None = Field(default=None, max_length=80)
    quality_passed: bool | None = None
    error_codes: list[str] = Field(default_factory=list, max_length=100)
    warnings: list[str] = Field(default_factory=list, max_length=100)


class MetadataAuditSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["running", "succeeded", "failed"]
    title: MetadataStageAuditSummary
    description: MetadataStageAuditSummary


class ArtifactSet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    images: list[Path] = Field(default_factory=list, max_length=100)
    video: Path | None = None
    upload_result: dict[str, Any] | None = None
    validation: MediaValidation = Field(default_factory=MediaValidation)
    source_results: list[AiArtSourceResult] = Field(default_factory=list, max_length=100)
    groups: list[AiArtGroupArtifact] = Field(default_factory=list, max_length=100)
    source_document: Path | None = None
    narration_script: str | None = Field(default=None, max_length=4000)
    script_path: Path | None = None
    script_generation_audit: dict[str, Any] | None = None
    script_audit_path: Path | None = None
    script_plan_path: Path | None = None
    script_attempts_path: Path | None = None
    script_quality_report_path: Path | None = None
    final_title: str | None = None
    final_description: str | None = None
    metadata_audit: MetadataAuditSummary | None = None
    metadata_audit_path: Path | None = None
    subtitle: Path | None = None
    mpt_task_dir: Path | None = None
    handoff_path: Path | None = None
    external_status_path: Path | None = None
    manifest_path: Path | None = None
    publish_results: dict[str, dict[str, Any]] = Field(default_factory=dict)


class JobSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    status: JobStatus
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    store_version: int = Field(default=1, ge=1)
    created_at: str | None = Field(default=None, max_length=80)
    updated_at: str | None = Field(default=None, max_length=80)
    started_at: str | None = Field(default=None, max_length=80)
    finished_at: str | None = Field(default=None, max_length=80)
    current_step: PipelineStep | None = None
    progress: JobProgress | None = None
    task: TaskInput
    route: RouteResult | None = None
    artifacts: ArtifactSet = Field(default_factory=ArtifactSet)
    publication_attempts: list[PublicationAttempt] = Field(default_factory=list, max_length=20)
    error: str | None = Field(default=None, max_length=2000)


def deferred_publish_fingerprint(task_id: str, video_sha256: str, request: DeferredPublishInput) -> str:
    payload = json.dumps(
        {
            "task_id": task_id,
            "video_sha256": video_sha256,
            "request": request.model_dump(mode="json"),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def task_fingerprint(task: TaskInput) -> str:
    payload = json.dumps(task.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
