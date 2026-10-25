from __future__ import annotations

import re
from datetime import date
from typing import Any

from content_pipeline.script_generation.policy import get_policy
from content_pipeline.script_generation.quality import extract_number_tokens
from content_pipeline.tools.narrated_mpt_client import looks_like_file_reference

__all__ = [
    "CONTENT_STUDIO_FINANCE_DISCLAIMER",
    "CONTENT_STUDIO_MAX_CHARS",
    "CONTENT_STUDIO_MIN_CHARS",
    "DAILY_FINANCE_DISCLAIMER",
    "FINANCE_MAX_CHARS",
    "FINANCE_MIN_CHARS",
    "FINANCE_RISK_NOTE",
    "FINANCE_SECTION_LABELS",
    "FinanceScriptError",
    "build_daily_markdown_script",
    "build_structured_finance_script",
    "clean_markdown",
    "trim_complete",
    "validate_daily_finance_script",
]

CONTENT_STUDIO_FINANCE_DISCLAIMER = "以上内容仅为市场信息整理，不构成投资建议。"
DAILY_FINANCE_DISCLAIMER = "以上内容仅为市场信息整理和个人观点，不构成任何投资建议。"
FINANCE_RISK_NOTE = "面对市场波动，请结合自身投资期限、仓位水平和风险承受能力审慎决策。"
FINANCE_MIN_CHARS, FINANCE_MAX_CHARS = get_policy("finance").hard_length
CONTENT_STUDIO_MIN_CHARS, CONTENT_STUDIO_MAX_CHARS = get_policy("finance_multi_asset").hard_length
GENERIC_FILLER_MARKERS = (
    "资料不完整时",
    "黄金通常同时受到",
    "宏观数据有月度",
    "单日行情只代表",
    "涉及滞后或回填",
    "如果只有单一来源",
    "正式生成视频前",
    "自动生成内容优先",
)
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


class FinanceScriptError(ValueError):
    pass


def clean_markdown(value: str) -> str:
    value = re.sub(r"!\[[^]]*]\([^)]*\)", "", value)
    value = re.sub(r"\[([^]]+)]\([^)]*\)", r"\1", value)
    value = re.sub(r"[`*_>#]", "", value)
    value = re.sub(r"^[|\s]+|[|\s]+$", "", value)
    value = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9%+\-.,，。；：:、（）()/$≈=~]", "", value)
    return re.sub(r"\s+", "", value).strip("，。； ")


def _bullet_text(line: str) -> str:
    return re.sub(r"^[-•·]+\s*", "", clean_markdown(line))


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
    return _truncate_text("；".join(line for line in lines if line), limit).rstrip("；")


def trim_complete(value: str, limit: int) -> str:
    value = value.strip("，。； ")
    if len(value) <= limit:
        return value
    clipped = value[:limit]
    stops = [clipped.rfind(mark) for mark in "。！？；"]
    stop = max(stops)
    if stop >= max(120, limit - 100):
        return clipped[: stop + 1].strip()
    return clipped.rstrip("，；： ") + "。"


def _section(markdown: str, *keywords: str) -> str:
    candidates = re.finditer(r"(?m)^(?:#{2,4}\s+(.+?)|\*\*(.+?)\*\*)\s*$", markdown)
    headings = []
    for heading in candidates:
        title = clean_markdown(heading.group(1) or heading.group(2))
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
    cells = [clean_markdown(cell) for cell in stripped.strip("|").split("|")]
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
        cells = [clean_markdown(cell) for cell in stripped.strip("|").split("|")]
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
        if not name.endswith(("涨", "跌"))
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
        title = clean_markdown(line)
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
        values[clean_markdown(row[0])] = clean_markdown(row[1])
    return values


def _summary_value(values: dict[str, str], *keywords: str) -> str:
    for label, value in values.items():
        if any(keyword in label for keyword in keywords):
            return value
    return ""


def _first_sentence(value: str) -> str:
    cleaned = clean_markdown(value)
    parts = re.split(r"(?<=[。！？])", cleaned)
    return next((part.strip() for part in parts if part.strip()), cleaned)


