from __future__ import annotations

import re
from typing import Literal

from content_pipeline.script_generation.models import StructuredFacts
from content_pipeline.script_generation.policy import ATTRIBUTION_MARKERS
from content_pipeline.script_generation.quality import (
    extract_number_tokens,
    extract_quantity_tokens,
    quantity_token_supported,
)

__all__ = [
    "ALWAYS_HARD_METADATA_ERRORS",
    "QUALITY_VERSION",
    "RULE_DESCRIPTIONS",
    "Stage",
    "validate_metadata",
]

Stage = Literal["title", "description"]
RULE_DESCRIPTIONS = {
    "ai_briefing": "今日AI简报",
    "finance": "每日基金日报，仅供参考，不构成投资建议",
}
QUALITY_VERSION = "metadata-quality-v1"

# Grounding checks, not precision checks: the text must not be empty and must be
# anchored in the facts and in the narration script. These stay blocking under the
# soft gate; every other code is downgraded to a warning.
ALWAYS_HARD_METADATA_ERRORS = frozenset({"empty_text", "fact_anchor_missing", "script_anchor_missing"})
_UNCERTAIN = ("计划", "拟", "预计", "即将", "可能", "有望", "或将", "传闻", "据称", "测试", "试点", "预览", "内测")
_COMPLETED = ("已发布", "已上线", "已开放", "已确认", "正式发布", "正式上线", "已经推出", "全面开放")
_ATTRIBUTION = ATTRIBUTION_MARKERS
_PROHIBITED = ("保证收益", "稳赚", "必涨", "必赚", "保本", "翻倍收益", "满仓", "梭哈", "无风险", "确定上涨")
_DATE_ONLY = re.compile(r"^[\s\d年月日\-/.:]+$")
_HIGH_RISK_ACTIONS = ("收购", "救市", "引发", "导致")
_COMMON_CHINESE_TERMS = {
    "今日",
    "今天",
    "每日",
    "简报",
    "市场",
    "模型",
    "人工",
    "智能",
    "投资",
    "建议",
    "风险",
    "能力",
    "相关",
    "方面",
    "发布",
    "上线",
    "计划",
    "可能",
    "已经",
    "目前",
    "继续",
    "观察",
    "开发者",
    "公司",
    "行业",
    "产品",
    "新模型",
    "重点",
    "关注",
    "实际",
    "测试",
    "进展",
    "用于",
    "具体",
    "应用",
    "后续",
    "情况",
    "开放",
}


def _normalize_quantities(text: str) -> str:
    return re.sub(r"\s+", " ", text).replace("，", ",")


def _unsupported_quantities(text: str, candidates) -> bool:
    return any(not quantity_token_supported(token, candidates) for token in extract_quantity_tokens(text))


def _validate_market_binding(text: str, facts: StructuredFacts, errors: list[str]) -> None:
    market = [fact for fact in facts.facts if fact.kind in {"index", "sector"} and fact.subject]
    if not market:
        return
    subjects = {fact.subject for fact in market}
    subject_pattern = re.compile("|".join(re.escape(subject) for subject in sorted(subjects, key=len, reverse=True)))
    normalized = _normalize_quantities(text)
    foreign: dict[str, tuple[set[str], set[str]]] = {}
    for subject in subjects:
        own_text = _normalize_quantities(
            " ".join(fact.text.replace(fact.subject, "") for fact in market if fact.subject == subject)
        )
        other_text = _normalize_quantities(
            " ".join(fact.text.replace(fact.subject, "") for fact in market if fact.subject != subject)
        )
        foreign[subject] = (
            extract_number_tokens(other_text) - extract_number_tokens(own_text),
            extract_quantity_tokens(other_text) - extract_quantity_tokens(own_text),
        )
    for sentence in re.split(r"[。！？；;!?\n]", normalized):
        mentions = list(subject_pattern.finditer(sentence))
        for index, mention in enumerate(mentions):
            end = mentions[index + 1].start() if index + 1 < len(mentions) else len(sentence)
            clause = sentence[mention.end() : end]
            if index == 0:
                clause = sentence[: mention.start()] + clause
            foreign_numbers, foreign_quantities = foreign[mention.group()]
            if extract_number_tokens(clause) & foreign_numbers:
                errors.append("market_number_binding")
            if any(quantity_token_supported(token, foreign_quantities) for token in extract_quantity_tokens(clause)):
                errors.append("market_quantity_binding")


def _anchors(text: str) -> set[str]:
    words = set(re.findall(r"[A-Za-z][A-Za-z0-9.+-]+", text.lower()))
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        words.update(run[index : index + 2] for index in range(len(run) - 1))
    return words - _COMMON_CHINESE_TERMS


def _chinese_entities(text: str) -> set[str]:
    entities: set[str] = set()
    verbs = "发布|计划|拟|已|收购|救市|引发|导致|上涨|下跌|测试|开放|上线"
    for match in re.finditer(rf"([\u4e00-\u9fff]{{2,4}})(?=(?:{verbs}))", text):
        candidate = match.group(1)
        if candidate not in _COMMON_CHINESE_TERMS and not any(term in candidate for term in _COMMON_CHINESE_TERMS):
            entities.add(candidate)
    return entities


