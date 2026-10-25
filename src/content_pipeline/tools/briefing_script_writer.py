from __future__ import annotations

from typing import Any

from content_pipeline.script_generation.client import OpenAICompatibleScriptClient
from content_pipeline.script_generation.models import ScriptPlan
from content_pipeline.script_generation.prompts import (
    AI_PROMPT_VERSION,
    FINANCE_PROMPT_VERSION,
    build_draft_prompts,
)
from content_pipeline.script_generation.quality import validate_script
from content_pipeline.script_generation.service import build_structured_facts
from content_pipeline.settings import Settings

__all__ = [
    "FINANCE_PROMPT_VERSION",
    "PROMPT_VERSION",
    "rewrite_ai_briefing_script_with_llm",
    "rewrite_finance_script_with_llm",
]

PROMPT_VERSION = AI_PROMPT_VERSION


def rewrite_ai_briefing_script_with_llm(
    material: dict[str, Any],
    *,
    settings: Settings,
    timeout: int = 120,
) -> str:
    facts = build_structured_facts("ai_briefing", material)
    plan = _compatibility_plan(facts, material, finance=False)
    system_prompt, user_prompt = build_draft_prompts(facts, plan)
    script = OpenAICompatibleScriptClient(settings, timeout=timeout).complete(
        system_prompt=system_prompt, user_prompt=user_prompt, label="AI briefing script"
    )
    _validate_compatibility_output(script, facts, plan)
    return script


def rewrite_finance_script_with_llm(
    material: dict[str, Any],
    *,
    settings: Settings,
    timeout: int = 120,
) -> str:
    facts = build_structured_facts("finance", material)
    plan = _compatibility_plan(facts, material, finance=True)
    system_prompt, user_prompt = build_draft_prompts(facts, plan)
    script = OpenAICompatibleScriptClient(settings, timeout=timeout).complete(
        system_prompt=system_prompt, user_prompt=user_prompt, label="finance script"
    )
    _validate_compatibility_output(script, facts, plan)
    return script


def _validate_compatibility_output(script: str, facts, plan: ScriptPlan) -> None:
    report = validate_script(script, facts, plan=plan)
    if not report.passed:
        raise ValueError("; ".join(f"{issue.code}: {issue.message}" for issue in report.errors))


def _compatibility_plan(facts, material: dict[str, Any], *, finance: bool) -> ScriptPlan:
    summary = str(material.get("summary") or material.get("title") or "今日重点").strip()
    required_ids = [fact.id for fact in facts.facts if fact.required]
    main_fact_id = required_ids[0] if required_ids else facts.facts[0].id
    if main_fact_id not in required_ids:
        required_ids.insert(0, main_fact_id)
    return ScriptPlan(
        mainline=summary[:300] or "围绕素材中的关键变化展开",
        main_fact_id=main_fact_id,
        opening="直接交代日期和主线",
        sections=["主线与核心事实", "重要补充与影响", "风险边界与收束"],
        required_facts=required_ids,
        risk_close=str(material.get("risk_note") or material.get("disclaimer") or "")[:400] or None
        if finance
        else None,
        section_fact_ids={
            "opening": [main_fact_id],
            "main": required_ids,
            "support": [],
            "close": [item for item in required_ids if item.endswith(("risk_note", "disclaimer"))],
        },
    )
