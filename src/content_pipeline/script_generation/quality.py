from __future__ import annotations

import re
from datetime import datetime

from content_pipeline.script_generation.models import (
    QualityIssue,
    ScriptFact,
    ScriptPlan,
    ScriptQualityReport,
    StructuredFacts,
)
from content_pipeline.script_generation.policy import ALWAYS_HARD_ISSUE_CODES, ATTRIBUTION_MARKERS, get_policy

__all__ = [
    "QuantityToken",
    "count_script_characters",
    "extract_number_tokens",
    "extract_quantity_tokens",
    "quantity_token_supported",
    "validate_script",
]


class QuantityToken:
    def __init__(self, value: str, magnitude: str = "", unit: str = ""):
        self.value = value
        self.magnitude = magnitude
        self.unit = unit

    def __eq__(self, other: object) -> bool:
        return isinstance(other, QuantityToken) and self.key == other.key

    def __hash__(self) -> int:
        return hash(self.key)

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.value, self.magnitude, self.unit)


_MARKDOWN_RE = re.compile(r"!\[[^]]*]\([^)]*\)|^#{1,6}\s+|^[\-*+]\s+|```", re.MULTILINE)
_PATH_RE = re.compile(r"(?:^|[：:])(?:~|[A-Za-z]:)?[/\\][^\s，。]+\.(?:md|txt|json|srt)", re.IGNORECASE)
_EXPLANATION_MARKERS = ("以下是", "根据素材", "我将", "口播稿如下", "Markdown")
_STYLE_MARKERS = ("值得注意的是", "不难发现", "可以说", "总的来说", "让我们来看看", "随着技术发展")
_BACKGROUND_OPENING_RE = re.compile(
    r"^(?:(?:北京时间)?(?:今天|今日|昨天|昨晚|今晨|凌晨)|\d{4}年\d{1,2}月\d{1,2}日|.*?(?:举办|举行)(?:的)?(?:发布会|活动)|(?:每日|今天)?AI简报)"
)
_FACT_CHANGE_RE = re.compile(r"发布|推出|开放|上线|部署|测试|开源|支持|实现|达到|突破|刷新|搬进|落地|升级")


def count_script_characters(script: str) -> int:
    return len(re.sub(r"\s+", "", script))


_QUANTITY_RE = re.compile(
    r"(?<![\d.,，])(?P<value>[+-]?(?:\d{1,3}(?:[,，]\d{3})+|\d+)(?:\.\d+)?)"
    r"\s*(?P<magnitude>万亿|亿|万|千|百)?\s*"
    r"(?P<unit>个百分点|百分点|百分比|%|美元|人民币|港元|欧元|日元|元|人|用户|点|倍|个|台|家|项|名)?"
)


def _normalized_number(value: str) -> str:
    value = value.replace(",", "").replace("，", "")
    sign = "" if not value.startswith(("+", "-")) else value[0]
    core = value.lstrip("+-")
    if "." not in core:
        core = str(int(core))
    return sign + core


def extract_quantity_tokens(value: str) -> set[QuantityToken]:
    tokens: set[QuantityToken] = set()
    for match in _QUANTITY_RE.finditer(value):
        unit = match.group("unit") or ""
        magnitude = match.group("magnitude") or ""
        if not unit and not magnitude:
            continue
        tokens.add(QuantityToken(_normalized_number(match.group("value")), magnitude, unit))
    return tokens


def quantity_token_supported(token: QuantityToken, candidates: set[QuantityToken]) -> bool:
    if token in candidates:
        return True
    unsigned = QuantityToken(token.value.lstrip("+-"), token.magnitude, token.unit)
    if unsigned in candidates or any(
        candidate.value.lstrip("+-") == unsigned.value
        and candidate.magnitude == unsigned.magnitude
        and candidate.unit == unsigned.unit
        for candidate in candidates
    ):
        return True
    if token.unit == "元":
        return (
            QuantityToken(token.value, token.magnitude, "") in candidates
            or QuantityToken(token.value.lstrip("+-"), token.magnitude, "") in candidates
        )
    if not token.unit and token.magnitude:
        return (
            QuantityToken(token.value, token.magnitude, "元") in candidates
            or QuantityToken(token.value.lstrip("+-"), token.magnitude, "元") in candidates
        )
    return False