def _claim_windows(text: str, anchor: str) -> list[str]:
    windows = []
    for match in re.finditer(re.escape(anchor), text):
        start = max(text.rfind(mark, 0, match.start()) for mark in "。！？；\n") + 1
        end_positions = [position for mark in "，。！？；\n" if (position := text.find(mark, match.end())) >= 0]
        end = min(end_positions) if end_positions else len(text)
        windows.append(text[start:end])
    return windows


def _fact_anchors(text: str, fact) -> list[str]:
    direct = [anchor for anchor in (fact.subject, *fact.entities) if anchor and len(anchor) >= 2 and anchor in text]
    shared = _anchors(text) & _anchors(fact.text)
    return list(dict.fromkeys([*direct, *(anchor for anchor in shared if len(anchor) >= 2)]))


def _certainty_upgraded(fact, anchor: str, clause: str) -> bool:
    source_clauses = _claim_windows(fact.text, anchor)
    uncertain_clauses = [
        source_clause for source_clause in source_clauses if any(marker in source_clause for marker in _UNCERTAIN)
    ]
    if not uncertain_clauses:
        return False
    anchor_occurrences = sum(source_clause.count(anchor) for source_clause in source_clauses)
    if anchor_occurrences > 1:
        return False
    return any(
        any(marker in clause for marker in _COMPLETED) and not any(marker in source_clause for marker in _COMPLETED)
        for source_clause in uncertain_clauses
    )


def _attribution_required(fact, anchor: str) -> bool:
    return any(
        any(marker in source_clause for marker in _ATTRIBUTION) for source_clause in _claim_windows(fact.text, anchor)
    )


def _has_direction_reversal(clause: str, direction: str) -> bool:
    negated_up = bool(re.search(r"(?:没有|并未|未|并没有)(?:出现)?(?:上涨|走高|上行|领涨)", clause))
    negated_down = bool(re.search(r"(?:没有|并未|未|并没有)(?:出现)?(?:下跌|走低|回落|领跌)", clause))
    up = bool(re.search(r"上涨|走高|上行|领涨|\+\d", clause))
    down = bool(re.search(r"下跌|走低|回落|领跌|-\d", clause))
    return (direction == "up" and (down or negated_up)) or (direction == "down" and (up or negated_down))


def _market_subject_clause(text: str, subject: str) -> str:
    match = re.search(re.escape(subject), text)
    if not match:
        return ""
    start = max(text.rfind(mark, 0, match.start()) for mark in "，。！？；\n") + 1
    end_positions = [position for mark in "，。！？；\n" if (position := text.find(mark, match.end())) >= 0]
    end = min(end_positions) if end_positions else len(text)
    return text[start:end]


def _has_unambiguous_direction(clause: str, direction: str) -> bool:
    if direction == "up":
        return bool(re.search(r"上涨|走高|上行|领涨|\+\d", clause))
    if direction == "down":
        return bool(re.search(r"下跌|走低|回落|领跌|-\d", clause))
    return "持平" in clause or "平收" in clause


def _apply_gate(errors: list[str], warnings: list[str], gate: str) -> None:
    if gate != "soft":
        return
    codes = list(dict.fromkeys(errors))
    errors[:] = [code for code in codes if code in ALWAYS_HARD_METADATA_ERRORS]
    warnings.extend(code for code in codes if code not in ALWAYS_HARD_METADATA_ERRORS)


