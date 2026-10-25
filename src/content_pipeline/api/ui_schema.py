from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from content_pipeline.models import TaskInput
from content_pipeline.pipeline_config import load_effective_pipeline_defaults
from content_pipeline.pipelines.registry import list_pipelines
from content_pipeline.task_validation import validate_task_params
from content_pipeline.tools.common import run_command

__all__ = [
    "build_ui_pipelines",
    "sanitized_error_items",
    "sanitized_validation_errors",
    "validate_task_for_ui",
]

DEFAULT_PHOTO_PROCESS_OPTIONS = ["动漫图像比例更改", "日语视觉化"]
PHOTO_PROCESS_PIPELINES = {"ai_art", "xhs_image_note", "japanese"}


PIPELINE_LABELS = {
    "anime": "动漫短片",
    "finance": "每日金融",
    "ai_briefing": "AI 简报",
    "ai_art": "AI 艺术处理",
    "xhs_image_note": "小红书图片优化",
    "grouped_anime": "分组动漫",
    "japanese": "日语视觉化",
    "script_video": "口播视频",
}

PIPELINE_ICONS = {
    "anime": "AN",
    "finance": "FN",
    "ai_briefing": "AI",
    "ai_art": "AR",
    "xhs_image_note": "XH",
    "grouped_anime": "GR",
    "japanese": "JP",
    "script_video": "VO",
}

PIPELINE_TASK_DEFAULTS = {
    "anime": {"description": "生成动漫短片", "topic": "动漫短片"},
    "finance": {"description": "生成每日金融视频", "topic": "每日金融"},
    "ai_briefing": {"description": "生成每日 AI 简报视频", "topic": "AI 简报"},
    "ai_art": {"description": "处理 AI 艺术图片并生成视频", "topic": "AI 艺术处理"},
    "xhs_image_note": {"description": "优化小红书 3:4 图片", "topic": "小红书图片优化"},
    "grouped_anime": {"description": "按文件名前缀生成分组动漫视频", "topic": "分组动漫"},
    "japanese": {"description": "执行日语视觉化图片处理", "topic": "日语视觉化"},
    "script_video": {"description": "生成口播视频", "topic": "口播视频"},
}


def _field(
    name: str,
    label: str,
    field_type: str = "text",
    *,
    required: bool = False,
    default: Any = None,
    placeholder: str = "",
    help_text: str = "",
    minimum: float | None = None,
    maximum: float | None = None,
    step: float | None = None,
    options: list[dict[str, Any]] | None = None,
    rows: int | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "label": label,
        "type": field_type,
        "required": required,
        "default": default,
        "placeholder": placeholder,
        "help": help_text,
        "minimum": minimum,
        "maximum": maximum,
        "step": step,
        "options": options or [],
        "rows": rows,
    }


def _quality_gate_field() -> dict[str, Any]:
    return _field(
        "quality_gate",
        "质量门禁",
        "select",
        default="soft",
        help_text="soft 把非合规的质量问题记为告警，不再阻断任务；strict 恢复旧行为，问题一律阻断。",
        options=[
            {"value": "soft", "label": "soft：告警并继续（默认）"},
            {"value": "strict", "label": "strict：阻断任务（旧行为）"},
        ],
    )


def _metadata_fields() -> list[dict[str, Any]]:
    writer_options = [
        {"value": "follow_script", "label": "跟随口播稿生成模式"},
        {"value": "rule", "label": "规则生成"},
        {"value": "llm", "label": "大模型生成"},
    ]
    return [
        _field(
            "title_writer",
            "标题生成",
            "select",
            default="follow_script",
            help_text="留空标题时，在口播完成后独立生成；follow_script 跟随 script_writer。手填标题优先。",
            options=writer_options,
        ),
        _field(
            "description_writer",
            "简介生成",
            "select",
            default="follow_script",
            help_text="留空简介时，在标题完成后独立生成；follow_script 跟随 script_writer，rule 使用原固定简介。",
            options=writer_options,
        ),
        _field(
            "metadata_failure_policy",
            "标题/简介失败策略",
            "select",
            default="fail",
            help_text="默认阻断封面、视频和发布；显式选择回退时，规则结果也必须通过校验。",
            options=[
                {"value": "fail", "label": "失败并阻断后续"},
                {"value": "validated_rule_fallback", "label": "仅回退到已通过校验的规则结果"},
            ],
        ),
        _field("metadata_repair_attempts", "标题/简介定向修复次数", "number", default=1, minimum=0, maximum=2),
    ]


