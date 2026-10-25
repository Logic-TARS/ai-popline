from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from typing import Any, Literal

from content_pipeline.errors import ConfigError
from content_pipeline.script_generation.client import OpenAICompatibleScriptClient
from content_pipeline.script_generation.models import (
    GenerationAttempt,
    ScriptFact,
    ScriptGenerationAudit,
    ScriptGenerationResult,
    ScriptPlan,
    StructuredFacts,
)
from content_pipeline.script_generation.policy import ATTRIBUTION_MARKERS, QUALITY_POLICY_VERSION, get_policy
from content_pipeline.script_generation.prompts import (
    AI_PROMPT_VERSION,
    DRAFT_PROMPT_VERSION,
    FINANCE_PROMPT_VERSION,
    PLAN_PROMPT_VERSION,
    REPAIR_PROMPT_VERSION,
    build_draft_prompts,
    build_plan_prompts,
    build_repair_prompts,
)
from content_pipeline.script_generation.quality import extract_number_tokens, validate_script
from content_pipeline.settings import Settings

__all__ = ["ScriptGenerationService", "build_structured_facts", "validate_plan"]

FailurePolicy = Literal["fail", "validated_rule_fallback"]
ContentKind = Literal["ai_briefing", "finance", "finance_multi_asset"]
QualityGate = Literal["strict", "soft"]
_NON_FACT_KEYS = {"fallback_script", "fallback_error", "prompt_version", "script_mode", "script_writer"}