def extract_number_tokens(value: str) -> set[str]:
    tokens = set(re.findall(r"[+-]?\d+(?:\.\d+)?%?", value.replace(",", "").replace("，", "")))
    normalized = set(tokens)
    for token in tokens:
        suffix = "%" if token.endswith("%") else ""
        core = token.rstrip("%")
        sign = "" if not core.startswith(("+", "-")) else core[0]
        core = core.lstrip("+-")
        if core.isdigit():
            normalized.add(sign + str(int(core)) + suffix)
            normalized.add(str(int(core)) + suffix)
    return normalized


def validate_script(
    script: str,
    facts: StructuredFacts,
    *,
    plan: ScriptPlan | None = None,
    profile: str = "generated",
    gate: str = "strict",
    duration_variant: str = "legacy_90_seconds",
) -> ScriptQualityReport:
    normalized = re.sub(r"\s+", "", script)
    policy = get_policy(facts.content_type, duration_variant)
    minimum, maximum = policy.hard_length
    if profile not in {"generated", "reviewed", "legacy"}:
        raise ValueError(f"unknown script validation profile: {profile}")
    if gate not in {"strict", "soft"}:
        raise ValueError(f"unknown script quality gate: {gate}")
    issues: list[QualityIssue] = []
    _require(
        minimum <= len(normalized) <= maximum,
        issues,
        "length",
        f"正文去空白后必须为 {minimum}-{maximum} 字，当前 {len(normalized)} 字",
        severity="warning" if profile == "reviewed" else "error",
    )
    _require(not _MARKDOWN_RE.search(script), issues, "markdown", "正文不能包含 Markdown")
    _require(not _PATH_RE.search(script), issues, "file_path", "正文不能包含文件路径")
    _require(
        not any(marker in script for marker in _EXPLANATION_MARKERS),
        issues,
        "explanation_prefix",
        "正文不能包含解释性前缀",
    )
    _require(normalized.endswith(("。", "！", "？")), issues, "sentence_end", "正文必须以完整中文句子结束")
    _require(not re.search(r"[，。！？；：、]{2,}", normalized), issues, "punctuation", "正文包含异常连续标点")
    repeated_word = _repeated_chinese_word(normalized)
    if repeated_word:
        _issue(issues, "repeated_chinese_word", "正文包含直接连续重复的中文词", evidence=repeated_word)
    _require(_balanced_punctuation(normalized), issues, "unbalanced_punctuation", "正文包含不配对的括号或引号")
    source_numbers = set(facts.source_numbers)
    source_numbers |= {number.lstrip("+-") for number in source_numbers}
    extra_numbers = {
        number
        for number in extract_number_tokens(script)
        if number not in source_numbers and number.lstrip("+-").rstrip("%") not in source_numbers
    }
    if extra_numbers:
        _issue(issues, "unsupported_number", "正文包含素材外数字", evidence=", ".join(sorted(extra_numbers)[:8]))
    selected = _selected_facts(facts, plan)
    if profile == "generated":
        for fact in selected:
            if fact.kind not in {"index", "sector", "risk"}:
                _require(
                    _fact_matches(script, fact),
                    issues,
                    "required_fact_missing",
                    f"正文未覆盖选中的必选事实：{fact.id}",
                    evidence=fact.id,
                )
    if facts.content_type == "ai_briefing":
        if profile == "generated":
            _validate_ai(script, selected, issues)
    else:
        _validate_finance(
            script, selected, issues, strict=profile != "legacy", plan=plan, content_type=facts.content_type
        )
    for marker in _STYLE_MARKERS:
        if marker in script:
            _issue(issues, "style_boilerplate", f"建议删除套话：{marker}", severity="warning", evidence=marker)
    if gate == "soft":
        issues = [
            issue
            if issue.severity == "warning" or issue.code in ALWAYS_HARD_ISSUE_CODES
            else issue.model_copy(update={"severity": "warning"})
            for issue in issues
        ]
    return ScriptQualityReport(
        passed=not any(issue.severity == "error" for issue in issues),
        character_count=len(normalized),
        issues=issues,
        checked_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        gate=gate,
    )