PIPELINE_FIELDS: dict[str, list[dict[str, Any]]] = {
    "anime": [
        _field("script", "分镜脚本", "textarea", required=True, placeholder="第一幕……第二幕……", rows=6),
        _field("dry_run", "安全干运行", "checkbox", default=True, help_text="仅生成占位产物，不调用真实工具。"),
    ],
    "finance": [
        _field("source_dir", "Markdown 目录", "path", placeholder="留空时使用 FINANCE_MD_DIR"),
        _field("date", "内容日期", default="auto", placeholder="auto 或 YYYY-MM-DD"),
        _field(
            "script_mode",
            "脚本模式",
            "select",
            default="90_seconds",
            options=[
                {"value": "90_seconds", "label": "90 秒（兼容）"},
                {"value": "short_30_60", "label": "30–60 秒实验版"},
                {"value": "long_1_3_minutes", "label": "1–3 分钟实验版"},
            ],
        ),
        _field("experiment_id", "实验编号", placeholder="可选，例如 2026-11-content-test-01"),
        _field(
            "script_writer",
            "口播稿生成",
            "select",
            default="rule",
            help_text="rule 为本地规则稿；llm 执行主线规划、写稿和最多两次定向修复。失败策略由下方设置决定。",
            options=[{"value": "rule", "label": "规则生成"}, {"value": "llm", "label": "大模型两阶段写稿"}],
        ),
        _field(
            "script_failure_policy",
            "写稿失败策略",
            "select",
            default="fail",
            options=[
                {"value": "fail", "label": "失败并阻断后续"},
                {"value": "validated_rule_fallback", "label": "仅回退到已通过校验的规则稿"},
            ],
        ),
        _field("script_repair_attempts", "定向修复次数", "number", default=1, minimum=0, maximum=2),
        _quality_gate_field(),
        *_metadata_fields(),
        _field(
            "title",
            "视频标题",
            placeholder="留空时自动生成",
            help_text="手填非空标题优先，并跳过标题生成阶段；留空时按标题生成模式处理。",
        ),
        _field(
            "description",
            "视频简介",
            "textarea",
            default="",
            placeholder="留空时自动生成",
            help_text="手填非空简介优先，并跳过简介生成阶段；规则模式使用原固定简介。",
            rows=3,
        ),
        _field("tags", "标签", "tags", default=["基金", "A股", "投资"], placeholder="基金, A股, 投资"),
        _field("douyin_account", "抖音账号", default="金融破壁人"),
        _field("kuaishou_account", "快手账号", default="硅基思维TARS"),
        _field(
            "dry_run",
            "安全干运行",
            "checkbox",
            default=True,
            help_text="仅媒体与发布干运行；口播、标题或简介选择 llm（含跟随口播）时仍会真实调用大模型。",
        ),
    ],
    "ai_briefing": [
        _field("source_dir", "简报目录", "path", placeholder="留空时使用 AI_BRIEFING_DIR"),
        _field("date", "内容日期", default="auto", placeholder="auto 或 YYYY-MM-DD"),
        _field(
            "script_mode",
            "脚本模式",
            "select",
            default="90_seconds",
            options=[
                {"value": "90_seconds", "label": "90 秒（兼容）"},
                {"value": "short_30_60", "label": "30–60 秒实验版"},
                {"value": "long_1_3_minutes", "label": "1–3 分钟实验版"},
            ],
        ),
        _field("experiment_id", "实验编号", placeholder="可选，例如 2026-11-content-test-01"),
        _field(
            "script_writer",
            "口播稿生成",
            "select",
            default="rule",
            help_text="rule 为本地规则稿；llm 执行主线规划、写稿和最多两次定向修复。失败策略由下方设置决定。",
            options=[{"value": "rule", "label": "规则生成"}, {"value": "llm", "label": "大模型两阶段写稿"}],
        ),
        _field(
            "script_failure_policy",
            "写稿失败策略",
            "select",
            default="fail",
            options=[
                {"value": "fail", "label": "失败并阻断后续"},
                {"value": "validated_rule_fallback", "label": "仅回退到已通过校验的规则稿"},
            ],
        ),
        _field("script_repair_attempts", "定向修复次数", "number", default=1, minimum=0, maximum=2),
        _quality_gate_field(),
        *_metadata_fields(),
        _field("handoff_wait_seconds", "交接等待秒数", "number", default=0, minimum=0, maximum=3600),
        _field(
            "title",
            "简报标题",
            placeholder="留空时自动生成",
            help_text="手填非空标题优先，并跳过标题生成阶段；同时用于封面、视频和发布标题，保留各目标长度适配。",
        ),
        _field(
            "cover_size",
            "封面尺寸",
            "select",
            default="story",
            options=[
                {"value": "landscape", "label": "横版 1920×1080"},
                {"value": "portrait", "label": "竖版 1080×1440"},
                {"value": "story", "label": "故事 1080×1920"},
            ],
        ),
        _field(
            "cover_template",
            "封面模板",
            "select",
            default="ai-poster",
            options=[
                {"value": "default", "label": "默认"},
                {"value": "ai-poster", "label": "AI 海报"},
            ],
        ),
        _field(
            "cover_title_position",
            "封面标题位置",
            "select",
            default="center",
            options=[{"value": "left", "label": "左对齐"}, {"value": "center", "label": "居中"}],
        ),
        _field("cover_background_image", "封面背景图片", "path", help_text="可选 PNG、JPEG 或 WebP，最大 5 MiB。"),
        _field(
            "cover_background_scale",
            "封面背景缩放",
            "number",
            default=1.0,
            minimum=0.25,
            maximum=3.0,
            step=0.05,
        ),
        _field("cover_background_position_x", "封面背景水平位置", "number", default=50.0, minimum=0, maximum=100),
        _field("cover_background_position_y", "封面背景垂直位置", "number", default=50.0, minimum=0, maximum=100),
        _field(
            "description",
            "视频简介",
            "textarea",
            default="",
            placeholder="留空时自动生成",
            help_text="手填非空简介优先，并跳过简介生成阶段；规则模式使用原固定简介。",
            rows=3,
        ),
        _field("tags", "标签", "tags", default=["AI", "人工智能", "科技"], placeholder="AI, 人工智能, 科技"),
        _field("douyin_account", "抖音账号", default="金融破壁人"),
        _field("kuaishou_account", "快手账号", default="硅基思维TARS"),
        _field(
            "dry_run",
            "安全干运行",
            "checkbox",
            default=True,
            help_text="仅媒体与发布干运行；口播、标题或简介选择 llm（含跟随口播）时仍会真实调用大模型。",
        ),
        _field("force_regenerate", "强制重新生成", "checkbox", default=False),
    ],
    "ai_art": [
        _field("source_dir", "输入图片目录", "path", required=True, placeholder=r"G:\Job\Photo-Datasets\input"),
        _field("archive_dir", "成功归档目录", "path"),
        _field("failed_dir", "失败归档目录", "path"),
        _field("source_files", "指定文件", "tags", help_text="留空时处理目录顶层全部图片。"),
        _field("process_name", "处理名称", required=True, placeholder="Photo-Process 中配置的处理名称"),
        _field("title", "视频标题", required=True),
        _field("description", "视频简介", "textarea", rows=3),
        _field("tags", "标签", "tags", placeholder="AI绘画, 作品集"),
        _field("group_size", "每组图片数", "number", default=4, minimum=1, maximum=20),
    ],
    "xhs_image_note": [
        _field("source_dir", "输入图片目录", "path", required=True, placeholder=r"G:\Job\Photo-Datasets\input"),
        _field("archive_dir", "成功归档目录", "path"),
        _field("failed_dir", "失败归档目录", "path"),
        _field("source_files", "指定文件", "tags", help_text="留空时处理目录顶层全部图片。"),
        _field("process_name", "处理名称", required=True, placeholder="Photo-Process 中配置的 3:4 图片优化处理名称"),
    ],
    "grouped_anime": [
        _field("source_dir", "输入图片目录", "path", required=True, placeholder=r"G:\Job\Photo-Datasets\input"),
        _field("archive_dir", "成功归档目录", "path"),
        _field("failed_dir", "失败归档目录", "path"),
        _field("source_files", "指定文件", "tags", help_text="留空时按文件名前缀扫描目录。"),
        _field("title", "视频标题"),
        _field("description", "视频简介", "textarea", rows=3),
        _field("tags", "标签", "tags", placeholder="动漫, AI视频, 作品集"),
        _field("group_size", "每组图片数", "number", default=4, minimum=1, maximum=20),
        _field("seconds_per_image", "单图秒数", "number", default=5, minimum=1, maximum=60),
    ],
    "japanese": [
        _field("source_dir", "输入图片目录", "path", required=True),
        _field("output_dir", "输出目录", "path", placeholder="留空时写入输入目录/日语改图"),
        _field("source_files", "指定文件", "tags"),
        _field("process_name", "处理名称", default="日语视觉化", help_text="Photo-Process 中配置的处理名称。"),
        _field(
            "image_prompt",
            "兼容附加文本",
            "textarea",
            help_text="通常留空；真实提示词由 Photo-Process 按处理名称决定。",
            rows=3,
        ),
        _field("target_gem_url", "目标 Gem URL", "url"),
        _field("dry_run", "安全干运行", "checkbox", default=True),
    ],
    "script_video": [
        _field("title", "视频标题", required=True),
        _field(
            "script",
            "口播稿",
            "textarea",
            required=True,
            help_text="建议 300–600 字；超出建议范围仍可生成和发布，金融内容应包含必要的风险提示。",
            rows=10,
        ),
        _field("description", "视频简介", "textarea", rows=3),
        _field("tags", "标签", "tags", placeholder="AI, 人工智能, 科技"),
        _field("douyin_account", "抖音账号", default="金融破壁人"),
        _field("kuaishou_account", "快手账号", default="硅基思维TARS"),
        _field("dry_run", "安全干运行", "checkbox", default=True),
        _field("force_regenerate", "强制重新生成", "checkbox", default=False),
        _field(
            "voice_rate",
            "朗读速度",
            "number",
            placeholder="留空自动；1.0 为正常",
            help_text="越小越慢，越大越快。建议 0.75–1.1。",
            minimum=0.55,
            maximum=1.2,
            step=0.05,
        ),
    ],
}


