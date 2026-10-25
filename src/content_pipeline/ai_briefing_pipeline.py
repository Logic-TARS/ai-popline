from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .errors import ConfigError, ExternalToolError, MediaValidationError
from .media_validation import validate_images, validate_video
from .metadata_generation.audit import generate_pipeline_metadata
from .models import AiBriefingParams, CoverParams, ErrorCode, JobStatus, PipelineStep, PublishTarget
from .pipelines.registry import PipelineContext, PipelineMeta, register
from .publish_policy import resolve_publish_plan
from .publishing import publish_generated_video
from .script_generation import ScriptGenerationService
from .script_generation.audit import persist_script_audit, record_script_attempt_event
from .tools.cover_forge_client import run_cover_forge_adapter
from .tools.local_cover import generate_local_cover
from .tools.narrated_mpt_client import call_narrated_mpt, validate_spoken_subtitle

__all__ = [
    "acquire_run_lock",
    "build_90_second_briefing_script",
    "build_briefing_script_material",
    "build_publish_title",
    "build_video_title",
    "normalize_date",
    "normalize_handoff",
    "release_run_lock",
    "run_ai_briefing_pipeline",
    "validate_ai_briefing_narration",
    "wait_for_daily_inputs",
]


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def normalize_date(value: str, now: datetime | None = None) -> str:
    compact = (now or datetime.now()).strftime("%Y%m%d") if value == "auto" else value.replace("-", "")
    try:
        datetime.strptime(compact, "%Y%m%d")
    except ValueError as exc:
        raise ConfigError(f"ai briefing date must be auto, YYYYMMDD, or YYYY-MM-DD: {value}") from exc
    return compact


def wait_for_daily_inputs(source_root: Path, date: str, wait_seconds: int) -> tuple[Path, Path, Path, Path]:
    day_dir = source_root.expanduser().resolve() / date
    handoff = day_dir / "video_handoff.json"
    article = day_dir / "article.md"
    text = day_dir / "article.txt"
    required = (handoff, article, text)
    if not all(path.is_file() for path in required) and wait_seconds:
        time.sleep(wait_seconds)
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise ConfigError("missing_input: " + ", ".join(missing))
    return day_dir, handoff, article, text


def _clean_text(value: str) -> str:
    value = re.sub(r"!\[[^]]*]\([^)]*\)", "", value)
    value = re.sub(r"\[([^]]+)]\([^)]*\)", r"\1", value)
    value = re.sub(r"<[^>]+>", "", value)
    value = value.replace("~~", "")
    value = re.sub(r"[`*_>#]", "", value)
    value = re.sub(r"^[\s\-+•🔥🧠⚠🏢📱🚀🌏🤖🎮💼]+", "", value.strip())
    value = value.replace("\ufe0f", "")
    return re.sub(r"\s+", " ", value).strip(" ，；")


def _frontmatter(markdown: str) -> dict[str, str]:
    match = re.match(r"\A---\s*\n(.*?)\n---\s*\n", markdown, re.DOTALL)
    if not match:
        return {}
    values: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            values[key.strip()] = _clean_text(value)
    return values


def _section(markdown: str, heading: str) -> str:
    match = re.search(
        rf"(?ms)^##\s+[^\n]*{re.escape(heading)}[^\n]*\n(.*?)(?=^##\s+|\Z)",
        markdown,
    )
    return match.group(1).strip() if match else ""


def _subheadings(section: str, limit: int) -> list[str]:
    return [_clean_text(value) for value in re.findall(r"(?m)^###\s+(.+)$", section)[:limit]]


def _first_sentences(section: str, limit: int) -> list[str]:
    cleaned_lines = []
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "---", "![", "|")):
            continue
        cleaned = _clean_text(stripped)
        if cleaned:
            cleaned_lines.extend(part.strip() for part in re.split(r"(?<=[。！？])", cleaned) if part.strip())
        if len(cleaned_lines) >= limit:
            break
    return cleaned_lines[:limit]