def _first_sentences(value: str, limit: int) -> list[str]:
    return _sentence_parts(clean_markdown(value))[:limit]


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _value(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = payload.get(key)
        if value is not None and value != "":
            return value
    return None


def _number_text(value: Any, *, suffix: str = "") -> str:
    if value is None or value == "":
        return ""
    return f"{value}{suffix}"


def _change_direction(value: str) -> str:
    text = str(value).strip()
    if text.startswith("+") or "上涨" in text or "涨" in text:
        return "up"
    if text.startswith("-") or "下跌" in text or "跌" in text:
        return "down"
    return "flat"


def _spoken_change(value: str) -> str:
    text = str(value).strip()
    direction = _change_direction(text)
    if direction == "up":
        if text.startswith("+"):
            return "上涨" + text[1:]
        return text if "上涨" in text else "上涨" + text.lstrip("涨")
    if direction == "down":
        if text.startswith("-"):
            return "下跌" + text[1:]
        return text if "下跌" in text else "下跌" + text.lstrip("跌")
    return text if any(marker in text for marker in ("持平", "平收")) else "持平"


def _change_cell(value: str) -> str:
    match = re.match(r"\s*([+-]?\d+(?:\.\d+)?(?:%|个百分点|点))", str(value))
    return match.group(1) if match else str(value).strip()


def _row_change(row: list[str]) -> str:
    for cell in row[1:]:
        match = re.search(r"[+-]?\d+(?:\.\d+)?(?:%|个百分点|点)", cell)
        if match:
            return match.group()
    return ""


def _change_phrase(value: str) -> str:
    change = _change_cell(value)
    if _change_direction(change) == "flat" and not any(marker in change for marker in ("持平", "平收")):
        return f"涨跌幅{change}"
    return _spoken_change(change)


def _sector_phrase(row: list[str]) -> str:
    change = _row_change(row)
    return f"{row[0]}{_spoken_change(change)}" if change else row[0]


def _cross_asset_text(section: str, limit: int) -> str:
    bullets = [_bullet_text(line) for line in section.splitlines()]
    gold = next((text for text in bullets if text and "债" not in text), "")
    bond = next((text for text in bullets if "债" in text), "")
    return "；".join(_truncate_text(text, limit) for text in (gold, bond) if text)


def _append_until(parts: list[str], candidates: list[str], min_chars: int, tail: str) -> None:
    for candidate in candidates:
        if len("".join(parts)) + len(tail) >= min_chars:
            break
        if candidate and candidate not in parts:
            parts.append(candidate)


def _validate_content_studio_finance_script(script: str) -> None:
    if not CONTENT_STUDIO_MIN_CHARS <= len(script) <= CONTENT_STUDIO_MAX_CHARS:
        raise FinanceScriptError(f"金融初稿需要 300-600 字，当前为 {len(script)} 字")
    if any(marker in script for marker in GENERIC_FILLER_MARKERS):
        raise FinanceScriptError("金融初稿包含通用填充话术，缺少可播信息")
    if "不构成投资建议" not in script:
        raise FinanceScriptError("金融初稿缺少风险提示")
    if not re.search(r"\d", script):
        raise FinanceScriptError("金融初稿缺少具体数值")
    required_markers = ("黄金", "债市", "宏观")
    missing = [marker for marker in required_markers if marker not in script]
    if missing:
        raise FinanceScriptError("金融初稿缺少关键模块：" + "、".join(missing))


def _finalize_script(
    parts: list[str],
    *,
    disclaimer: str,
    required_tail: str = "",
    fillers: list[str] | None = None,
    min_chars: int = FINANCE_MIN_CHARS,
    max_chars: int = FINANCE_MAX_CHARS,
    length_error: str = "金融初稿长度不合规：{length} 字",
) -> str:
    core = re.sub(r"[。！？；]{2,}", "。", "".join(part for part in parts if part))
    tail = required_tail + disclaimer
    for filler in fillers or []:
        if len(core) + len(tail) >= min_chars:
            break
        core += filler
    limit = max_chars - len(tail)
    core = trim_complete(core, limit)
    script = core.rstrip("。") + "。" + tail
    if not min_chars <= len(script) <= max_chars:
        raise FinanceScriptError(length_error.format(length=len(script)))
    return script


def validate_daily_finance_script(script: str) -> None:
    if not FINANCE_MIN_CHARS <= len(script) <= FINANCE_MAX_CHARS:
        raise FinanceScriptError(
            f"finance narration must contain 300-600 characters; generated {len(script)} characters"
        )
    normalized = script.replace(" ", "")
    if (
        looks_like_file_reference(normalized)
        or "文件路径" in script
        or re.search(r"(?:^|[：:])(?:~|[A-Za-z]:)[/\\].+\.(?:md|txt|json|srt)", script, re.IGNORECASE)
    ):
        raise FinanceScriptError("finance narration contains a file reference instead of spoken content")
    if not all(marker in script for marker in ("指数", "板块")):
        raise FinanceScriptError("finance narration is missing required index or sector market information")


_SIGN_UNSAFE_CHARS = ("−", "–", "－", "±")
_HYPE_WORDS = ("炸裂", "封神", "颠覆", "硬拉", "跳水", "被砸", "杀跌", "强反差")
_TRADE_NEGATIONS = ("不要", "不宜", "不可", "避免", "切勿", "不能", "别", "不")
_INDEX_LABELS = {3: "三大指数", 4: "四大指数", 5: "五大指数"}
_INDEX_SECTION_KEYWORDS = ("四大指数", "四指数", "五指数")
_HEADING_SECTIONS = {
    "signals": ("今日关键词", "三个信号", "核心信号"),
    "interpretation": ("我的解读", "策略建议", "投资建议"),
    "valuation": ("估值水位",),
    "cross_asset": ("黄金债市", "黄金", "债券"),
}


def _sentence_parts(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"(?<=[。！？])", value) if part.strip()]