def build_ui_pipelines(settings: Any | None = None) -> list[dict[str, Any]]:
    metadata = {item.content_type: item for item in list_pipelines()}
    default_params = _ui_pipeline_defaults(settings)
    photo_process_options = _photo_process_options(settings)
    result = []
    for content_type in PIPELINE_FIELDS:
        meta = metadata.get(content_type)
        if meta is None:
            continue
        defaults = default_params.get(content_type, {})
        result.append(
            {
                "content_type": content_type,
                "label": PIPELINE_LABELS[content_type],
                "icon": PIPELINE_ICONS[content_type],
                "description": meta.description,
                "task_defaults": PIPELINE_TASK_DEFAULTS[content_type],
                "required_params": meta.required_params,
                "external_tools": meta.external_tools,
                "publish_targets": meta.publish_targets,
                "fields": _fields_with_defaults(
                    PIPELINE_FIELDS[content_type],
                    defaults,
                    photo_process_options if content_type in PHOTO_PROCESS_PIPELINES else None,
                ),
            }
        )
    return result


def _ui_pipeline_defaults(settings: Any | None) -> dict[str, dict[str, Any]]:
    if settings is None:
        return {}
    payload = load_effective_pipeline_defaults(settings)
    if not payload.get("enabled"):
        return {}
    pipelines = payload.get("pipelines") or {}
    if not isinstance(pipelines, dict):
        return {}
    defaults: dict[str, dict[str, Any]] = {}
    for content_type, pipeline_config in pipelines.items():
        if not isinstance(pipeline_config, dict):
            continue
        params = pipeline_config.get("params") or {}
        if isinstance(params, dict):
            defaults[str(content_type)] = params
    return defaults


