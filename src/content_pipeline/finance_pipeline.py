from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from .errors import ConfigError
from .finance_script import FinanceScriptError, build_daily_markdown_script
from .media_validation import validate_video
from .metadata_generation.audit import generate_pipeline_metadata
from .models import FinanceParams, PipelineStep, PublishTarget
from .pipelines.registry import PipelineContext, PipelineMeta, register
from .publish_policy import resolve_publish_plan
from .publishing import publish_generated_video
from .script_generation import ScriptGenerationService, build_structured_facts, validate_script
from .script_generation.audit import persist_script_audit, record_script_attempt_event
from .tools.finance_mpt_client import call_finance_mpt
from .tools.narrated_mpt_client import looks_like_file_reference, validate_spoken_subtitle

__all__ = [
    "DISCLAIMER",
    "FINANCE_NARRATION_MAX_CHARS",
    "FINANCE_SECTION_LABELS",
    "RISK_NOTE",
    "build_90_second_script",
    "build_finance_script_material",
    "extract_response",
    "run_finance_pipeline",
    "select_daily_markdown",
    "validate_finance_narration",
]

DISCLAIMER = "以上内容仅为市场信息整理和个人观点，不构成任何投资建议。"
RISK_NOTE = "面对市场波动，请结合自身投资期限、仓位水平和风险承受能力审慎决策。"
FINANCE_NARRATION_MAX_CHARS = 450
FINANCE_SECTION_LABELS = (
    "顶部摘要",
    "四大指数",
    "四指数",
    "四指数行情",
    "四指数表格",
    "五指数",
    "五指数表格",
    "板块轮动",
    "估值水位",
    "今日关键词",
    "三个信号",
    "核心信号",
    "我的解读",
    "策略建议",
    "投资建议",
    "黄金债市",
    "黄金",
    "债券",
)


RESPONSE_HEADING_RE = re.compile(r"(?im)^##\s+Response\s*$")


def _has_response_section(path: Path) -> bool:
    try:
        return bool(RESPONSE_HEADING_RE.search(path.read_text(encoding="utf-8-sig")))
    except OSError:
        return False


def select_daily_markdown(source_dir: Path, requested_date: str = "auto", *, today: date | None = None) -> Path:
    source_dir = source_dir.expanduser().resolve()
    if not source_dir.is_dir():
        raise ConfigError(f"finance markdown directory not found: {source_dir}")

    current = today or date.today()
    if requested_date == "auto":
        dates = [current, current - timedelta(days=1)]
    else:
        try:
            dates = [datetime.strptime(requested_date.replace("/", "-"), "%Y-%m-%d").date()]
        except ValueError as exc:
            raise ConfigError("finance params.date must be 'auto' or YYYY-MM-DD") from exc

    for candidate_date in dates:
        prefix = candidate_date.isoformat()
        candidates = sorted(
            source_dir.glob(f"{prefix}*.md"),
            key=lambda path: (path.stat().st_mtime_ns, path.name),
            reverse=True,
        )
        for candidate in candidates:
            if requested_date != "auto" or _has_response_section(candidate):
                return candidate
    if requested_date == "auto":
        candidates = sorted(
            source_dir.glob("*.md"),
            key=lambda path: (path.stat().st_mtime_ns, path.name),
            reverse=True,
        )
        for candidate in candidates:
            if _has_response_section(candidate):
                return candidate
    wanted = ", ".join(item.isoformat() for item in dates)
    raise ConfigError(f"no finance markdown with a '## Response' section found for: {wanted}")


def extract_response(markdown: str) -> str:
    match = RESPONSE_HEADING_RE.search(markdown)
    if not match:
        raise ConfigError("finance markdown is missing a '## Response' section")
    response = markdown[match.end() :].strip()
    if not response:
        raise ConfigError("finance markdown '## Response' section is empty")
    has_report_content = bool(re.search(r"(?m)^\s*(?:\||#{2,4}\s+|\*\*一句话总结|一句话总结[：:])", response))
    if not has_report_content and ("文件路径" in response or looks_like_file_reference(response)):
        raise ConfigError("finance markdown response contains a file reference instead of report content")
    return response


def _clean_markdown(value: str) -> str:
    value = re.sub(r"!\[[^]]*]\([^)]*\)", "", value)
    value = re.sub(r"\[([^]]+)]\([^)]*\)", r"\1", value)
    value = re.sub(r"[`*_>#]", "", value)
    value = re.sub(r"^[|\s]+|[|\s]+$", "", value)
    value = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9%+\-.,，。；：:、（）()/$≈=~]", "", value)
    return re.sub(r"\s+", "", value).strip("，。； ")