def _repeated_chinese_word(text: str) -> str | None:
    match = re.search(r"([\u4e00-\u9fff]{2,4})\1", text)
    return match.group() if match else None


def _balanced_punctuation(text: str) -> bool:
    pairs = (("（", "）"), ("(", ")"), ("“", "”"), ("‘", "’"))
    return all(text.count(left) == text.count(right) for left, right in pairs)


def _selected_facts(facts: StructuredFacts, plan: ScriptPlan | None) -> list[ScriptFact]:
    if plan is None:
        return [fact for fact in facts.facts if fact.required]
    selected = set(plan.required_facts) | {plan.main_fact_id}
    return [fact for fact in facts.facts if fact.id in selected]


def _validate_ai(script: str, facts: list[ScriptFact], issues: list[QualityIssue]) -> None:
    _validate_ai_opening(script, facts, issues)
    for fact in facts:
        anchor = next((entity for entity in fact.entities if len(entity) >= 2 and entity in script), None)
        if not anchor:
            continue
        windows = _anchor_windows(script, anchor, radius=55)
        if set(fact.status_markers) & {"planned", "possible", "testing"}:
            completed = ("已发布", "已上线", "已确认", "正式发布", "正式上线", "已经推出")
            if "completed" not in fact.status_markers and any(
                any(marker in window for marker in completed) for window in windows
            ):
                _issue(
                    issues, "ai_status_upgrade", f"正文可能把非确定状态升级为已完成事实：{fact.id}", evidence=fact.id
                )
        if fact.attribution_markers and not any(marker in "".join(windows) for marker in ATTRIBUTION_MARKERS):
            _issue(issues, "ai_attribution_missing", f"正文遗漏来源归因：{fact.id}", evidence=fact.id)


def _validate_ai_opening(script: str, facts: list[ScriptFact], issues: list[QualityIssue]) -> None:
    match = re.search(r"[^。！？]*[。！？]", script)
    if not match:
        return
    opening = match.group()
    opening_numbers = extract_number_tokens(opening)
    hook_numbers = _opening_hook_numbers(facts)
    if hook_numbers and not opening_numbers & hook_numbers:
        _issue(
            issues,
            "ai_opening_number_missing",
            "AI 简报首句未使用已选事实中的有信息量来源数字",
            evidence=", ".join(sorted(hook_numbers)),
        )
    if hook_numbers:
        first_number = _first_number_position(opening)
        if len(re.sub(r"\s+", "", opening).rstrip("。！？")) > 28 or first_number is None or first_number >= 12:
            _issue(
                issues,
                "ai_opening_hook_weak",
                "AI 简报首句的数字钩子过长或数字未在前 12 个字符内出现",
                severity="warning",
                evidence=opening,
            )
    if _BACKGROUND_OPENING_RE.search(opening):
        entities = {entity for fact in facts for entity in fact.entities if len(entity) >= 2}
        if not (any(entity in opening for entity in entities) and _FACT_CHANGE_RE.search(opening)):
            _issue(issues, "ai_weak_opening", "AI 简报首句未先交代主体、具体变化和事实锚点", evidence=opening)


def _opening_hook_numbers(facts: list[ScriptFact]) -> set[str]:
    candidates = {token for fact in facts for token in fact.numbers if _is_informative_number(token, fact.text)}
    if not candidates:
        return set()
    highest_magnitude = max(_number_magnitude(token, facts) for token in candidates)
    return {token for token in candidates if _number_magnitude(token, facts) == highest_magnitude}


def _is_informative_number(token: str, text: str) -> bool:
    core = token.rstrip("%")
    if re.fullmatch(r"(?:19|20)\d{2}", core):
        return False
    date_numbers = {
        number
        for date in re.findall(r"\d{4}(?:[-/.年])\d{1,2}(?:[-/.月])\d{1,2}(?:日)?", text)
        for number in extract_number_tokens(date)
    }
    if token in date_numbers or core in date_numbers:
        return False
    date_patterns = (rf"{re.escape(core)}年", rf"{re.escape(core)}月", rf"{re.escape(core)}日", rf"第{re.escape(core)}")
    return not any(re.search(pattern, text) for pattern in date_patterns)