def _photo_process_options(settings: Any | None) -> list[dict[str, str]]:
    names = _photo_process_names_from_cli(settings) if settings is not None else []
    if not names:
        names = DEFAULT_PHOTO_PROCESS_OPTIONS
    return [{"value": name, "label": name} for name in dict.fromkeys(names) if name]


def _photo_process_names_from_cli(settings: Any) -> list[str]:
    photo_process_dir = getattr(settings, "photo_process_dir", None)
    photo_process_python = getattr(settings, "photo_process_python", None)
    if photo_process_dir is None or photo_process_python is None:
        return []

    try:
        result = run_command(
            [str(photo_process_python), str(photo_process_dir / "main.py"), "list-processes", "--json"],
            cwd=photo_process_dir,
            timeout=30,
            env={"PYTHONIOENCODING": "utf-8"},
        )
        payload = json.loads(result.stdout)
    except Exception:
        return []

    processes = payload.get("processes") if isinstance(payload, dict) else None
    if not isinstance(processes, list):
        return []

    names: list[str] = []
    for process in processes:
        name = process.get("name") if isinstance(process, dict) else process
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return names


def _fields_with_defaults(
    fields: list[dict[str, Any]],
    defaults: dict[str, Any],
    photo_process_options: list[dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    hydrated_fields = []
    for field in fields:
        hydrated = field.copy()
        if field["name"] in defaults:
            hydrated["default"] = defaults[field["name"]]
        if field["name"] == "process_name" and photo_process_options is not None:
            hydrated["type"] = "select"
            hydrated["options"] = _options_with_default(photo_process_options, hydrated.get("default"))
        hydrated_fields.append(hydrated)
    return hydrated_fields


def _options_with_default(options: list[dict[str, str]], default: Any) -> list[dict[str, str]]:
    hydrated = [option.copy() for option in options]
    if isinstance(default, str) and default and all(option.get("value") != default for option in hydrated):
        hydrated.insert(0, {"value": default, "label": default})
    return hydrated


def validate_task_for_ui(task: TaskInput) -> tuple[TaskInput, list[str]]:
    normalized = validate_task_params(task)
    content_type = normalized.content_type

    meta = next((item for item in list_pipelines() if item.content_type == content_type), None)
    if meta is None:
        raise ValueError("所选流水线当前不可用")
    unsupported = sorted({target.platform for target in normalized.publish_targets} - set(meta.publish_targets))
    if unsupported:
        raise ValueError(f"流水线不支持以下发布平台：{', '.join(unsupported)}")

    warnings: list[str] = []
    if not normalized.publish and normalized.publish_targets:
        warnings.append("发布已关闭；发布目标仅作为模板保留，不会调用上传器。")
    if normalized.publish:
        warnings.append("该任务会在生成和验证后尝试发布，请先使用关闭发布的同类任务审查产物。")
    if normalized.params.get("dry_run"):
        if content_type in {"finance", "ai_briefing"}:
            warnings.append(
                "安全干运行仅跳过真实媒体与发布；口播、标题或简介选择 llm（含跟随口播）时仍会真实调用大模型。"
            )
        else:
            warnings.append("安全干运行不会调用真实生成或发布工具。")
    if content_type == "script_video":
        script = str(normalized.params.get("script") or "").strip()
        if script and not 300 <= len(script) <= 600:
            warnings.append(f"口播稿当前为 {len(script)} 字，超出建议的 300–600 字范围，仍可生成和发布。")
    return normalized, warnings


def sanitized_error_items(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "field": ".".join(str(item) for item in error["loc"] if item != "body"),
            "message": error["msg"],
            "type": error["type"],
        }
        for error in errors
    ]


def sanitized_validation_errors(exc: ValidationError) -> list[dict[str, Any]]:
    return sanitized_error_items(exc.errors())