def validate_metadata(
    *,
    stage: Stage,
    text: str,
    fact_ids: list[str],
    facts: StructuredFacts,
    script: str,
    mode: str,
    gate: str = "strict",
) -> dict:
    if gate not in {"strict", "soft"}:
        raise ValueError(f"unknown metadata quality gate: {gate}")
    errors: list[str] = []
    warnings: list[str] = []
    compact = re.sub(r"\s+", "", text)
    count = len(text) if stage == "title" else len(compact)
    if not compact:
        errors.append("empty_text")
    if mode == "manual":
        warnings.append("manual_override_not_fact_checked")
        _apply_gate(errors, warnings, gate)
        return {"passed": not errors, "character_count": count, "errors": errors, "warnings": warnings, "gate": gate}
    is_rule = mode in {"rule", "validated_rule_fallback"}
    limit = (2000 if is_rule else 16) if stage == "title" else 300
    if count > limit:
        errors.append("length_limit")
    recommended = (8, 16) if stage == "title" else (60, 150)
    if not recommended[0] <= count <= recommended[1]:
        warnings.append("outside_recommended_length")
    if re.search(r"`|\*\*|__|~~|https?://|(?:[A-Za-z]:|~)[/\\]|^\s*[#>]|!\[|\[[^]]*]\(|<[^>]+>", text):
        errors.append("not_plain_text")
    if stage == "title" and len(text.splitlines()) > 1:
        errors.append("multiline_title")
    if (
        stage == "description"
        and not is_rule
        and not text.rstrip("”’\"'）)").endswith(("。", "！", "？", ".", "!", "?"))
    ):
        warnings.append("sentence_end_missing")
    if any(marker in text for marker in ("以下是", "标题：", "描述：", "根据素材")):
        errors.append("explanation_prefix")
    if any(marker in compact for marker in _PROHIBITED):
        errors.append("prohibited_promise")
    by_id = {fact.id: fact for fact in facts.facts}
    if set(fact_ids) - by_id.keys():
        errors.append("unknown_fact_id")
    selected = [by_id[fact_id] for fact_id in dict.fromkeys(fact_ids) if fact_id in by_id]
    legacy_description = is_rule and stage == "description" and text == RULE_DESCRIPTIONS.get(facts.content_type)
    if not is_rule and not selected:
        errors.append("missing_fact_ids")
    generic_rule_title = (
        is_rule
        and stage == "title"
        and facts.content_type == "finance"
        and bool(re.fullmatch(r"每日基金日报 · \d{4}-\d{2}-\d{2}", text))
    )
    if not legacy_description and not generic_rule_title:
        usable = [
            fact
            for fact in (facts.facts if is_rule else selected)
            if fact.kind != "risk" and not _DATE_ONLY.fullmatch(fact.text.strip())
        ]
        if not usable or not any(_anchors(text) & _anchors(fact.text) for fact in usable):
            errors.append("fact_anchor_missing")
        if not _anchors(text) & _anchors(script):
            errors.append("script_anchor_missing")
    source = " ".join(fact.text for fact in (selected if not is_rule else facts.facts))
    if not legacy_description and not generic_rule_title:
        names = set(re.findall(r"[A-Za-z][A-Za-z0-9.+-]+", text.lower())) - {"ai"}
        if any(name not in source.lower() or name not in script.lower() for name in names):
            errors.append("unsupported_entity")
        known_named_entities = {"腾讯", "百度", "阿里巴巴", "字节跳动", "央行", "OpenAI", "微软", "英伟达"}
        if any(entity in text and entity not in source and entity not in script for entity in known_named_entities):
            errors.append("unsupported_entity")
    numbers = extract_number_tokens(_normalize_quantities(text))
    source_numbers = {number.lstrip("+-") for number in extract_number_tokens(_normalize_quantities(source))}
    script_numbers = {number.lstrip("+-") for number in extract_number_tokens(_normalize_quantities(script))}
    source_numbers |= {match.group(1) for match in re.finditer(r"[\u4e00-\u9fff]+(\d+)", source)}
    script_numbers |= {match.group(1) for match in re.finditer(r"[\u4e00-\u9fff]+(\d+)", script)}
    if {number.lstrip("+-") for number in numbers} - source_numbers:
        errors.append("unsupported_number")
    if not is_rule and {number.lstrip("+-") for number in numbers} - script_numbers:
        errors.append("number_not_in_script")
    if _unsupported_quantities(text, extract_quantity_tokens(source)):
        errors.append("unsupported_quantity")
    _validate_market_binding(text, facts, errors)
    for fact in selected if not is_rule else facts.facts:
        anchors = _fact_anchors(text, fact)
        if not anchors:
            continue
        anchor_clauses = [(anchor, clause) for anchor in anchors for clause in _claim_windows(text, anchor)]
        clauses = [clause for _, clause in anchor_clauses]
        source_actions = {action for action in _HIGH_RISK_ACTIONS if action in fact.text}
        if any(action in text for action in _HIGH_RISK_ACTIONS) and any(
            action not in source_actions for action in _HIGH_RISK_ACTIONS if action in "".join(clauses)
        ):
            errors.append("unsupported_action_or_causality")
        for anchor, clause in anchor_clauses:
            if _certainty_upgraded(fact, anchor, clause):
                errors.append("certainty_upgrade")
            if _attribution_required(fact, anchor) and not any(marker in clause for marker in _ATTRIBUTION):
                errors.append("attribution_missing")
        if fact.subject and fact.direction:
            subject_clause = _market_subject_clause(text, fact.subject)
            if subject_clause and _has_direction_reversal(subject_clause, fact.direction):
                errors.append("direction_mismatch")
            elif subject_clause and _has_unambiguous_direction(subject_clause, fact.direction):
                pass
    if facts.content_type == "finance" and stage == "description":
        if not re.search(r"不构成(?:任何)?投资建议", compact):
            errors.append("finance_disclaimer")
        if not any(marker in compact for marker in ("风险", "审慎决策", "市场波动")):
            if legacy_description:
                warnings.append("legacy_rule_description_uses_reference_notice")
            else:
                errors.append("finance_risk_warning")
    _apply_gate(errors, warnings, gate)
    return {
        "passed": not errors,
        "character_count": count,
        "errors": list(dict.fromkeys(errors)),
        "warnings": warnings,
        "gate": gate,
    }
