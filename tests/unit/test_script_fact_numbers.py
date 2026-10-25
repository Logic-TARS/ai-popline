import pytest

from content_pipeline.script_generation.models import ScriptPlan
from content_pipeline.script_generation.prompts import build_draft_prompts, build_repair_prompts
from content_pipeline.script_generation.quality import extract_quantity_tokens, quantity_token_supported
from content_pipeline.script_generation.service import _change_value, _point_value, build_structured_facts


@pytest.mark.parametrize(
    ("subject", "point", "change"),
    [
        ("上证指数", "3,813.79", "-1.85%"),
        ("深证成指", "12,345.67", "+0.25%"),
        ("沪深300", "4,698.43", "+0.25%"),
        ("科创50", "1,234.56", "-2.95%"),
        ("沪深300", "4698.43", "0.25%"),
        ("科创50", "1234.56", "0%"),
        ("上证指数", "3813.79", "+12.34点"),
        ("沪深300", "4,698.43", "-12.34点"),
        ("科创50", "1,234.56", "上涨12.34点"),
        ("上证指数", "3,813.79", "下跌1,012.34点"),
    ],
)
def test_index_facts_bind_complete_point_and_change(subject: str, point: str, change: str) -> None:
    facts = build_structured_facts("finance", {"indices": [[subject, point, change]]})
    fact = facts.facts[0]

    assert fact.kind == "index"
    assert fact.subject == subject
    assert fact.point_value == point
    assert fact.change_value == change.removeprefix("上涨").removeprefix("下跌")


@pytest.mark.parametrize(
    ("subject", "change", "expected"),
    [
        ("半导体", "+6.42%", "+6.42%"),
        ("沪深300行业", "-7.87%", "-7.87%"),
        ("科创50行业", "上涨1.25%", "1.25%"),
        ("5G通信", "下跌0.25个百分点", "0.25个百分点"),
        ("半导体", "+0.25个百分点", "+0.25个百分点"),
        ("半导体", "-0.25个百分点", "-0.25个百分点"),
        ("5G通信", "+12.34点", "+12.34点"),
        ("半导体", "下跌12.34点", "12.34点"),
        ("半导体", "+1,234.56点", "+1,234.56点"),
        ("沪深300行业", "-0.25", "-0.25"),
    ],
)
def test_sector_facts_do_not_use_name_digits_or_change_as_point(subject: str, change: str, expected: str) -> None:
    facts = build_structured_facts("finance", {"sector_rows": [[subject, change]]})
    fact = facts.facts[0]

    assert fact.kind == "sector"
    assert fact.change_value == expected
    assert fact.point_value is None


@pytest.mark.parametrize(
    ("text", "point", "change"),
    [
        ("上证指数报3813.79点", "3813.79", None),
        ("沪深300报4,698.43点", "4,698.43", None),
        ("科创50", None, None),
        ("暂无行情", None, None),
        ("1.25", None, "1.25"),
        ("12.34点", "12.34", None),
        ("沪深300上涨12.34点，收报4,698.43点", "4,698.43", "12.34点"),
        ("科创50下跌12.34点（-1.25%），收报1,234.56点", "1,234.56", "-1.25%"),
        ("沪深300 4,698.43点 +0.25 %", "4,698.43", "+0.25%"),
    ],
)
def test_market_numbers_require_value_context(text: str, point: str | None, change: str | None) -> None:
    assert _point_value(text) == point
    assert _change_value(text) == change


@pytest.mark.parametrize(
    ("source", "output", "supported"),
    [
        ("500亿美元", "500亿美元", True),
        ("500 亿 美元", "500亿美元", True),
        ("1,200亿美元", "1200亿美元", True),
        ("500亿美元", "500万元", False),
        ("500亿美元", "500亿元", False),
        ("500亿", "500万", False),
        ("3%", "3个百分点", False),
    ],
)
def test_structured_quantity_tokens_preserve_magnitude_currency_and_rate_units(source, output, supported) -> None:
    source_tokens = extract_quantity_tokens(source)
    output_tokens = extract_quantity_tokens(output)
    assert all(quantity_token_supported(token, source_tokens) for token in output_tokens) is supported


def test_finance_prompts_keep_full_source_point_in_opening() -> None:
    facts = build_structured_facts("finance", {"indices": [["上证指数", "3,813.79", "-1.85%"]]})
    plan = ScriptPlan(
        mainline="指数回落",
        main_fact_id="finance.index.1",
        opening="行情钩子",
        sections=["开场", "正文"],
        required_facts=["finance.index.1"],
    )
    _, draft = build_draft_prompts(facts, plan)
    _, repair = build_repair_prompts(facts, plan, "短稿。", [{"code": "length"}])

    for prompt in (draft, repair):
        assert "指定财经首句行情锚点：上证指数、3,813.79、-1.85%" in prompt
        assert "“3,813.79点，上证指数下跌1.85%，直接跳水。”" in prompt
        assert "“813.79点" not in prompt