def _bullet_text(line: str) -> str:
    return re.sub(r"^[-•·]+\s*", "", _clean_markdown(line))


def _truncate_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    clipped = value[:limit]
    stop = max(clipped.rfind(mark) for mark in "。；！？")
    if stop >= limit - 60:
        return clipped[: stop + 1]
    trailing_number = re.search(r"[+-]?\d+(?:\.\d+)?%?$", clipped)
    if trailing_number:
        clipped = clipped[: trailing_number.start()]
    return clipped.rstrip("，。；、:：")


def _section_text(section: str, limit: int) -> str:
    lines = [_bullet_text(line) for line in section.splitlines() if not re.search(r"\|\s*:?-{2,}", line)]
    return _truncate_text("；".join(line for line in lines if line), limit)


def _section(markdown: str, *keywords: str) -> str:
    candidates = re.finditer(r"(?m)^(?:#{2,4}\s+(.+?)|\*\*(.+?)\*\*)\s*$", markdown)
    headings = []
    for heading in candidates:
        title = _clean_markdown(heading.group(1) or heading.group(2))
        if heading.group(1) is None and (len(title) > 40 or any(mark in title for mark in "。！？")):
            continue
        if any(label in title for label in FINANCE_SECTION_LABELS):
            headings.append((heading, title))
    for index, (heading, title) in enumerate(headings):
        if any(keyword in title for keyword in keywords):
            end = headings[index + 1][0].start() if index + 1 < len(headings) else len(markdown)
            return markdown[heading.end() : end].strip()
    return ""


def _table_row(line: str) -> list[str] | None:
    stripped = line.strip()
    if not stripped.startswith("|") or re.search(r"\|\s*:?-{2,}", stripped):
        return None
    cells = [_clean_markdown(cell) for cell in stripped.strip("|").split("|")]
    if not cells or cells[0] in {"指数", "板块", "品种"}:
        return None
    return cells