def _balanced_text(value: str) -> str:
    stack: list[int] = []
    chars: list[str] = []
    for char in value:
        if char in "（(":
            stack.append(len(chars))
        elif char in "）)":
            if not stack:
                continue
            stack.pop()
        chars.append(char)
    if stack:
        del chars[stack[0] :]
    return "".join(chars)


def _tidy_fragment(value: str) -> str:
    line = re.sub(r"^[-•·]{1,2}\s+", "", value.strip())
    line = re.sub(r"^\d{1,2}[.、](?!\d)\s*", "", line)
    text = clean_markdown(re.sub(r"[—―]+", "，", line))
    if not text or re.search(r":-|--", text):
        return ""
    text = re.sub(r"[，。！？；：、]{2,}", "。", text)
    text = _balanced_text(text).replace("(", "（").replace(")", "）")
    return text.strip("，。；：、 ")


def _condense_fragment(value: str, limit: int) -> str:
    kept = ""
    for sentence in _sentence_parts(_tidy_fragment(value)):
        if len(kept) + len(sentence) > limit:
            break
        kept += sentence
    return kept.strip("，。；：、 ")


def _numbers_supported(value: str, supported: set[str]) -> bool:
    return all(
        token in supported or token.lstrip("+-").rstrip("%") in supported for token in extract_number_tokens(value)
    )


def _safe_value(value: str) -> str:
    text = _balanced_text(value)
    if text != value:
        text = re.sub(r"[+-]?\d+(?:\.\d+)?%?$", "", text)
    return text.strip("，。；：、 ")


def _supported_value(value: str, supported: set[str]) -> str:
    text = _safe_value(value)
    return text if text and _numbers_supported(text, supported) else ""


def _readable_fragment(value: str, limit: int, supported: set[str]) -> str:
    if not value or not _fragment_allowed(value):
        return ""
    text = _safe_value(_condense_fragment(value, limit))
    return text if _numbers_supported(text, supported) else ""


def _fragment_allowed(value: str) -> bool:
    return (
        not any(char in value for char in _SIGN_UNSAFE_CHARS)
        and not any(word in value for word in _HYPE_WORDS)
        and not _has_prohibited_expression(value)
    )


def _has_prohibited_expression(value: str) -> bool:
    for expression in get_policy("finance").prohibited_expressions:
        for match in re.finditer(re.escape(expression), value):
            clause_start = max(value.rfind(mark, 0, match.start()) for mark in "。！？；，") + 1
            if any(negation in value[clause_start : match.start()] for negation in _TRADE_NEGATIONS):
                continue
            return True
    return False


def _lead_fragment(section: str, limit: int, supported: set[str]) -> str:
    for line in section.splitlines():
        if not line.strip() or line.lstrip().startswith("|"):
            continue
        text = _readable_fragment(line, limit, supported)
        if text:
            return text
    return ""