def _number_magnitude(token: str, facts: list[ScriptFact]) -> float:
    core = token.rstrip("%")
    multiplier = 1.0
    for fact in facts:
        for match in re.finditer(rf"{re.escape(core)}(?:\.\d+)?\s*(万亿|亿|万|兆|千|百)", fact.text):
            multiplier = max(
                multiplier,
                {
                    "百": 100.0,
                    "千": 1_000.0,
                    "万": 10_000.0,
                    "亿": 100_000_000.0,
                    "万亿": 1_000_000_000_000.0,
                    "兆": 1_000_000_000_000.0,
                }[match.group(1)],
            )
    return float(core) * multiplier


def _first_number_position(text: str) -> int | None:
    match = re.search(r"\d", text)
    return match.start() if match else None


def _validate_finance(
    script: str,
    facts: list[ScriptFact],
    issues: list[QualityIssue],
    *,
    strict: bool,
    plan: ScriptPlan | None,
    content_type: str,
) -> None:
    _require(
        "不构成" in script and "投资建议" in script, issues, "finance_disclaimer", "财经口播必须包含不构成投资建议声明"
    )
    _require(
        any(marker in script for marker in ("风险承受能力", "审慎决策", "市场波动", "注意风险")),
        issues,
        "finance_risk_warning",
        "财经口播必须包含风险提示",
    )
    if strict:
        for fact in facts:
            if fact.kind in {"index", "sector"}:
                _validate_market_anchor(script, fact, issues)
        if content_type == "finance":
            _validate_finance_opening(script, facts, plan, issues)
        _validate_finance_hype(script, facts, issues)
    prohibited = _finance_prohibited_expression(script)
    if prohibited:
        _issue(issues, "finance_prohibited", "财经口播包含确定性或诱导性投资表述", evidence=prohibited)


_FINANCE_TRADE_ACTIONS = ("买入", "卖出", "加仓", "减仓", "清仓", "满仓", "梭哈", "抄底", "止盈", "上车")
_FINANCE_NEGATIONS = ("不要", "不宜", "不可", "避免", "切勿", "不能", "别")


def _finance_prohibited_expression(script: str) -> str | None:
    for expression in get_policy("finance").prohibited_expressions:
        for match in re.finditer(re.escape(expression), script):
            if expression in _FINANCE_TRADE_ACTIONS and _trade_action_is_negated(script, match.start()):
                continue
            return match.group()
    return None


def _trade_action_is_negated(script: str, action_start: int) -> bool:
    sentence_start = max(script.rfind(mark, 0, action_start) for mark in "。！？；") + 1
    clause = script[sentence_start:action_start]
    return any(negation in clause for negation in _FINANCE_NEGATIONS)


_FINANCE_HYPE_WORDS = ("炸裂", "封神", "颠覆", "硬拉", "跳水", "被砸", "杀跌", "强反差")
_FINANCE_INFERENCE_WORDS = (
    "必涨",
    "见底",
    "起飞",
    "牛市",
    "机会",
    "买入",
    "卖出",
    "加仓",
    "减仓",
    "满仓",
    "抄底",
    "收益",
)


def _validate_finance_opening(
    script: str, facts: list[ScriptFact], plan: ScriptPlan | None, issues: list[QualityIssue]
) -> None:
    match = re.search(r"[^。！？]*[。！？]", script)
    if not match:
        return
    opening = match.group()
    by_id = {fact.id: fact for fact in facts}
    primary = by_id.get(plan.main_fact_id) if plan else None
    if primary is None or primary.kind not in {"index", "sector"}:
        primary = next((fact for fact in facts if fact.kind == "index"), None) or next(
            (fact for fact in facts if fact.kind == "sector"), None
        )
    if primary is None:
        return
    has_subject = bool(primary.subject and _subject_anchor(opening, primary.subject))
    has_number = bool(set(primary.numbers) & extract_number_tokens(opening))
    has_direction = _direction(opening) == primary.direction if primary.direction else True
    has_unit = _opening_has_market_unit(opening, primary)
    if not (has_subject and has_number and has_direction and has_unit):
        _issue(
            issues,
            "finance_opening_market_anchor_missing",
            "财经首句必须包含优先来源行情的主体、数字、明确方向和正确单位",
            evidence=opening,
        )
    if not (has_subject and has_number and has_direction):
        _issue(issues, "finance_weak_opening", "财经首句未先给出完整来源行情钩子", evidence=opening)
    first_number = _first_number_position(opening)
    if first_number is None or first_number >= 12 or len(re.sub(r"\s+", "", opening).rstrip("。！？")) > 30:
        _issue(
            issues,
            "finance_opening_hook_weak",
            "财经首句数字钩子过长或数字未在前 12 个字符内出现",
            severity="warning",
            evidence=opening,
        )