def _trim_complete(text: str, limit: int = 500) -> str:
    if len(text) <= limit:
        return text
    clipped = text[:limit]
    boundary = max(clipped.rfind(mark) for mark in "。！？；")
    if boundary >= 300:
        return clipped[: boundary + 1].rstrip("；") + "。"
    return clipped.rstrip("，；： ") + "。"


def build_publish_title(title: str, max_chars: int = 20) -> str:
    cleaned = _clean_text(title)
    if len(cleaned) <= max_chars:
        return cleaned
    for separator in ("：", ":", "，", ",", "。", "！", "？"):
        first = cleaned.split(separator, 1)[0].strip()
        if 6 <= len(first) <= max_chars:
            return first
    return cleaned[:max_chars].rstrip("，；：、 -")


def build_video_title(title: str) -> str:
    return build_publish_title(title, max_chars=16)


def build_briefing_script_material(markdown: str, date: str, *, duration_variant: str = "legacy_90_seconds") -> dict[str, Any]:
    meta = _frontmatter(markdown)
    focus = _section(markdown, "今日焦点")
    frontier = _section(markdown, "前沿动态")
    quick = _section(markdown, "一句话快讯")
    conclusion = _section(markdown, "结语")
    date_text = f"{date[:4]}年{date[4:6]}月{date[6:]}日"
    focus_titles = _subheadings(focus, 3)
    frontier_titles = _subheadings(frontier, 4)
    quick_items = [_clean_text(line) for line in quick.splitlines() if line.strip().startswith("-")][:4]
    material = {
        "date": date,
        "date_text": date_text,
        "title": meta.get("标题", ""),
        "summary": meta.get("摘要", ""),
        "focus_titles": focus_titles,
        "focus_sentences": _first_sentences(focus, 5),
        "frontier_titles": frontier_titles,
        "frontier_sentences": _first_sentences(frontier, 5),
        "quick_items": quick_items,
        "conclusion_sentences": _first_sentences(conclusion, 2),
    }
    material["fallback_script"] = _build_rule_briefing_script(material, duration_variant=duration_variant)
    return material


def _build_rule_briefing_script(material: dict[str, Any], *, duration_variant: str = "legacy_90_seconds") -> str:
    parts: list[str] = []
    summary = str(material.get("summary") or "")
    if summary:
        parts.append(summary.rstrip("。") + "。")
    focus_titles = [str(value) for value in material.get("focus_titles", [])[:2]]
    parts.append(f"{material['date_text']}每日AI简报。")
    if focus_titles:
        parts.append("今日焦点包括" + "；".join(focus_titles) + "。")
    frontier_titles = [str(value) for value in material.get("frontier_titles", [])[:3]]
    if frontier_titles:
        parts.append("前沿动态还包括" + "；".join(frontier_titles) + "。")
    quick_items = [str(value) for value in material.get("quick_items", [])[:2]]
    if quick_items:
        parts.append("另外，" + "；".join(quick_items) + "。")
    conclusion_sentences = [str(value) for value in material.get("conclusion_sentences", [])[:1]]
    if conclusion_sentences:
        parts.append(conclusion_sentences[0].rstrip("。") + "。")

    script = "".join(dict.fromkeys(part for part in parts if part))
    minimum, maximum = {"short_30_60": (180, 320), "long_1_3_minutes": (500, 800)}.get(duration_variant, (350, 500))
    if len(script) < minimum:
        supplements = [str(value) for value in material.get("focus_sentences", [])]
        supplements += [str(value) for value in material.get("frontier_sentences", [])]
        for sentence in supplements:
            normalized = sentence.rstrip("。") + "。"
            if normalized not in script:
                script += normalized
            if len(script) >= minimum + 40:
                break
    script = re.sub(r"[。！？；]{2,}", "。", script)
    return _trim_complete(script, maximum)


def build_90_second_briefing_script(markdown: str, date: str) -> str:
    return str(build_briefing_script_material(markdown, date)["fallback_script"])