def _prose_candidates(section: str, supported: set[str]) -> list[str]:
    lines = [line for line in section.splitlines() if re.match(r"^[-•·]{1,2}\s+\S", line)]
    if not lines:
        lines = [line for line in section.splitlines() if "|" not in line]
    candidates: list[str] = []
    for line in lines:
        for sentence in _sentence_parts(line.strip()):
            if not _fragment_allowed(sentence):
                continue
            text = _tidy_fragment(sentence).strip("，。；：、 ")
            if len(text) >= 10 and _numbers_supported(text, supported):
                candidates.append(text)
    return candidates


def _supported_numbers(response: str, publication_date: str) -> set[str]:
    summary_values = _summary_table(response)
    values: list[str] = [publication_date]
    for label in ("盘面定性", "顶部摘要", "一句话总结", "四大指数", "四指数"):
        value = _summary_value(summary_values, label)
        if value:
            values.append(value)
    index_section = _section(response, *_INDEX_SECTION_KEYWORDS)
    values.extend(" ".join(row) for row in _table_rows(index_section))
    sector_section = _section(response, "板块轮动")
    values.extend(" ".join(row) for row in _sector_rows(sector_section))
    values.append(_section_text(sector_section, 1000))
    for key in ("signals", "interpretation"):
        values.extend(_first_sentences(_section(response, *_HEADING_SECTIONS[key]), 4))
    valuation_section = _section(response, *_HEADING_SECTIONS["valuation"])
    values.append(_table_text(valuation_section) or _section_text(valuation_section, 240))
    values.append(_section_text(_section(response, *_HEADING_SECTIONS["cross_asset"]), 240))
    tokens: set[str] = set()
    for value in values:
        tokens |= extract_number_tokens(value)
    return tokens | {token.lstrip("+-") for token in tokens}


def _stock_phrase(cell: str) -> str:
    match = re.match(r"(.+?)([+-]\d+(?:\.\d+)?%)$", cell)
    return f"{match.group(1)}{_spoken_change(match.group(2))}" if match else ""


def _is_bare_change(cell: str) -> bool:
    return bool(re.fullmatch(r"[+-]?\d+(?:\.\d+)?%", cell.strip()))


def _sector_stocks(cells: list[str]) -> list[str]:
    phrases: list[str] = []
    for cell in cells:
        if _is_bare_change(cell) or not cell:
            continue
        phrase = _stock_phrase(cell) if len(cell) <= 16 else ""
        if phrase:
            phrases.append(phrase)
            continue
        phrases.extend(
            f"{name}{_spoken_change(change)}"
            for name, change in re.findall(r"([\u4e00-\u9fff]{2,8})(?:\([^)]*\))?([+-]\d+(?:\.\d+)?%)", cell)
        )
    return phrases


def _sector_phrases(section: str, supported: set[str]) -> list[str]:
    if not section:
        return []
    groups = _sector_groups(section)
    chosen = _sector_rows(section)[:2]
    extra = next(
        (
            row
            for key in ("down", "up")
            for row in groups[key]
            if row not in chosen and _change_direction(_row_change(row)) == key
        ),
        None,
    )
    if extra is not None:
        chosen = [*chosen, extra]
    phrases: list[str] = []
    for row in chosen:
        change = _change_cell(_row_change(row))
        if not change:
            continue
        direction = _change_direction(change)
        stocks = _sector_stocks(row[1:])[:2]
        if direction not in {"up", "down"}:
            phrase = f"{row[0]}涨跌幅{change}"
        elif stocks and not _is_bare_change(row[1] if len(row) > 1 else ""):
            phrase = f"{row[0]}{'领涨' if direction == 'up' else '领跌'}：{'、'.join(stocks)}"
        else:
            phrase = f"{row[0]}{_spoken_change(change)}"
            if stocks:
                phrase += "，" + "、".join(stocks)
        phrase = _safe_value(phrase)
        if phrase and _numbers_supported(phrase, supported):
            phrases.append(phrase)
    return phrases