def _opening_has_market_unit(opening: str, fact: ScriptFact) -> bool:
    if fact.point_value and fact.point_value in opening and "点" not in opening:
        return False
    if fact.unit == "percent":
        return "%" in opening and "百分点" not in opening
    if fact.unit == "percentage_point":
        return "百分点" in opening
    return True


def _validate_finance_hype(script: str, facts: list[ScriptFact], issues: list[QualityIssue]) -> None:
    market_facts = [fact for fact in facts if fact.kind in {"index", "sector"}]
    for word in _FINANCE_HYPE_WORDS:
        for match in re.finditer(word, script):
            prefix = script[max(0, match.start() - 4) : match.start()]
            if word == "杀跌" and prefix.endswith("追涨"):
                continue
            binding_window = _hype_binding_window(script, match.start(), match.end())
            bound = any(
                fact.subject
                and _subject_anchor(binding_window, fact.subject)
                and bool(set(fact.numbers) & extract_number_tokens(binding_window))
                and _direction(binding_window) == fact.direction
                and _opening_has_market_unit(binding_window, fact)
                for fact in market_facts
            )
            if not bound or any(marker in binding_window for marker in _FINANCE_INFERENCE_WORDS):
                _issue(
                    issues,
                    "finance_hype_unbound",
                    "财经高张力词必须绑定来源行情，且不能同句或紧邻短句出现预测或交易结论",
                    evidence=word,
                )
                return


def _hype_binding_window(script: str, start: int, end: int) -> str:
    sentence_start = max(script.rfind(mark, 0, start) for mark in "。！？；") + 1
    sentence_ends = [script.find(mark, end) for mark in "。！？；"]
    sentence_end = (
        min(position for position in sentence_ends if position >= 0)
        if any(position >= 0 for position in sentence_ends)
        else len(script)
    )
    windows = [script[sentence_start : sentence_end + 1]]
    previous_end = sentence_start - 1
    previous_start = max(script.rfind(mark, 0, previous_end) for mark in "。！？；") + 1
    previous = script[previous_start:previous_end]
    if previous and len(previous) <= 40:
        windows.insert(0, previous)
    next_start = sentence_end + 1
    next_ends = [script.find(mark, next_start) for mark in "。！？；"]
    next_end = (
        min(position for position in next_ends if position >= 0)
        if any(position >= 0 for position in next_ends)
        else len(script)
    )
    following = script[next_start:next_end]
    if following and len(following) <= 40:
        windows.append(following)
    return "".join(windows)


# 日报把合并板块写成 光模块/通信，口播常写成 光模块和通信：同一个主体，两种写法。
_SUBJECT_SEPARATOR_RE = re.compile(r"[/、与和及]")
_SUBJECT_PART_RADIUS = 16


def _subject_anchor(script: str, subject: str) -> str | None:
    if subject in script:
        return subject
    parts = [part for part in _SUBJECT_SEPARATOR_RE.split(subject) if part]
    if len(parts) < 2:
        return None
    for match in re.finditer(re.escape(parts[0]), script):
        start, end = match.start(), match.end()
        for part in parts[1:]:
            position = script.find(part, start, end + _SUBJECT_PART_RADIUS)
            if position < 0:
                break
            end = position + len(part)
        else:
            return script[start:end]
    return None