def _keywords_from_material(material: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("title", "summary"):
        value = material.get(key)
        if isinstance(value, str):
            values.append(value)
    for key in ("focus_titles", "frontier_titles", "quick_items"):
        values.extend(str(item) for item in material.get(key, []) if item)
    text = " ".join(values)
    candidates = re.findall(r"[A-Za-z][A-Za-z0-9.+-]{1,}|[\u4e00-\u9fff]{2,12}", text)
    ignored = {"今日焦点", "前沿动态", "一句话快讯", "人工智能", "每日简报", "方面", "继续", "要求"}
    keywords: list[str] = []
    for candidate in candidates:
        if candidate in ignored or candidate in keywords:
            continue
        keywords.append(candidate)
        if len(keywords) >= 12:
            break
    return keywords


def validate_ai_briefing_narration(script: str, material: dict[str, Any], *, enforce_length: bool = True) -> None:
    normalized = re.sub(r"\s+", "", script)
    if enforce_length and not 350 <= len(normalized) <= 500:
        raise ConfigError(f"AI briefing LLM narration must contain 350-500 characters; generated {len(normalized)}")
    if re.search(r"!\[[^]]*]\([^)]*\)|^#{1,6}\s+|^[\-*+]\s+", script, re.MULTILINE):
        raise ConfigError("AI briefing LLM narration contains markdown")
    if re.search(r"(?:^|[：:])(?:~|[A-Za-z]:)?[/\\].+\.(?:md|txt|json|srt)", normalized, re.IGNORECASE):
        raise ConfigError("AI briefing LLM narration contains a file reference")
    if any(marker in script for marker in ("以下是", "根据素材", "我将", "口播稿如下", "Markdown")):
        raise ConfigError("AI briefing LLM narration contains explanatory boilerplate")
    if not normalized.endswith(("。", "！", "？")):
        raise ConfigError("AI briefing LLM narration must end with a complete Chinese sentence")
    if re.search(r"[，。！？；：、]{3,}", normalized):
        raise ConfigError("AI briefing LLM narration contains repeated punctuation")
    keywords = _keywords_from_material(material)
    if keywords:
        hits = sum(1 for keyword in keywords if keyword in script)
        required = min(3, len(keywords))
        if hits < required:
            raise ConfigError("AI briefing LLM narration dropped too many source keywords")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sau_file_hash(path: Path) -> str:
    """Mirror Social Auto Upload's partial SHA-256 dedup hash.

    SAU intentionally hashes the file size plus its first and last 1 MiB.
    Keep both this audit hash and the full SHA-256 in the external status so
    a later audit does not confuse the two different, valid identities.
    """
    chunk_size = 1_048_576
    size = path.stat().st_size
    digest = hashlib.sha256()
    digest.update(size.to_bytes(8, "big"))
    if size <= chunk_size:
        digest.update(path.read_bytes())
    else:
        with path.open("rb") as handle:
            digest.update(handle.read(chunk_size))
        with path.open("rb") as handle:
            handle.seek(-chunk_size, os.SEEK_END)
            digest.update(handle.read(chunk_size))
    return digest.hexdigest()


def normalize_handoff(raw: dict[str, Any], *, date: str, handoff_path: Path, day_dir: Path) -> dict[str, Any]:
    title = raw.get("title")
    if not title:
        raise ConfigError("schema_invalid: handoff title is required")
    article_path = Path(raw.get("article_path") or day_dir / "article.md").expanduser().resolve()
    text_path = Path(raw.get("text_path") or day_dir / "article.txt").expanduser().resolve()
    output_dir = Path(raw.get("output_dir") or day_dir / "video").expanduser().resolve()
    resolved_day = day_dir.resolve()
    if not output_dir.is_relative_to(resolved_day):
        raise ConfigError(f"schema_invalid: output_dir must stay inside {resolved_day}")
    for path in (article_path, text_path):
        if not path.is_file():
            raise ConfigError(f"missing_input: {path}")
    compact = date.replace("-", "")
    return {
        **raw,
        "schema_version": raw.get("schema_version", "1.0"),
        "handoff_id": raw.get("handoff_id", f"ai-{compact}-video-v1"),
        "producer_profile": raw.get("producer_profile", "ai_cron"),
        "consumer_profile": raw.get("consumer_profile", "sampo"),
        "pipeline": raw.get("pipeline", "ai"),
        "date": date,
        "title": title,
        "article_path": str(article_path),
        "text_path": str(text_path),
        "output_dir": str(output_dir),
        "runner": "ai-pipeline",
        "handoff_path": str(handoff_path.resolve()),
    }


def _pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
        return result.returncode == 0 and f'"{pid}"' in result.stdout
    except (OSError, subprocess.SubprocessError):
        return False


def acquire_run_lock(path: Path, date: str) -> None:
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = {}
        if _pid_running(int(existing.get("pid") or 0)):
            raise ConfigError(f"already_running: ai briefing pipeline is active for {date}")
        path.unlink(missing_ok=True)
    payload = json.dumps({"pid": os.getpid(), "date": date, "runner": "ai-pipeline", "started_at": _now_iso()})
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise ConfigError(f"already_running: ai briefing pipeline is active for {date}") from exc
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(payload)


def release_run_lock(path: Path) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if int(payload.get("pid") or 0) == os.getpid():
            path.unlink(missing_ok=True)
    except (OSError, json.JSONDecodeError):
        pass


def _external_status(date: str, title: str, task_id: str, handoff: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "status": "running",
        "date": date,
        "title": title,
        "pipeline_task_id": task_id,
        "handoff_id": handoff.get("handoff_id"),
        "producer_profile": handoff.get("producer_profile"),
        "consumer_profile": handoff.get("consumer_profile"),
        "runner": "ai-pipeline",
        "started_at": _now_iso(),
        "updated_at": _now_iso(),
        "steps": {
            "handoff_read": True,
            "script_build": "pending",
            "cover_generation": "pending",
            "video_generation": "pending",
            "subtitle_check": "pending",
            "publish_douyin": "pending",
            "publish_kuaishou": "pending",
        },
    }


def _handoff_event(
    handoff: dict[str, Any], status: dict[str, Any], event: str, error: str | None = None
) -> dict[str, Any]:
    record = {
        "timestamp": _now_iso(),
        "event": event,
        "handoff_id": handoff.get("handoff_id"),
        "pipeline": "ai",
        "producer_profile": handoff.get("producer_profile"),
        "consumer_profile": handoff.get("consumer_profile"),
        "runner": "ai-pipeline",
        "pipeline_task_id": status.get("pipeline_task_id"),
        "status": status.get("status"),
    }
    if error:
        record["error"] = error
    return record


@register(
    "ai_briefing",
    meta=PipelineMeta(
        content_type="ai_briefing",
        description="Daily handoff -> script and cover generation -> narrated MPT -> optional publish",
        required_params=[],
        external_tools=["Cover-Forge", "MoneyPrinterTurbo"],
        publish_targets=["douyin", "kuaishou", "tencent"],
    ),
)
def run_ai_briefing_pipeline(ctx: PipelineContext) -> None:
    task_id = ctx.task_id
    snapshot = ctx.snapshot
    artifacts = ctx.artifacts
    store = ctx.store
    settings = ctx.settings
    params = AiBriefingParams.model_validate(ctx.route.params)
    date = normalize_date(params.date)
    source_root = params.source_dir or settings.ai_briefing_dir
    day_dir = source_root.expanduser().resolve() / date
    status_path = day_dir / "video_status.json"
    index_path = day_dir / "handoff_index.jsonl"
    lock_path = day_dir / "handoff.lock.json"
    status: dict[str, Any] = {"date": date, "runner": "ai-pipeline", "pipeline_task_id": task_id}
    handoff: dict[str, Any] = {"handoff_id": f"ai-{date}-video-v1", "producer_profile": "ai_cron"}

    try:
        store.mark_running(task_id, PipelineStep.SOURCE_SCAN)
        day_dir, handoff_path, article_path, text_path = wait_for_daily_inputs(
            source_root, date, params.handoff_wait_seconds
        )
        acquire_run_lock(lock_path, date)
        raw_handoff = json.loads(handoff_path.read_text(encoding="utf-8-sig"))
        handoff = normalize_handoff(raw_handoff, date=date, handoff_path=handoff_path, day_dir=day_dir)
        title = params.title or str(handoff["title"])
        status = _external_status(date, title, task_id, handoff)
        _write_json(status_path, status)
        _append_jsonl(index_path, _handoff_event(handoff, status, "accepted"))

        markdown = article_path.read_text(encoding="utf-8-sig")
        material = build_briefing_script_material(markdown, date, duration_variant=params.duration_variant or "long_1_3_minutes")
        generator = ScriptGenerationService(settings)
        try:
            if params.script_writer == "llm":
                generation = generator.generate(
                    content_type="ai_briefing",
                    material=material,
                    fallback_script=str(material["fallback_script"]),
                    failure_policy=params.script_failure_policy,
                    repair_attempts=params.script_repair_attempts,
                    duration_variant=params.duration_variant or "long_1_3_minutes",
                    strategy_profile=params.strategy_profile,
                    on_attempt=lambda attempt: record_script_attempt_event(store, task_id, attempt),
                )
            else:
                generation = generator.validate_rule(
                    content_type="ai_briefing",
                    material=material,
                    script=str(material["fallback_script"]),
                    duration_variant=params.duration_variant or "long_1_3_minutes",
                    strategy_profile=params.strategy_profile,
                )
        except Exception as exc:
            status["script_writer"] = "llm_failed" if params.script_writer == "llm" else "rule_failed"
            status["script_writer_error"] = str(exc)
            status["steps"]["script_build"] = "failed"
            status["updated_at"] = _now_iso()
            _write_json(status_path, status)
            store.event(task_id, "script_generation_failed", {"writer": params.script_writer, "error": str(exc)[:500]})
            raise ConfigError(f"AI briefing script generation failed: {exc}") from exc
        script = generation.script
        audit = generation.audit.model_dump(mode="json")
        job_dir = store.job_dir(task_id)
        script_path = job_dir / "ai_briefing_script.txt"
        legacy_audit_path = job_dir / "ai_briefing_script_audit.json"
        script_path.write_text(script, encoding="utf-8")
        _write_json(legacy_audit_path, audit)
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
        artifacts.source_document = article_path
        artifacts.narration_script = script
        artifacts.script_path = script_path
        artifacts.script_generation_audit = audit
        artifacts.script_audit_path = audit_paths["audit"]
        artifacts.script_plan_path = audit_paths["plan"]
        artifacts.script_attempts_path = audit_paths["attempts"]
        artifacts.script_quality_report_path = audit_paths["quality"]
        artifacts.handoff_path = handoff_path
        artifacts.external_status_path = status_path
        status["script_writer"] = generation.audit.generation_mode
        status["script_prompt_version"] = generation.audit.prompt_version
        status["script_attempts"] = len(generation.audit.attempts)
        status["script_quality_report"] = generation.audit.quality_report.model_dump(mode="json")
        status["steps"]["script_build"] = "ok"
        status["narration_chars"] = generation.audit.quality_report.character_count
        status["updated_at"] = _now_iso()
        _write_json(status_path, status)
        store.set_artifacts(task_id, artifacts)

        status["steps"]["metadata_generation"] = "running"
        _write_json(status_path, status)
        title, description = generate_pipeline_metadata(
            ctx,
            params=params,
            material=material,
            script=script,
            rule_title=title,
        )
        artifacts.final_title = title
        artifacts.final_description = description
        status["video_title"] = build_video_title(title)
        status["description"] = description
        status["steps"]["metadata_generation"] = "ok"
        _write_json(status_path, status)
        store.set_artifacts(task_id, artifacts)

        if params.dry_run:
            status["steps"]["cover_generation"] = "skipped"
            status["updated_at"] = _now_iso()
            _write_json(status_path, status)
        else:
            store.mark_running(task_id, PipelineStep.IMAGE)
            cover_path = job_dir / "cover" / "cover.png"
            cover_title = build_publish_title(title, max_chars=80)
            try:
                if not cover_title:
                    raise ConfigError("cover_generation_failed: AI briefing title is blank after normalization")
                cover_params = CoverParams(
                    title=cover_title,
                    size=params.cover_size,
                    template=params.cover_template,
                    title_position=params.cover_title_position,
                    background_image=params.cover_background_image,
                    background_scale=params.cover_background_scale,
                    background_position_x=params.cover_background_position_x,
                    background_position_y=params.cover_background_position_y,
                )
                cover_result = run_cover_forge_adapter(
                    params=cover_params,
                    output_path=cover_path,
                    settings=settings,
                )
                if not cover_result.ok:
                    fallback_result = generate_local_cover(params=cover_params, output_path=cover_path)
                    if fallback_result.ok:
                        status["cover_generator"] = "local"
                        status["cover_forge_error"] = cover_result.message
                        cover_result = fallback_result
                if not cover_result.ok:
                    raise ExternalToolError(
                        f"cover generation failed [{cover_result.code.value}]: "
                        f"{cover_result.message or 'unknown error'}"
                    )
                generated_cover = Path(str(cover_result.artifacts.get("cover_path", "")))
                if not generated_cover.is_file():
                    raise ExternalToolError(
                        f"Cover-Forge failed [{ErrorCode.NO_OUTPUT.value}]: cover output is missing"
                    )
                cover_validation = validate_images([generated_cover], 1)
                expected_dimensions = {
                    "landscape": (1920, 1080),
                    "portrait": (1080, 1440),
                    "story": (1080, 1920),
                }[params.cover_size]
                validated_cover = cover_validation.files[0]
                if (validated_cover.width, validated_cover.height) != expected_dimensions:
                    raise MediaValidationError(
                        f"generated cover dimensions {validated_cover.width}x{validated_cover.height} do not match "
                        f"{params.cover_size} {expected_dimensions[0]}x{expected_dimensions[1]}"
                    )
                artifacts.images = [generated_cover]
                artifacts.validation.images = cover_validation
                store.set_artifacts(task_id, artifacts)
                status["steps"]["cover_generation"] = "ok"
                status["cover_path"] = str(generated_cover)
                status["cover_sha256"] = _sha256(generated_cover)
                status["updated_at"] = _now_iso()
                _write_json(status_path, status)
            except Exception:
                status["steps"]["cover_generation"] = "failed"
                status["updated_at"] = _now_iso()
                _write_json(status_path, status)
                raise

        store.mark_running(task_id, PipelineStep.VIDEO)
        input_hashes = {
            "article.md": _sha256(article_path),
            "article.txt": _sha256(text_path),
        }
        video_title = build_video_title(title)
        status["video_title"] = video_title
        result = call_narrated_mpt(
            task_name=f"ai-briefing-{date}",
            title=video_title,
            script=script,
            output_dir=job_dir / "video",
            settings=settings,
            dry_run=params.dry_run,
            force_regenerate=params.force_regenerate,
            input_hashes=input_hashes,
            voice_rate=1.0 if (params.duration_variant or "legacy_90_seconds") == "legacy_90_seconds" else None,
            target_duration_seconds=45.0 if params.duration_variant == "short_30_60" else 120.0 if params.duration_variant == "long_1_3_minutes" else 90.0,
            allowed_duration=(30.0, 60.0) if params.duration_variant == "short_30_60" else (60.0, 180.0) if params.duration_variant == "long_1_3_minutes" else None,
            generation_metadata={"experiment_id": params.experiment_id, "strategy_profile": params.strategy_profile, "duration_variant": params.duration_variant},
        )
        validate_spoken_subtitle(result.subtitle)
        artifacts.video = result.video
        artifacts.subtitle = result.subtitle
        artifacts.mpt_task_dir = result.task_dir
        artifacts.manifest_path = result.manifest
        if not params.dry_run:
            artifacts.validation.video = validate_video(result.video, expected_aspect="9:16", require_audio=True, allowed_duration=(30.0, 60.0) if params.duration_variant == "short_30_60" else (60.0, 180.0) if params.duration_variant == "long_1_3_minutes" else None)
            status["video_sha256"] = _sha256(result.video)
            status["sau_file_hash"] = _sau_file_hash(result.video)
            legacy_output = Path(str(handoff["output_dir"]))
            legacy_output.mkdir(parents=True, exist_ok=True)
            legacy_video = legacy_output / "briefing.mp4"
            legacy_subtitle = legacy_output / "briefing.srt"
            legacy_manifest = legacy_output / "generation_manifest.json"
            shutil.copy2(result.video, legacy_video)
            shutil.copy2(result.subtitle, legacy_subtitle)
            shutil.copy2(result.manifest, legacy_manifest)
            artifacts.video = legacy_video
            artifacts.subtitle = legacy_subtitle
            artifacts.manifest_path = legacy_manifest
        store.set_artifacts(task_id, artifacts)
        status["steps"]["video_generation"] = "ok"
        status["steps"]["subtitle_check"] = "ok"
        status["video_path"] = str(artifacts.video)
        status["subtitle_path"] = str(artifacts.subtitle)
        status["manifest_path"] = str(artifacts.manifest_path)
        status["mpt_task_dir"] = str(result.task_dir)

        plan = resolve_publish_plan("ai_briefing", snapshot.task, settings=settings)
        publish_title = build_publish_title(title)
        status["publish_title"] = publish_title
        default_targets = [
            PublishTarget(platform="douyin", account=params.douyin_account),
            PublishTarget(platform="kuaishou", account=params.kuaishou_account),
        ]
        targets = plan["targets"] or default_targets
        if snapshot.task.publish_targets:
            provided = [(target.platform, target.account) for target in snapshot.task.publish_targets]
            expected = [(target.platform, target.account) for target in default_targets]
            if provided != expected:
                raise ConfigError(f"ai briefing publish targets must be exactly: {expected}")
        if not plan["requested"]:
            for key in ("publish_douyin", "publish_kuaishou"):
                if key in status["steps"]:
                    status["steps"][key] = "skipped"

        def _record_step(target: PublishTarget, publish_result: dict[str, Any]) -> None:
            status["steps"][f"publish_{target.platform}"] = "ok" if publish_result.get("success") else "failed"

        final, error = publish_generated_video(
            snapshot=snapshot,
            store=store,
            artifacts=artifacts,
            settings=settings,
            video=Path(artifacts.video).resolve(),
            title=publish_title,
            description=description,
            tags=params.tags,
            requested=plan["requested"],
            targets=targets,
            dry_run=params.dry_run,
            on_target_finished=_record_step,
        )

        store.set_artifacts(task_id, artifacts)
        status["publish_results"] = artifacts.publish_results
        status["status"] = "succeeded" if final == JobStatus.SUCCEEDED else "partial_failed"
        status["updated_at"] = _now_iso()
        _write_json(status_path, status)
        _append_jsonl(index_path, _handoff_event(handoff, status, status["status"]))
        store.finish(task_id, final, error)
    except Exception as exc:
        if "metadata_generation" in status.get("steps", {}):
            status["steps"]["metadata_generation"] = "failed"
        status["status"] = "failed"
        status["error"] = str(exc)
        status["updated_at"] = _now_iso()
        _write_json(status_path, status)
        _append_jsonl(index_path, _handoff_event(handoff, status, "failed", str(exc)))
        raise
    finally:
        release_run_lock(lock_path)