def build_daily_markdown_script(
    response: str,
    publication_date: str,
    *,
    min_chars: int = FINANCE_MIN_CHARS,
    max_chars: int = FINANCE_MAX_CHARS,
) -> str:
    supported = _supported_numbers(response, publication_date)
    summary_values = _summary_table(response)
    summary_match = re.search(r"(?m)一句话总结[：:]\s*\*{0,2}\s*(.+)$", response)
    summary = _readable_fragment(summary_match.group(1), 80, supported) if summary_match else ""
    if not summary:
        summary = _safe_value(_summary_value(summary_values, "盘面定性", "顶部摘要", "一句话总结"))
    if not summary:
        summary = _lead_fragment(_section(response, "顶部摘要"), 80, supported)

    index_section = _section(response, *_INDEX_SECTION_KEYWORDS)
    index_rows = _table_rows(index_section)
    indices = [f"{row[0]}报{row[1]}点，{_change_phrase(row[2])}" for row in index_rows if len(row) >= 3]
    index_summary = _safe_value(_summary_value(summary_values, "四大指数", "四指数"))

    sector_section = _section(response, "板块轮动")
    sector_phrases = _sector_phrases(sector_section, supported)
    sector_summary = _safe_value(_summary_value(summary_values, "板块轮动"))
    sector_detail = _lead_fragment(sector_section, 60, supported)

    keyword_section = _section(response, *_HEADING_SECTIONS["signals"])
    keywords = [text for text in _prose_candidates(keyword_section, supported) if len(text) <= 60][:3]
    keyword_summary = _safe_value(_summary_value(summary_values, "今日关键词", "三个信号", "核心信号"))

    interpretation = _section(response, *_HEADING_SECTIONS["interpretation"])
    advice_candidates = [text for text in _prose_candidates(interpretation, supported) if len(text) <= 90]
    advice = "".join(advice_candidates[:2]).strip("，。；：、 ")
    advice_summary = _safe_value(_summary_value(summary_values, "我的解读", "策略建议", "投资建议"))

    valuation_section = _section(response, *_HEADING_SECTIONS["valuation"])
    valuation_rows = _table_rows(valuation_section)[:3]
    valuation_detail = _safe_value(
        "；".join(f"{row[0]}PE{row[1]}，历史百分位{row[2]}，{row[3]}" for row in valuation_rows if len(row) >= 4)
    )
    valuation_summary = (
        _supported_value(_summary_value(summary_values, "估值水位", "估值"), supported) or valuation_detail
    )
    cross_asset_section = _section(response, *_HEADING_SECTIONS["cross_asset"])
    cross_asset_summary = _supported_value(
        _summary_value(summary_values, "黄金债市", "黄金", "债券"), supported
    ) or _lead_fragment(cross_asset_section, 110, supported)

    date_text = publication_date.replace("-", "年", 1).replace("-", "月", 1) + "日"
    index_label = _INDEX_LABELS.get(len(index_rows), "主要指数")
    tail_chars = len(FINANCE_RISK_NOTE) + len(DAILY_FINANCE_DISCLAIMER)
    parts: list[str] = []
    if index_rows and len(index_rows[0]) >= 3:
        subject, point, change = index_rows[0][:3]
        change = _change_cell(change)
        direction = _change_direction(change)
        if direction == "flat" and not any(marker in change for marker in ("持平", "平收")):
            parts.append(f"{change}，{subject}报{point}点，当日涨跌幅{change}。")
        else:
            spoken_change = _spoken_change(change)
            action = {"down": "直接跳水", "up": "强势拉升", "flat": "平收横住"}[direction]
            parts.append(f"{change}，{subject}报{point}点，{action}{spoken_change}。")
        parts.append(f"{date_text}A股收盘观察。")
    elif index_summary:
        parts.extend((f"{date_text}A股收盘观察。", "四大指数方面，" + index_summary + "。"))
    else:
        parts.append(f"{date_text}A股收盘观察。")
    if indices:
        parts.append(f"{index_label}方面，" + "；".join(indices) + "。")
    if summary:
        parts.append(summary + "。")
    if sector_phrases:
        parts.append("板块方面，" + "；".join(sector_phrases) + "。")
    elif sector_summary:
        parts.append("板块方面，" + sector_summary + "。")
    elif sector_detail:
        parts.append("板块方面，" + sector_detail + "。")

    optional: list[str] = []
    if keywords:
        optional.append("今天需要关注的信号是：" + "；".join(keywords) + "。")
    elif keyword_summary:
        optional.append("今天需要关注的信号是：" + keyword_summary + "。")
    if valuation_summary:
        optional.append("估值方面，" + valuation_summary + "。")
    if cross_asset_summary:
        optional.append("其他资产方面，" + cross_asset_summary + "。")
    if advice:
        optional.append("策略上，" + advice + "。")
    elif advice_summary:
        optional.append("策略上，" + advice_summary + "。")

    used = len("".join(parts)) + tail_chars
    for candidate in optional:
        if used + len(candidate) <= max_chars:
            parts.append(candidate)
            used += len(candidate)
    if used < min_chars:
        for sentence in advice_candidates:
            candidate = sentence.strip("，。；：、 ") + "。"
            if used + len(candidate) > max_chars:
                break
            if candidate in "".join(parts):
                continue
            parts.append(candidate)
            used += len(candidate)
            if used >= min_chars + 40:
                break

    script = _finalize_script(
        parts,
        required_tail=FINANCE_RISK_NOTE,
        disclaimer=DAILY_FINANCE_DISCLAIMER,
        min_chars=min_chars,
        max_chars=max_chars,
        length_error="finance narration must contain 300-600 characters; generated {length} characters",
        fillers=[
            "短期盘面只代表当天资金偏好，不能直接外推为中长期趋势。",
            "估值、成交和政策预期需要放在同一时间维度里交叉验证。",
        ],
    )
    validate_daily_finance_script(script)
    return script