class ScriptGenerationService:
    def __init__(self, settings: Settings, *, client: OpenAICompatibleScriptClient | None = None):
        self.client = client or OpenAICompatibleScriptClient(settings)

    def generate(
        self,
        *,
        content_type: ContentKind,
        material: dict[str, Any],
        fallback_script: str | None,
        failure_policy: FailurePolicy = "fail",
        repair_attempts: int = 1,
        gate: QualityGate = "strict",
        strategy_profile: str | None = None,
        duration_variant: str = "legacy_90_seconds",
        on_attempt: Callable[[GenerationAttempt], None] | None = None,
    ) -> ScriptGenerationResult:
        facts = build_structured_facts(content_type, material)
        version = AI_PROMPT_VERSION if content_type == "ai_briefing" else FINANCE_PROMPT_VERSION
        attempts: list[GenerationAttempt] = []
        plan: ScriptPlan | None = None
        last_error: Exception | None = None
        failed_stage: Literal["plan", "draft", "repair"] = "plan"
        recorded_failure = False
        try:
            fingerprints = {}
            for plan_index in range(2):
                system, user = build_plan_prompts(
                    facts,
                    strategy_profile=strategy_profile,
                    duration_variant=duration_variant,
                    retry_error=str(last_error) if last_error else None,
                )
                fingerprints[f"plan_{plan_index}"] = _prompt_fingerprint(system, user)
                raw_plan = self.client.complete(
                    system_prompt=system,
                    user_prompt=user,
                    temperature=0.2,
                    response_format={"type": "json_object"},
                    label=f"{content_type} script plan",
                )
                try:
                    plan = _parse_plan(raw_plan)
                    validate_plan(plan, facts)
                except ConfigError as exc:
                    last_error = exc
                    _append_attempt(
                        attempts,
                        GenerationAttempt(
                            stage="plan",
                            index=plan_index,
                            prompt_version=PLAN_PROMPT_VERSION,
                            output=raw_plan,
                            error=str(exc),
                        ),
                        on_attempt,
                    )
                    recorded_failure = plan_index == 1
                    if plan_index == 0:
                        continue
                    raise
                _append_attempt(
                    attempts,
                    GenerationAttempt(
                        stage="plan", index=plan_index, prompt_version=PLAN_PROMPT_VERSION, output=raw_plan
                    ),
                    on_attempt,
                )
                break
            failed_stage = "draft"
            system, user = build_draft_prompts(
                facts, plan, strategy_profile=strategy_profile, duration_variant=duration_variant
            )
            fingerprints["draft"] = _prompt_fingerprint(system, user)
            script = _clean_script(
                self.client.complete(
                    system_prompt=system,
                    user_prompt=user,
                    temperature=0.4,
                    label=f"{content_type} script draft",
                )
            )
            report = validate_script(script, facts, plan=plan, gate=gate, duration_variant=duration_variant)
            _append_attempt(
                attempts,
                GenerationAttempt(
                    stage="draft",
                    index=0,
                    prompt_version=DRAFT_PROMPT_VERSION,
                    output=script,
                    quality_report=report,
                ),
                on_attempt,
            )
            for index in range(1, repair_attempts + 1):
                if report.passed and not report.warnings:
                    break
                failed_stage = "repair"
                issues = [issue.model_dump(mode="json") for issue in (report.errors or report.warnings)]
                system, user = build_repair_prompts(
                    facts,
                    plan,
                    script,
                    issues,
                    strategy_profile=strategy_profile,
                    duration_variant=duration_variant,
                )
                fingerprints[f"repair_{index}"] = _prompt_fingerprint(system, user)
                script = _clean_script(
                    self.client.complete(
                        system_prompt=system,
                        user_prompt=user,
                        temperature=0.25,
                        label=f"{content_type} script repair",
                    )
                )
                report = validate_script(script, facts, plan=plan, gate=gate, duration_variant=duration_variant)
                _append_attempt(
                    attempts,
                    GenerationAttempt(
                        stage="repair",
                        index=index,
                        prompt_version=REPAIR_PROMPT_VERSION,
                        output=script,
                        quality_report=report,
                    ),
                    on_attempt,
                )
            if report.passed:
                mode = "llm_repaired" if attempts[-1].stage == "repair" else "llm"
                return ScriptGenerationResult(
                    script=script,
                    audit=ScriptGenerationAudit(
                        generation_mode=mode,
                        prompt_version=version,
                        plan=plan,
                        attempts=attempts,
                        quality_report=report,
                        quality_policy_version=QUALITY_POLICY_VERSION,
                        plan_prompt_version=PLAN_PROMPT_VERSION,
                        draft_prompt_version=DRAFT_PROMPT_VERSION,
                        repair_prompt_version=REPAIR_PROMPT_VERSION,
                        model_name=getattr(self.client, "model", None),
                        prompt_fingerprints=fingerprints,
                        within_recommended_length=_within_recommended(report.character_count, content_type, duration_variant),
                        strategy_profile=strategy_profile,
                        duration_variant=duration_variant,
                    ),
                )
            last_error = ConfigError(_quality_error(report))
        except Exception as exc:
            last_error = exc if str(exc) else last_error or exc
            if not recorded_failure:
                _append_attempt(
                    attempts,
                    GenerationAttempt(
                        stage=failed_stage,
                        index=sum(attempt.stage == failed_stage for attempt in attempts),
                        prompt_version=_stage_version(failed_stage),
                        error=str(exc),
                    ),
                    on_attempt,
                )
        if failure_policy == "validated_rule_fallback" and fallback_script:
            fallback = _clean_script(fallback_script)
            report = validate_script(fallback, facts, plan=None, gate=gate, duration_variant=duration_variant)
            _append_attempt(
                attempts,
                GenerationAttempt(
                    stage="fallback",
                    index=0,
                    prompt_version=version,
                    output=fallback,
                    quality_report=report,
                ),
                on_attempt,
            )
            if report.passed:
                return ScriptGenerationResult(
                    script=fallback,
                    audit=ScriptGenerationAudit(
                        generation_mode="validated_rule_fallback",
                        prompt_version=version,
                        plan=plan,
                        attempts=attempts,
                        quality_report=report,
                        fallback_reason=str(last_error) if last_error else None,
                        quality_policy_version=QUALITY_POLICY_VERSION,
                        plan_prompt_version=PLAN_PROMPT_VERSION,
                        draft_prompt_version=DRAFT_PROMPT_VERSION,
                        repair_prompt_version=REPAIR_PROMPT_VERSION,
                        model_name=getattr(self.client, "model", None),
                        prompt_fingerprints=locals().get("fingerprints", {}),
                        within_recommended_length=_within_recommended(report.character_count, content_type, duration_variant),
                        strategy_profile=strategy_profile,
                        duration_variant=duration_variant,
                    ),
                )
        raise ConfigError(f"{content_type} script generation failed: {last_error}") from last_error

    def validate_rule(
        self,
        *,
        content_type: ContentKind,
        material: dict[str, Any],
        script: str,
        gate: QualityGate = "strict",
        strategy_profile: str | None = None,
        duration_variant: str = "legacy_90_seconds",
    ) -> ScriptGenerationResult:
        facts = build_structured_facts(content_type, material)
        cleaned = _clean_script(script)
        report = validate_script(cleaned, facts, plan=None, gate=gate, duration_variant=duration_variant)
        if not report.passed:
            raise ConfigError(_quality_error(report))
        version = AI_PROMPT_VERSION if content_type == "ai_briefing" else FINANCE_PROMPT_VERSION
        attempt = GenerationAttempt(
            stage="fallback", index=0, prompt_version=version, output=cleaned, quality_report=report
        )
        return ScriptGenerationResult(
            script=cleaned,
            audit=ScriptGenerationAudit(
                generation_mode="rule",
                prompt_version=version,
                attempts=[attempt],
                quality_report=report,
                quality_policy_version=QUALITY_POLICY_VERSION,
                within_recommended_length=_within_recommended(report.character_count, content_type, duration_variant),
                strategy_profile=strategy_profile,
                duration_variant=duration_variant,
            ),
        )