def _validate_market_anchor(script: str, fact: ScriptFact, issues: list[QualityIssue]) -> None:
    anchor = _subject_anchor(script, fact.subject) if fact.subject else None
    if anchor is None:
        _issue(
            issues,
            "required_fact_missing",
            f"正文未覆盖选中的具体行情主体：{fact.subject or fact.id}",
            evidence=fact.id,
        )
        return
    clauses = _subject_clauses(script, anchor, include_change=fact.kind == "index")
    expected_numbers = set(fact.numbers)
    if fact.change_value:
        expected_numbers = extract_number_tokens(fact.change_value)
    expected_numbers |= {number.lstrip("+-") for number in expected_numbers}
    anchored = next((clause for clause in clauses if expected_numbers & extract_number_tokens(clause)), None)
    subject_context = "".join(_anchor_windows(script, anchor, radius=50))
    if anchored is None and expected_numbers & extract_number_tokens(subject_context):
        anchored = subject_context
    if anchored is None:
        _issue(issues, "finance_number_binding", f"{fact.subject} 与其数值未在同句或近邻窗口出现", evidence=fact.id)
        return
    actual_direction = _direction(anchored)
    if fact.direction and actual_direction and _direction(anchored.replace(fact.subject, "")) != actual_direction:
        actual_direction = _direction(anchored.replace(fact.subject, ""))
    if fact.direction and not actual_direction:
        _issue(issues, "finance_direction_missing", f"{fact.subject} 未明确说出涨跌方向", evidence=fact.id)
    elif fact.direction and actual_direction != fact.direction:
        _issue(issues, "finance_direction_mismatch", f"{fact.subject} 的涨跌方向与素材不一致", evidence=fact.id)
    if fact.unit == "percent" and ("百分点" in subject_context or "百分点" in anchored):
        _issue(issues, "finance_unit_mismatch", f"{fact.subject} 的百分比被写成百分点", evidence=fact.id)
    if fact.unit == "percentage_point" and "百分点" not in subject_context:
        _issue(issues, "finance_unit_mismatch", f"{fact.subject} 的百分点单位缺失或被改写", evidence=fact.id)
    if fact.point_value and fact.point_value in anchored and "点" not in anchored:
        _issue(issues, "finance_unit_mismatch", f"{fact.subject} 的点位单位缺失", evidence=fact.id)


def _subject_clauses(script: str, subject: str, *, include_change: bool = False) -> list[str]:
    clauses = []
    for match in re.finditer(re.escape(subject), script):
        sentence_ends = [position for mark in "。！？；" if (position := script.find(mark, match.end())) >= 0]
        sentence_end = min(sentence_ends) if sentence_ends else min(len(script), match.end() + 100)
        commas = [position for position in (script.find("，", match.end()),) if 0 <= position < sentence_end]
        end = sentence_end + 1
        if commas and not include_change:
            end = commas[0] + 1
        clauses.append(script[match.start() : end])
    return clauses


def _direction(text: str) -> str | None:
    if re.search(r"(?:下跌|跌|回落|走低)|-\d", text):
        return "down"
    if re.search(r"(?:上涨|涨|走高|上行)|\+\d", text):
        return "up"
    if any(marker in text for marker in ("持平", "平收")):
        return "flat"
    return None


def _anchor_windows(script: str, anchor: str, *, radius: int) -> list[str]:
    return [
        script[max(0, match.start() - radius) : min(len(script), match.end() + radius)]
        for match in re.finditer(re.escape(anchor), script)
    ]


def _fact_matches(script: str, fact: ScriptFact) -> bool:
    entities = [entity for entity in fact.entities if len(entity) >= 2]
    return (not entities or any(entity in script for entity in entities[:5])) and (
        not fact.numbers or bool(set(fact.numbers) & extract_number_tokens(script))
    )


def _require(
    condition: bool,
    issues: list[QualityIssue],
    code: str,
    message: str,
    *,
    severity: str = "error",
    evidence: str | None = None,
) -> None:
    if not condition:
        _issue(issues, code, message, severity=severity, evidence=evidence)


def _issue(
    issues: list[QualityIssue], code: str, message: str, *, severity: str = "error", evidence: str | None = None
) -> None:
    issues.append(QualityIssue(code=code, message=message, severity=severity, evidence=evidence))