def _table_rows(section: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in section.splitlines():
        row = _table_row(line)
        if row is not None:
            rows.append(row)
    return rows


def _table_text(section: str) -> str:
    header: list[str] = []
    rows: list[str] = []
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or re.search(r"\|\s*:?-{2,}", stripped):
            continue
        cells = [_clean_markdown(cell) for cell in stripped.strip("|").split("|")]
        if not cells:
            continue
        if cells[0] in {"指数", "板块", "品种"}:
            header = cells
            continue
        labels = header[1:]
        parts = [cells[0]]
        for index, cell in enumerate(cells[1:]):
            label = labels[index] if index < len(labels) else ""
            parts.append(f"{label} {cell}" if label and not cell.startswith(label) else cell)
        rows.append(" ".join(part for part in parts if part))
    return "；".join(rows)


def _sector_bullet_row(line: str) -> list[str] | None:
    match = re.match(r"^([^：:（）()]{2,24})(?:（[^）]*）)?[：:](.+)$", _bullet_text(line))
    if not match:
        return None
    stocks = [
        f"{name}{change}"
        for name, change in re.findall(r"([\u4e00-\u9fffA-Za-z]{2,12})([+-]\d+(?:\.\d+)?%)", match.group(2))
    ]
    if not stocks:
        return None
    return [match.group(1), *stocks]


def _sector_row(line: str) -> list[str] | None:
    row = _table_row(line)
    if row is None:
        return _sector_bullet_row(line)
    if any(cell in {"代表个股", "涨幅", "跌幅", "涨跌幅"} for cell in row[1:]):
        return None
    if not re.search(r"\d+(?:\.\d+)?%", " ".join(row[1:])):
        return None
    return row


def _sector_groups(section: str) -> dict[str, list[list[str]]]:
    groups: dict[str, list[list[str]]] = {"up": [], "down": [], "": []}
    direction = ""
    for line in section.splitlines():
        row = _sector_row(line)
        if row is not None:
            groups[direction].append(row)
            continue
        title = _clean_markdown(line)
        if "领涨" in title:
            direction = "up"
        elif "领跌" in title:
            direction = "down"
    return groups


def _sector_rows(section: str) -> list[list[str]]:
    groups = _sector_groups(section)
    up, down = groups["up"], groups["down"]
    rows: list[list[str]] = []
    for index in range(max(len(up), len(down))):
        for group in (up, down):
            if index < len(group):
                rows.append(group[index])
    return rows + groups[""]


def _summary_table(markdown: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for row in _table_rows(markdown):
        if len(row) < 2 or row[0] in {"模块", "项目", "类别"}:
            continue
        values[_clean_markdown(row[0])] = _clean_markdown(row[1])
    return values


def _summary_value(values: dict[str, str], *keywords: str) -> str:
    for label, value in values.items():
        if any(keyword in label for keyword in keywords):
            return value
    return ""


def _first_sentence(value: str) -> str:
    cleaned = _clean_markdown(value)
    parts = re.split(r"(?<=[。！？])", cleaned)
    return next((part.strip() for part in parts if part.strip()), cleaned)


def _first_sentences(value: str, limit: int) -> list[str]:
    cleaned = _clean_markdown(value)
    return [part.strip() for part in re.split(r"(?<=[。！？])", cleaned) if part.strip()][:limit]


def _trim_complete(value: str, limit: int) -> str:
    value = value.strip("，。； ")
    if len(value) <= limit:
        return value
    clipped = value[:limit]
    stops = [clipped.rfind(mark) for mark in "。！？；"]
    stop = max(stops)
    if stop >= max(120, limit - 100):
        return clipped[: stop + 1].strip()
    return clipped.rstrip("，；： ") + "。"


def _validate_finance_script(script: str) -> None:
    if not 300 <= len(script) <= 600:
        raise ConfigError(f"finance narration must contain 300-600 characters; generated {len(script)} characters")
    _validate_finance_script_safety(script)


def _validate_finance_script_safety(script: str) -> None:
    normalized = script.replace(" ", "")
    if (
        looks_like_file_reference(normalized)
        or "文件路径" in script
        or re.search(r"(?:^|[：:])(?:~|[A-Za-z]:)[/\\].+\.(?:md|txt|json|srt)", script, re.IGNORECASE)
    ):
        raise ConfigError("finance narration contains a file reference instead of spoken content")
    if not all(marker in script for marker in ("指数", "板块")):
        raise ConfigError("finance narration is missing required index or sector market information")


def build_finance_script_material(response: str, publication_date: str, *, duration_variant: str = "legacy_90_seconds") -> dict[str, object]:
    summary_values = _summary_table(response)
    index_section = _section(response, "四大指数", "四指数", "五指数")
    sector_section = _section(response, "板块轮动")
    valuation_section = _section(response, "估值水位")
    cross_asset_section = _section(response, "黄金债市", "黄金", "债券")
    material = {
        "publication_date": publication_date,
        "summary": _summary_value(summary_values, "盘面定性", "顶部摘要", "一句话总结"),
        "indices": _table_rows(index_section),
        "index_summary": _summary_value(summary_values, "四大指数", "四指数"),
        "sector_rows": _sector_rows(sector_section)[:8],
        "sector_text": _section_text(sector_section, 1000),
        "sector_summary": _summary_value(summary_values, "板块轮动"),
        "keywords": _first_sentences(_section(response, "今日关键词", "三个信号", "核心信号"), 4),
        "keyword_summary": _summary_value(summary_values, "今日关键词", "三个信号", "核心信号"),
        "strategy": _first_sentences(_section(response, "我的解读", "策略建议", "投资建议"), 4),
        "strategy_summary": _summary_value(summary_values, "我的解读", "策略建议", "投资建议"),
        "valuation": _table_text(valuation_section) or _section_text(valuation_section, 240),
        "valuation_summary": _summary_value(summary_values, "估值水位", "估值"),
        "cross_asset": _section_text(cross_asset_section, 240),
        "cross_asset_summary": _summary_value(summary_values, "黄金债市", "黄金", "债券"),
        "risk_note": RISK_NOTE,
        "disclaimer": DISCLAIMER,
    }
    try:
        if duration_variant in {"legacy_90_seconds", "90_seconds"}:
            material["fallback_script"] = build_90_second_script(response, publication_date)
        else:
            limits = {"short_30_60": (180, 320), "long_1_3_minutes": (300, 800)}.get(duration_variant, (300, 600))
            material["fallback_script"] = build_daily_markdown_script(response, publication_date, min_chars=limits[0], max_chars=limits[1])
    except ConfigError as exc:
        material["fallback_script"] = ""
        material["fallback_error"] = str(exc)
    return material


def validate_finance_narration(script: str, material: dict[str, object], *, enforce_length: bool = True) -> None:
    report = validate_script(script, build_structured_facts("finance", dict(material)), profile="legacy")
    errors = [
        issue
        for issue in report.errors
        if (enforce_length or issue.code != "length") and issue.code != "unsupported_number"
    ]
    source_numbers = _finance_material_number_tokens(material)
    extra_numbers = {value for value in _number_tokens(script) - source_numbers if value not in {"90", "300", "600"}}
    if extra_numbers:
        raise ConfigError("finance LLM narration contains numbers not present in the source material")
    if errors:
        if any(issue.code == "unsupported_number" for issue in errors):
            raise ConfigError("finance LLM narration contains numbers not present in the source material")
        if any(issue.code == "finance_disclaimer" for issue in errors):
            raise ConfigError("finance LLM narration is missing investment disclaimer")
        if any(issue.code == "finance_prohibited" for issue in errors):
            raise ConfigError("finance LLM narration contains prohibited investment language")
        raise ConfigError("; ".join(f"{issue.code}: {issue.message}" for issue in errors))


def _finance_material_number_tokens(material: dict[str, object]) -> set[str]:
    allowed_keys = {
        "fallback_script",
        "publication_date",
        "summary",
        "indices",
        "index_summary",
        "sector_rows",
        "sector_text",
        "sector_summary",
        "keywords",
        "keyword_summary",
        "strategy",
        "strategy_summary",
        "valuation",
        "valuation_summary",
        "cross_asset",
        "cross_asset_summary",
    }
    return _number_tokens_from_value({key: value for key, value in material.items() if key in allowed_keys})


def _number_tokens_from_value(value: object) -> set[str]:
    if isinstance(value, str):
        return _number_tokens(value)
    if isinstance(value, dict):
        tokens: set[str] = set()
        for nested in value.values():
            tokens.update(_number_tokens_from_value(nested))
        return tokens
    if isinstance(value, list | tuple):
        tokens = set()
        for nested in value:
            tokens.update(_number_tokens_from_value(nested))
        return tokens
    return set()


def _number_tokens(value: str) -> set[str]:
    tokens = set(re.findall(r"\d+(?:\.\d+)?%?", value.replace(",", "")))
    normalized = set(tokens)
    for token in tokens:
        suffix = "%" if token.endswith("%") else ""
        core = token.rstrip("%")
        if core.isdigit():
            normalized.add(str(int(core)) + suffix)
    return normalized


def build_90_second_script(response: str, publication_date: str) -> str:
    try:
        return build_daily_markdown_script(response, publication_date)
    except FinanceScriptError as exc:
        raise ConfigError(str(exc)) from exc


def _publication_date_from_path(path: Path) -> str:
    match = re.match(r"(\d{4}-\d{2}-\d{2})", path.name)
    if not match:
        raise ConfigError(f"finance markdown filename must start with YYYY-MM-DD: {path.name}")
    return match.group(1)


@register(
    "finance",
    meta=PipelineMeta(
        content_type="finance",
        description="Markdown parsing -> narrated MPT video -> Douyin/Kuaishou publish",
        required_params=[],
        external_tools=["MoneyPrinterTurbo"],
        publish_targets=["douyin", "kuaishou"],
    ),
)
def run_finance_pipeline(ctx: PipelineContext) -> None:
    task_id = ctx.task_id
    snapshot = ctx.snapshot
    artifacts = ctx.artifacts
    store = ctx.store
    settings = ctx.settings
    params = FinanceParams.model_validate(ctx.route.params)
    source_dir = params.source_dir or settings.finance_md_dir
    job_dir = store.job_dir(task_id)

    store.mark_running(task_id, PipelineStep.SOURCE_SCAN)
    source = select_daily_markdown(source_dir, params.date)
    publication_date = _publication_date_from_path(source)
    response = extract_response(source.read_text(encoding="utf-8-sig"))
    material = build_finance_script_material(response, publication_date, duration_variant=params.duration_variant or "long_1_3_minutes")
    generator = ScriptGenerationService(settings)
    try:
        if params.script_writer == "llm":
            generation = generator.generate(
                content_type="finance",
                material=material,
                fallback_script=str(material.get("fallback_script") or ""),
                failure_policy=params.script_failure_policy,
                repair_attempts=params.script_repair_attempts,
                gate=params.quality_gate,
                strategy_profile=params.strategy_profile,
                duration_variant=params.duration_variant or "long_1_3_minutes",
                on_attempt=lambda attempt: record_script_attempt_event(store, task_id, attempt),
            )
        else:
            generation = generator.validate_rule(
                content_type="finance",
                material=material,
                script=str(material.get("fallback_script") or ""),
                gate=params.quality_gate,
                strategy_profile=params.strategy_profile,
                duration_variant=params.duration_variant or "long_1_3_minutes",
            )
    except Exception as exc:
        artifacts.upload_result = {
            "script_writer": "llm_failed" if params.script_writer == "llm" else "rule_failed",
            "script_writer_error": str(exc),
        }
        store.set_artifacts(task_id, artifacts)
        store.event(task_id, "script_generation_failed", {"writer": params.script_writer, "error": str(exc)[:500]})
        raise ConfigError(f"finance script generation failed: {exc}") from exc
    script = generation.script
    audit = generation.audit.model_dump(mode="json")
    script_path = job_dir / "finance_script.txt"
    legacy_audit_path = job_dir / "finance_script_audit.json"
    script_path.write_text(script, encoding="utf-8")
    legacy_audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    audit_paths = persist_script_audit(job_dir, generation.audit)
    if generation.audit.plan:
        store.event(
            task_id,
            "script_plan_generated",
            {
                "main_fact_id": generation.audit.plan.main_fact_id,
                "selected_fact_ids": generation.audit.plan.required_facts,
            },
        )
    artifacts.source_document = source
    artifacts.narration_script = script
    artifacts.script_path = script_path
    artifacts.script_generation_audit = audit
    artifacts.script_audit_path = audit_paths["audit"]
    artifacts.script_plan_path = audit_paths["plan"]
    artifacts.script_attempts_path = audit_paths["attempts"]
    artifacts.script_quality_report_path = audit_paths["quality"]
    artifacts.upload_result = {
        "script_writer": generation.audit.generation_mode,
        "script_prompt_version": generation.audit.prompt_version,
        "script_attempts": len(generation.audit.attempts),
        "script_warnings": [issue.message for issue in generation.audit.quality_report.warnings],
    }
    store.set_artifacts(task_id, artifacts)

    title, description = generate_pipeline_metadata(
        ctx,
        params=params,
        material=material,
        script=script,
        rule_title=f"每日基金日报 · {publication_date}",
    )
    store.mark_running(task_id, PipelineStep.VIDEO)
    mpt_result = call_finance_mpt(
        publication_date=publication_date,
        title=title,
        script=script,
        output_dir=job_dir / "video",
        settings=settings,
        dry_run=params.dry_run,
        target_duration_seconds=(
            45.0
            if params.duration_variant == "short_30_60"
            else 120.0
            if params.duration_variant == "long_1_3_minutes"
            else 90.0
        ),
        allowed_duration=(30.0, 60.0) if params.duration_variant == "short_30_60" else (60.0, 180.0) if params.duration_variant == "long_1_3_minutes" else None,
        generation_metadata={
            "experiment_id": params.experiment_id,
            "strategy_profile": params.strategy_profile,
            "duration_variant": params.duration_variant,
        },
    )
    artifacts.video = mpt_result.video
    artifacts.subtitle = mpt_result.subtitle
    artifacts.mpt_task_dir = mpt_result.task_dir
    validate_spoken_subtitle(mpt_result.subtitle)
    if not params.dry_run:
        artifacts.validation.video = validate_video(mpt_result.video, expected_aspect="9:16", require_audio=True, allowed_duration=(30.0, 60.0) if params.duration_variant == "short_30_60" else (60.0, 180.0) if params.duration_variant == "long_1_3_minutes" else None)
    store.set_artifacts(task_id, artifacts)

    plan = resolve_publish_plan("finance", snapshot.task, settings=settings)
    default_targets = [
        PublishTarget(platform="douyin", account=params.douyin_account),
        PublishTarget(platform="kuaishou", account=params.kuaishou_account),
    ]
    final, error = publish_generated_video(
        snapshot=snapshot,
        store=store,
        artifacts=artifacts,
        settings=settings,
        video=mpt_result.video,
        title=title,
        description=description,
        tags=params.tags,
        requested=plan["requested"],
        targets=plan["targets"] or default_targets,
        dry_run=params.dry_run,
    )
    store.finish(task_id, final, error)