def build_structured_finance_script(
    sources: dict[str, dict[str, Any]],
    *,
    publication_date: str | None = None,
    disclaimer: str = CONTENT_STUDIO_FINANCE_DISCLAIMER,
) -> str:
    gold = sources.get("gold") or {}
    bond = sources.get("bond") or {}
    macro = sources.get("macro-cn") or {}
    as_of = str(publication_date or gold.get("as_of") or bond.get("update_time") or date.today().isoformat())
    parts = [f"{as_of.replace('-', '年', 1).replace('-', '月', 1)}日金融市场观察。"]

    quotes = _as_dict(gold.get("gold_quotes"))
    sge = _as_dict(quotes.get("sge_benchmark"))
    au9999 = _as_dict(quotes.get("au9999"))
    futures = _as_dict(quotes.get("gold_futures_shfe"))
    central_bank = _as_dict(quotes.get("central_bank_gold"))
    gold_bits = []
    if futures.get("close") is not None:
        text = f"{futures.get('date', '最新交易日')}黄金期货主力收于{futures['close']}元每克"
        if futures.get("change_pct") is not None:
            text += f"，涨跌幅{futures['change_pct']}%"
        if futures.get("volume") is not None:
            text += f"，成交量{futures['volume']}"
        gold_bits.append(text)
    if au9999.get("close") is not None:
        text = f"Au99.99收于{au9999['close']}元每克"
        if au9999.get("change_pct") is not None:
            text += f"，涨跌幅{au9999['change_pct']}%"
        gold_bits.append(text)
    elif sge.get("evening_price") is not None or sge.get("morning_price") is not None:
        price = sge.get("evening_price") or sge.get("morning_price")
        gold_bits.append(f"{sge.get('date', '最新交易日')}上海金基准价{price}元每克")
    if central_bank.get("gold_reserves") is not None:
        reserve_date = central_bank.get("date", "最近一期")
        reserve_trend = central_bank.get("trend", "待核对")
        gold_bits.append(f"央行{reserve_date}黄金储备{central_bank['gold_reserves']}万盎司，趋势为{reserve_trend}")
    if gold_bits:
        parts.append("黄金方面，" + "；".join(gold_bits[:3]) + "。")
    has_gold = bool(gold_bits)

    bond_summary = str(bond.get("text_summary") or "").strip()
    has_bond = False
    if bond_summary:
        bond_summary = bond_summary.split("建议", 1)[0].rstrip("，。；： ")
        parts.append("债市方面，" + bond_summary + "。")
        has_bond = True
    else:
        market = _as_dict(bond.get("market"))
        rate_items = _as_list(market.get("barometerRateItemList"))
        rate_text = ""
        if rate_items:
            first = _as_dict(rate_items[0])
            rate_text = clean_markdown("".join(str(value) for value in first.values() if value is not None))[:60]
        if market:
            bits = [
                f"交易日{market.get('tradeDate')}" if market.get("tradeDate") else "",
                f"债市天气{market.get('weather')}" if market.get("weather") else "",
                rate_text,
            ]
            parts.append("债市方面，" + "，".join(bit for bit in bits if bit) + "。")
            has_bond = True

    monthly_records: list[dict[str, Any]] = []
    for frequency in macro.get("frequencies") or []:
        if isinstance(frequency, dict) and "月" in str(frequency.get("频率")):
            monthly_records = [item for item in frequency.get("记录") or [] if isinstance(item, dict)]
            break
    complete = next((item for item in reversed(monthly_records) if len(item) >= 4), None)
    latest = monthly_records[-1] if monthly_records else None
    has_macro = False
    if complete:
        metrics = []
        for key, label in (
            ("CPI:当月同比(%)", "CPI同比"),
            ("PPI:当月同比(%)", "PPI同比"),
            ("PMI(%)", "PMI"),
            ("M2:同比(%)", "M2同比"),
        ):
            if complete.get(key) is not None:
                metrics.append(f"{label}{complete[key]}%")
        if metrics:
            parts.append(f"宏观数据方面，{complete.get('日期', '最近一期')}公布的" + "、".join(metrics) + "。")
            has_macro = True
    if latest and latest is not complete and latest.get("PMI(%)") is not None:
        parts.append(f"更新到{latest.get('日期', '最近一期')}的PMI为{latest['PMI(%)']}%。")
        has_macro = True

    risk = _as_dict(gold.get("risk_indicators"))
    risk_bits = []
    vix = _as_dict(risk.get("vix"))
    dxy = _as_dict(risk.get("dxy"))
    exchprice = _as_dict(risk.get("exchprice"))
    if vix.get("INDICATOR_VAL") is not None:
        risk_bits.append(f"VIX为{vix['INDICATOR_VAL']}")
    if dxy.get("INDICATOR_VAL") is not None:
        risk_bits.append(f"美元指数{dxy['INDICATOR_VAL']}")
    if exchprice.get("EXCHPRICE") is not None:
        risk_bits.append(f"美元兑人民币{exchprice['EXCHPRICE']}")
    if risk_bits:
        parts.append("跨资产风险指标方面，" + "、".join(risk_bits) + "。")

    missing_modules = [
        label for label, present in (("黄金", has_gold), ("债市", has_bond), ("宏观", has_macro)) if not present
    ]
    if missing_modules:
        raise FinanceScriptError("金融初稿缺少关键模块：" + "、".join(missing_modules))

    news = _as_dict(gold.get("news"))
    news_items = [_as_dict(item) for item in _as_list(news.get("items"))]
    fresh_news = [
        clean_markdown(str(item.get("summary") or item.get("title") or ""))
        for item in news_items
        if item.get("publish_date") == str(as_of)[:10] and (item.get("summary") or item.get("title"))
    ]
    if fresh_news:
        parts.append("消息面，" + trim_complete(fresh_news[0], 72) + "。")

    for source_id, payload in sources.items():
        if not source_id.startswith("stock-") or not isinstance(payload, dict):
            continue
        name = payload.get("stock_name") or payload.get("query") or "关注股票"
        price = _value(payload, "current_price", "price", "latest_price")
        change = _value(payload, "change_percent", "change_pct", "pct_chg")
        if price is not None:
            text = f"个股方面，{name}最新价为{price}元"
            if change is not None:
                text += f"，涨跌幅{change}%"
            parts.append(text + "。")

    tail = disclaimer
    _append_until(
        parts,
        [
            "组合观察上，黄金上涨、债市震荡和PMI回落同时出现，说明避险与增长预期仍在拉扯。",
            "操作语气上，先把黄金和债市当作风险偏好温度计，再看宏观数据是否支持权益继续修复。",
            "发布前重点复核黄金交易日、债市更新时间和宏观指标月份，避免把滞后数据说成实时行情。",
            "如果关注股票缺失报价，本稿只覆盖大类资产和宏观环境，不替代个股复盘。",
        ],
        CONTENT_STUDIO_MIN_CHARS,
        tail,
    )
    script = _finalize_script(
        parts,
        disclaimer=disclaimer,
        min_chars=CONTENT_STUDIO_MIN_CHARS,
        max_chars=CONTENT_STUDIO_MAX_CHARS,
    )
    _validate_content_studio_finance_script(script)
    return script