def validate_plan(plan: ScriptPlan, facts: StructuredFacts) -> None:
    fact_ids = {fact.id for fact in facts.facts}
    declared = set(plan.required_facts)
    if plan.main_fact_id not in declared:
        raise ConfigError("script plan main_fact_id must also appear in required_facts")
    unknown = declared - fact_ids
    if unknown:
        raise ConfigError(f"script plan references unknown fact ids: {', '.join(sorted(unknown))}")
    required = {fact.id for fact in facts.facts if fact.required}
    missing = required - declared
    if missing:
        raise ConfigError(f"script plan omitted required fact ids: {', '.join(sorted(missing))}")
    section_ids = {fact_id for ids in plan.section_fact_ids.values() for fact_id in ids}
    if section_ids:
        unknown_sections = section_ids - fact_ids
        if unknown_sections:
            raise ConfigError(f"script plan sections reference unknown fact ids: {', '.join(sorted(unknown_sections))}")
        if plan.main_fact_id not in section_ids:
            raise ConfigError("script plan sections must include main_fact_id")
        if not declared <= section_ids:
            raise ConfigError("script plan sections must cover every selected fact id")
    if facts.content_type != "ai_briefing":
        risk_ids = {fact.id for fact in facts.facts if fact.kind == "risk" and fact.required}
        if risk_ids and plan.section_fact_ids and not risk_ids <= set(plan.section_fact_ids.get("close", [])):
            raise ConfigError("script plan must place required finance risk facts in close")


def build_structured_facts(content_type: ContentKind, material: dict[str, Any]) -> StructuredFacts:
    clean = {key: value for key, value in material.items() if key not in _NON_FACT_KEYS}
    if content_type == "ai_briefing":
        facts = _ai_facts(clean)
    elif content_type == "finance":
        facts = _finance_facts(clean)
    else:
        facts = _multi_asset_facts(clean)
    if not facts:
        raise ConfigError(f"{content_type} material contains no usable facts")
    return StructuredFacts(content_type=content_type, facts=facts)


def _ai_facts(material: dict[str, Any]) -> list[ScriptFact]:
    facts: list[ScriptFact] = []
    summary = str(material.get("summary") or material.get("title") or "").strip()
    if summary:
        facts.append(_fact("ai.summary", "summary", summary, required=True, kind="summary"))
    date_text = str(material.get("date_text") or material.get("date") or "").strip()
    if date_text:
        facts.append(_fact("ai.date", "context", date_text, kind="context"))
    sections = (
        ("focus", "focus_sentences", "focus_titles", True),
        ("frontier", "frontier_sentences", "frontier_titles", False),
        ("quick", "quick_items", None, False),
        ("conclusion", "conclusion_sentences", None, False),
    )
    for section, key, title_key, required_first in sections:
        titles = material.get(title_key, []) if title_key else []
        for index, value in enumerate(material.get(key, []) or [], start=1):
            sentence = str(value).strip()
            title = str(titles[index - 1]).strip() if index <= len(titles) else ""
            text = "：".join(part for part in (title, sentence) if part)
            if text:
                facts.append(
                    _fact(f"ai.{section}.{index}", section, text, required=required_first and index <= 2, kind="ai")
                )
    return facts


def _finance_facts(material: dict[str, Any]) -> list[ScriptFact]:
    facts: list[ScriptFact] = []
    summary = str(material.get("summary") or material.get("index_summary") or "").strip()
    if summary:
        facts.append(_fact("finance.summary", "summary", summary, required=True, kind="summary"))
    publication_date = str(material.get("publication_date") or "").strip()
    if publication_date:
        facts.append(_fact("finance.date", "context", publication_date, kind="context"))
    for index, row in enumerate(material.get("indices", []) or [], start=1):
        if not isinstance(row, (list, tuple)) or not row:
            continue
        subject = str(row[0]).strip()
        cells = [str(cell).strip() for cell in row if str(cell).strip()]
        text = " ".join(cells)
        if len(cells) >= 3 and "点" not in cells[1]:
            text = f"{cells[0]} {cells[1]}点 {cells[2]}"
        facts.append(
            _fact(
                f"finance.index.{index}",
                "indices",
                text,
                required=True,
                kind="index",
                subject=subject,
                direction=_direction(text),
                unit=_market_unit(text, default="point"),
            )
        )
    for index, row in enumerate(material.get("sector_rows", []) or [], start=1):
        if not isinstance(row, (list, tuple)) or not row:
            continue
        subject = str(row[0]).strip()
        text = " ".join(str(cell).strip() for cell in row if str(cell).strip())
        facts.append(
            _fact(
                f"finance.sector.{index}",
                "sectors",
                text,
                required=index <= 2,
                kind="sector",
                subject=subject,
                direction=_direction(text),
                unit=_market_unit(text, default="percent"),
            )
        )
    sector_text = str(material.get("sector_text") or "").strip()
    if sector_text:
        facts.append(_fact("finance.sector.context", "sectors", sector_text, kind="context"))
    for key, section, kind in (
        ("keywords", "signals", "context"),
        ("strategy", "strategy", "strategy"),
        ("valuation", "valuation", "context"),
        ("cross_asset", "cross_asset", "asset"),
    ):
        values = material.get(key) or []
        if isinstance(values, str):
            values = [values]
        for index, value in enumerate(values, start=1):
            text = str(value).strip()
            if text:
                facts.append(_fact(f"finance.{section}.{index}", section, text, kind=kind))
    facts.extend(_risk_facts(material, "finance"))
    return facts


def _multi_asset_facts(material: dict[str, Any]) -> list[ScriptFact]:
    facts: list[ScriptFact] = []
    modules = material.get("modules") if isinstance(material.get("modules"), dict) else material
    for index, (name, value) in enumerate(modules.items(), start=1):
        if name in _NON_FACT_KEYS or name in {"risk_note", "disclaimer"}:
            continue
        text = _compact_text(value)
        if text:
            facts.append(
                _fact(
                    f"asset.{_slug(name)}.{index}",
                    str(name)[:80],
                    text,
                    required=False,
                    kind="asset",
                    subject=str(name)[:120],
                )
            )
    facts.extend(_risk_facts(material, "asset"))
    if not any(fact.kind == "asset" for fact in facts):
        raise ConfigError("finance_multi_asset material contains no usable asset modules")
    return facts


def _risk_facts(material: dict[str, Any], prefix: str) -> list[ScriptFact]:
    result = []
    for key in ("risk_note", "disclaimer"):
        text = str(material.get(key) or "").strip()
        if text:
            result.append(_fact(f"{prefix}.{key}", "risk", text, required=True, kind="risk"))
    return result


def _fact(
    fact_id: str,
    section: str,
    text: str,
    *,
    required: bool = False,
    kind: str = "context",
    subject: str | None = None,
    direction: str | None = None,
    unit: str | None = None,
) -> ScriptFact:
    return ScriptFact(
        id=fact_id,
        section=section,
        text=text,
        required=required,
        numbers=sorted(extract_number_tokens(text))[:60],
        entities=_entities(text)[:20],
        status_markers=_status_markers(text),
        kind=kind,
        subject=subject,
        direction=direction,
        unit=unit,
        point_value=_point_value(text),
        change_value=_change_value(text),
        attribution_markers=[marker for marker in ATTRIBUTION_MARKERS if marker in text],
    )


def _entities(text: str) -> list[str]:
    candidates = re.findall(r"[A-Za-z][A-Za-z0-9.+-]{1,}|[\u4e00-\u9fff]{2,10}", text)
    ignored = {"今日焦点", "前沿动态", "一句话快讯", "每日简报", "人工智能", "今天", "方面", "市场"}
    return list(dict.fromkeys(item for item in candidates if item not in ignored))


def _status_markers(text: str) -> list[str]:
    groups = {
        "planned": ("计划", "拟", "将", "预计", "准备"),
        "possible": ("可能", "有望", "或将", "传闻", "据称"),
        "testing": ("测试", "试点", "预览", "内测"),
        "completed": ("已发布", "已上线", "已确认", "正式发布", "正式上线"),
    }
    return [name for name, markers in groups.items() if any(marker in text for marker in markers)]


def _direction(text: str) -> str | None:
    if re.search(r"(?:下跌|跌|回落|走低|负增长)|-\d", text):
        return "down"
    if re.search(r"(?:上涨|涨|走高|上行|正增长)|\+\d", text):
        return "up"
    if any(marker in text for marker in ("持平", "平收")):
        return "flat"
    return None


def _market_unit(text: str, *, default: str) -> str:
    if "百分点" in text:
        return "percentage_point"
    if "%" in text or "百分比" in text:
        return "percent"
    if "点" in text:
        return "point"
    return default


_MARKET_NUMBER = re.compile(
    r"(?<![\d.,])(?P<value>[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)"
    r"(?![\d.,])\s*(?P<unit>%|个百分点|点)?"
)


def _is_change_number(text: str, match: re.Match[str]) -> bool:
    return match.group("value").startswith(("+", "-")) or bool(
        re.search(r"(?:涨|跌|回落|走高|走低|上行|下行|升|降)(?:幅)?(?:为|了)?\s*$", text[: match.start()])
    )


def _point_value(text: str) -> str | None:
    for match in _MARKET_NUMBER.finditer(text):
        if match.group("unit") == "点" and not _is_change_number(text, match):
            return match.group("value")
    return None


def _change_value(text: str) -> str | None:
    matches = list(_MARKET_NUMBER.finditer(text))
    for match in matches:
        if match.group("unit") in {"%", "个百分点"}:
            return match.group("value") + match.group("unit")
    for match in matches:
        if _is_change_number(text, match):
            return match.group("value") + (match.group("unit") or "")
    for match in matches:
        if (
            not match.group("unit")
            and (match.start() == 0 or text[match.start() - 1].isspace())
            and (match.end("value") == len(text) or text[match.end("value")].isspace())
        ):
            return match.group("value")
    return None


def _compact_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()[:2000]
    if value in (None, {}, []):
        return ""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))[:2000]


def _slug(value: Any) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value).lower()).strip("-")
    return slug[:40] or "module"


def _parse_plan(raw: str) -> ScriptPlan:
    value = raw.strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*|\s*```$", "", value, flags=re.IGNORECASE)
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        start = value.find("{")
        if start < 0:
            raise ConfigError("script plan LLM returned invalid JSON") from None
        try:
            payload, _end = json.JSONDecoder().raw_decode(value[start:])
        except json.JSONDecodeError as exc:
            raise ConfigError("script plan LLM returned invalid JSON") from exc
    if isinstance(payload, dict):
        payload = {key: value for key, value in payload.items() if key in ScriptPlan.model_fields}
        risk_close = payload.get("risk_close")
        if isinstance(risk_close, str):
            payload["risk_close"] = risk_close[:400]
        sections = payload.get("sections")
        if isinstance(sections, list) and all(isinstance(section, dict) for section in sections):
            payload["sections"] = [
                str(
                    section.get("title")
                    or section.get("description")
                    or section.get("purpose")
                    or section.get("key")
                    or "段落"
                )
                for section in sections
            ]
            if not payload.get("section_fact_ids"):
                payload["section_fact_ids"] = {
                    str(section.get("key") or ""): list(section.get("fact_ids") or [])
                    for section in sections
                    if str(section.get("key") or "") and section.get("fact_ids")
                }
    try:
        return ScriptPlan.model_validate(payload)
    except ValueError as exc:
        raise ConfigError("script plan LLM returned invalid JSON") from exc


def _clean_script(script: str) -> str:
    return re.sub(r"[ \t]{2,}", " ", script).strip()


def _prompt_fingerprint(system: str, user: str) -> str:
    return hashlib.sha256(f"{system}\n{user}".encode()).hexdigest()


def _within_recommended(character_count: int, content_type: ContentKind, duration_variant: str = "legacy_90_seconds") -> bool:
    minimum, maximum = get_policy(content_type, duration_variant).recommended_length
    return minimum <= character_count <= maximum


def _quality_error(report) -> str:
    return "; ".join(f"{issue.code}: {issue.message}" for issue in report.errors)


def _append_attempt(
    attempts: list[GenerationAttempt],
    attempt: GenerationAttempt,
    callback: Callable[[GenerationAttempt], None] | None,
) -> None:
    attempts.append(attempt)
    if callback:
        callback(attempt)


def _stage_version(stage: str) -> str:
    return {
        "plan": PLAN_PROMPT_VERSION,
        "draft": DRAFT_PROMPT_VERSION,
        "repair": REPAIR_PROMPT_VERSION,
    }[stage]

