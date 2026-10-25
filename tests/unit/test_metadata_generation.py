from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from content_pipeline.metadata_generation import (
    RULE_DESCRIPTIONS,
    MetadataGenerationError,
    MetadataGenerationService,
    metadata_audit_summary,
    validate_metadata,
)
from content_pipeline.models import AiBriefingParams, ArtifactSet, FinanceParams
from content_pipeline.script_generation import build_structured_facts
from content_pipeline.settings import Settings

MATERIAL = {"summary": "Qwen计划开放新模型，测试工具调用能力。", "date": "20260819"}
SCRIPT = "Qwen计划开放新模型，测试工具调用能力。开发者仍需关注实际测试进展。"
TITLE = "Qwen计划开放模型"
DESCRIPTION = (
    "Qwen计划开放新模型，目前处于测试阶段，重点关注工具调用能力。开发者仍需关注实际测试进展，"
    "相关能力能否用于具体应用，还要结合后续开放情况观察。"
)


def response(text: str, ids: list[str] | None = None) -> str:
    return json.dumps({"text": text, "fact_ids": ids or ["ai.summary"]}, ensure_ascii=False)


class FakeClient:
    def __init__(self, *outputs: str | Exception):
        self.outputs = iter(outputs)
        self.calls: list[dict] = []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        value = next(self.outputs)
        if isinstance(value, Exception):
            raise value
        return value


def generate(tmp_path: Path, client: FakeClient, **kwargs):
    return MetadataGenerationService(Settings(_env_file=None), client=client).generate(
        content_type="ai_briefing",
        material=MATERIAL,
        script=SCRIPT,
        rule_title=TITLE,
        rule_description=RULE_DESCRIPTIONS["ai_briefing"],
        output_dir=tmp_path,
        **kwargs,
    )


def test_independent_stages_follow_script_and_audit(tmp_path: Path):
    client = FakeClient(response(TITLE), response(DESCRIPTION))
    result = generate(tmp_path, client, script_writer="llm")
    assert result["status"] == "succeeded"
    assert [call["label"] for call in client.calls] == [
        "ai_briefing metadata title",
        "ai_briefing metadata description",
    ]
    assert json.loads(client.calls[1]["user_prompt"])["final_title"] == TITLE
    assert result["description"]["final_text"] == DESCRIPTION
    for name in ("title_audit.json", "description_audit.json", "metadata_audit.json"):
        assert json.loads((tmp_path / name).read_text(encoding="utf-8"))["status"] == "succeeded"
    summary = metadata_audit_summary(result)
    assert summary.model_dump(mode="json") == {
        "status": "succeeded",
        "title": {
            "status": "succeeded",
            "generation_mode": "llm",
            "quality_passed": True,
            "error_codes": [],
            "warnings": [],
        },
        "description": {
            "status": "succeeded",
            "generation_mode": "llm",
            "quality_passed": True,
            "error_codes": [],
            "warnings": [],
        },
    }
    assert {"facts", "attempts", "final_text", "prompt_sha256"}.isdisjoint(json.dumps(summary.model_dump()))


def test_manual_priority_does_not_call_llm_or_shorten_title(tmp_path: Path):
    client = FakeClient()
    long_title = "人工确认的长标题" * 10
    result = generate(tmp_path, client, title=long_title, description="  手动描述  ", script_writer="llm")
    assert not client.calls
    assert result["title"]["final_text"] == long_title
    assert result["description"]["final_text"] == "手动描述"
    assert result["title"]["generation_mode"] == "manual"


def test_writers_are_independent_and_whitespace_means_auto(tmp_path: Path):
    client = FakeClient(response(DESCRIPTION))
    result = generate(tmp_path, client, title_writer="rule", description_writer="llm", description="  ")
    assert len(client.calls) == 1
    assert result["title"]["generation_mode"] == "rule"
    assert result["description"]["generation_mode"] == "llm"


def test_rule_mode_restores_old_description(tmp_path: Path):
    result = generate(tmp_path, FakeClient())
    assert result["description"]["final_text"] == "今日AI简报"
    assert result["description"]["generation_mode"] == "rule"


def test_invalid_json_and_quality_failures_are_repaired_with_limit(tmp_path: Path):
    client = FakeClient("not json", response("Qwen已发布新模型"), response(TITLE))
    service = MetadataGenerationService(Settings(_env_file=None), client=client)
    result = service.generate_title(
        facts=build_structured_facts("ai_briefing", MATERIAL),
        script=SCRIPT,
        rule_text=TITLE,
        audit_path=tmp_path / "title.json",
        writer="llm",
        repair_attempts=2,
    )
    assert len(client.calls) == 3
    assert result["generation_mode"] == "llm_repaired"
    assert "certainty_upgrade" in result["attempts"][1]["quality_report"]["errors"]
    assert json.loads(client.calls[2]["user_prompt"])["repair"]["errors"]


def test_failed_generation_audited_without_silent_fallback_or_error_body(tmp_path: Path):
    secret = "Bearer private-test-value https://server.invalid/?key=private"
    client = FakeClient(RuntimeError(secret))
    with pytest.raises(MetadataGenerationError) as caught:
        generate(tmp_path, client, script_writer="llm")
    assert secret not in str(caught.value)
    audit_text = (tmp_path / "metadata_audit.json").read_text(encoding="utf-8")
    assert secret not in audit_text
    audit = json.loads(audit_text)
    assert audit["status"] == "failed"
    assert audit["title"]["final_text"] is None
    assert audit["description"]["status"] == "not_started"
    assert len(client.calls) == 1


def test_explicit_validated_fallback_is_recorded(tmp_path: Path):
    client = FakeClient(RuntimeError("offline"))
    result = generate(tmp_path, client, title_writer="llm", failure_policy="validated_rule_fallback", repair_attempts=0)
    assert result["title"]["generation_mode"] == "validated_rule_fallback"
    assert result["title"]["fallback_reason"]
    assert result["title"]["quality_report"]["passed"]


def test_invalid_rule_fallback_cannot_bypass_validation(tmp_path: Path):
    service = MetadataGenerationService(Settings(_env_file=None), client=FakeClient(RuntimeError("offline")))
    with pytest.raises(MetadataGenerationError):
        service.generate_title(
            facts=build_structured_facts("ai_briefing", MATERIAL),
            script=SCRIPT,
            rule_text="Qwen保证收益99倍",
            audit_path=tmp_path / "title.json",
            writer="llm",
            failure_policy="validated_rule_fallback",
        )
    audit = json.loads((tmp_path / "title.json").read_text(encoding="utf-8"))
    assert audit["status"] == "failed"
    assert "prohibited_promise" in audit["attempts"][-1]["quality_report"]["errors"]


@pytest.mark.parametrize(
    ("text", "ids", "error"),
    [
        ("Qwen计划开放99个模型", ["ai.summary"], "unsupported_number"),
        (TITLE, ["ai.unknown"], "unknown_fact_id"),
        ("Qwen已发布新模型", ["ai.summary"], "certainty_upgrade"),
        ("Qwen计划开放模型" * 3, ["ai.summary"], "length_limit"),
        ("某个毫无关联主题", ["ai.summary"], "fact_anchor_missing"),
        ("Qwen计划保证收益", ["ai.summary"], "prohibited_promise"),
        ("Qwen计划联手FakeCo", ["ai.summary"], "unsupported_entity"),
        ("Qwen  计划  开放  新模型", ["ai.summary"], "length_limit"),
        ("Qwen计划\n开放模型", ["ai.summary"], "multiline_title"),
        ("**Qwen计划开放模型**", ["ai.summary"], "not_plain_text"),
        ("__Qwen计划开放模型__", ["ai.summary"], "not_plain_text"),
        ("<b>Qwen计划开放模型</b>", ["ai.summary"], "not_plain_text"),
    ],
)
def test_quality_rejects_bad_metadata(text, ids, error):
    report = validate_metadata(
        stage="title",
        text=text,
        fact_ids=ids,
        facts=build_structured_facts("ai_briefing", MATERIAL),
        script=SCRIPT,
        mode="llm",
    )
    assert error in report["errors"]
    assert not report["passed"]


def test_soft_metadata_gate_records_precision_errors_as_warnings() -> None:
    facts = build_structured_facts("ai_briefing", MATERIAL)
    common = {"stage": "title", "fact_ids": ["ai.summary"], "facts": facts, "script": SCRIPT, "mode": "llm"}
    too_long = TITLE * 3

    strict = validate_metadata(**common, text=too_long)
    assert strict["gate"] == "strict"
    assert strict["errors"] == ["length_limit"]
    assert not strict["passed"]

    soft = validate_metadata(**common, text=too_long, gate="soft")
    assert soft["gate"] == "soft"
    assert soft["passed"]
    assert soft["errors"] == []
    assert "length_limit" in soft["warnings"]


def test_soft_metadata_gate_keeps_grounding_errors_blocking() -> None:
    facts = build_structured_facts("ai_briefing", MATERIAL)
    common = {"stage": "title", "fact_ids": ["ai.summary"], "facts": facts, "script": SCRIPT, "mode": "llm"}

    ungrounded = validate_metadata(**common, text="毫无关联的新闻主题", gate="soft")
    assert not ungrounded["passed"]
    assert "fact_anchor_missing" in ungrounded["errors"]

    empty = validate_metadata(**common, text="", gate="soft")
    assert not empty["passed"]
    assert "empty_text" in empty["errors"]
    assert "empty_text" not in empty["warnings"]


def test_rejects_unknown_metadata_quality_gate() -> None:
    facts = build_structured_facts("ai_briefing", MATERIAL)
    with pytest.raises(ValueError, match="unknown metadata quality gate"):
        validate_metadata(
            stage="title", text=TITLE, fact_ids=["ai.summary"], facts=facts, script=SCRIPT, mode="llm", gate="lenient"
        )


def test_certainty_validation_scopes_mixed_summary_to_title_anchor():
    material = {"summary": "TypeSafe AI的Jev上线数周即完成8.7亿美元融资；谷歌Gemini 4即将发布，内部正在测试Carbon。"}
    facts = build_structured_facts("ai_briefing", material)
    common = {
        "stage": "title",
        "fact_ids": ["ai.summary"],
        "facts": facts,
        "script": material["summary"],
        "mode": "llm",
    }
    assert validate_metadata(**common, text="Jev数周完成8.7亿美元融资")["passed"]
    assert "certainty_upgrade" in validate_metadata(**common, text="谷歌已发布Gemini 4")["errors"]


def test_certainty_validation_rejects_upgrades_in_mixed_claims_and_keeps_attribution():
    material = {"summary": "Qwen计划发布新模型；Kimi可能开放编码能力；据报道称，Jev可能完成融资。"}
    facts = build_structured_facts("ai_briefing", material)
    common = {
        "stage": "description",
        "fact_ids": ["ai.summary"],
        "facts": facts,
        "script": material["summary"],
        "mode": "llm",
    }
    assert "certainty_upgrade" in validate_metadata(**common, text="Qwen已发布新模型。")["errors"]
    assert "certainty_upgrade" in validate_metadata(**common, text="Kimi已开放编码能力。")["errors"]
    assert "attribution_missing" in validate_metadata(**common, text="Jev可能完成融资。")["errors"]


def test_attribution_is_required_only_for_the_attributed_local_claim():
    material = {
        "summary": (
            "Karpathy 把全球 80 亿人分成四层，只有约五千人能接触实验室内部系统；"
            "同一天，亚马逊被曝洽谈收购 Decart，DeepSeek 融资规模被传至 120 亿美元。"
        )
    }
    facts = build_structured_facts("ai_briefing", material)
    common = {
        "stage": "title",
        "fact_ids": ["ai.summary"],
        "facts": facts,
        "script": material["summary"],
        "mode": "llm",
    }
    assert "attribution_missing" not in validate_metadata(**common, text="约五千人站前沿")["errors"]
    assert "attribution_missing" in validate_metadata(**common, text="亚马逊洽谈收购Decart")["errors"]
    assert "attribution_missing" not in validate_metadata(**common, text="亚马逊被曝洽谈收购Decart")["errors"]


def test_numbers_require_matching_units_and_script():
    facts = build_structured_facts("ai_briefing", {"summary": "Qwen模型支持2万用户。"})
    report = validate_metadata(
        stage="title",
        text="Qwen支持2亿用户",
        fact_ids=["ai.summary"],
        facts=facts,
        script="Qwen支持用户。",
        mode="llm",
    )
    assert {"unsupported_quantity", "number_not_in_script"} <= set(report["errors"])


def test_finance_description_risk_and_legacy_exception():
    facts = build_structured_facts("finance", {"indices": [["上证指数", "3000", "-1%"]]})
    args = {"stage": "description", "fact_ids": ["finance.index.1"], "facts": facts, "script": "上证指数下跌1%。"}
    report = validate_metadata(**args, text="上证指数下跌1%。", mode="llm")
    assert {"finance_disclaimer", "finance_risk_warning"} <= set(report["errors"])
    report = validate_metadata(**args, text="上证指数下跌1%，注意风险，不构成投资建议。", mode="llm")
    assert report["passed"]
    report = validate_metadata(**args, text=RULE_DESCRIPTIONS["finance"], mode="rule")
    assert report["passed"]
    assert "legacy_rule_description_uses_reference_notice" in report["warnings"]


@pytest.mark.parametrize("params_type", [AiBriefingParams, FinanceParams])
def test_params_defaults_bounds_and_old_artifacts(params_type):
    params = params_type()
    assert params.description == ""
    assert params.title_writer == params.description_writer == "follow_script"
    assert params.metadata_failure_policy == "fail"
    assert params.metadata_repair_attempts == 1
    for invalid in (-1, 3):
        with pytest.raises(ValidationError):
            params_type(metadata_repair_attempts=invalid)
    assert params_type(title="  ", description="  ").title is None
    assert ArtifactSet.model_validate({}).metadata_audit_path is None
    artifact = ArtifactSet.model_validate({"metadata_audit_path": "metadata_audit.json"})
    assert isinstance(artifact.metadata_audit_path, Path)


@pytest.mark.parametrize("params_type", [AiBriefingParams, FinanceParams])
def test_params_default_to_the_soft_quality_gate_and_round_trip(params_type):
    assert params_type().quality_gate == "soft"
    assert params_type(quality_gate="soft").quality_gate == "soft"
    assert params_type(quality_gate="strict").quality_gate == "strict"
    with pytest.raises(ValidationError):
        params_type(quality_gate="lenient")


def test_metadata_gate_reaches_the_validator_and_the_title_audit(tmp_path: Path):
    long_title = TITLE * 3
    result = generate(
        tmp_path,
        FakeClient(response(long_title)),
        script_writer="llm",
        title_writer="llm",
        description_writer="rule",
        gate="soft",
    )
    quality = result["title"]["quality_report"]
    assert quality["gate"] == "soft"
    assert quality["passed"]
    assert quality["errors"] == []
    assert "length_limit" in quality["warnings"]

    with pytest.raises(MetadataGenerationError):
        generate(
            tmp_path / "strict",
            FakeClient(response(long_title)),
            script_writer="llm",
            title_writer="llm",
            description_writer="rule",
            gate="strict",
        )


def test_retry_does_not_resurrect_old_description(tmp_path: Path):
    generate(tmp_path, FakeClient(response(TITLE), response(DESCRIPTION)), script_writer="llm")
    with pytest.raises(MetadataGenerationError):
        generate(tmp_path, FakeClient(RuntimeError("offline")), script_writer="llm")
    audit = json.loads((tmp_path / "metadata_audit.json").read_text(encoding="utf-8"))
    assert audit["description"]["status"] == "not_started"
    assert "final_text" not in audit["description"]


def test_rule_title_must_be_grounded_and_description_has_hard_limit():
    facts = build_structured_facts("ai_briefing", MATERIAL)
    report = validate_metadata(
        stage="title", text="毫无关联的新闻主题", fact_ids=[], facts=facts, script=SCRIPT, mode="rule"
    )
    assert "fact_anchor_missing" in report["errors"]
    report = validate_metadata(
        stage="description", text=DESCRIPTION * 6, fact_ids=["ai.summary"], facts=facts, script=SCRIPT, mode="llm"
    )
    assert "length_limit" in report["errors"]


def test_llm_title_counts_internal_spaces_and_description_end_is_advisory():
    facts = build_structured_facts("ai_briefing", MATERIAL)
    text = "Qwen 计划 开放 新模型"
    report = validate_metadata(
        stage="title", text=text, fact_ids=["ai.summary"], facts=facts, script=SCRIPT, mode="llm"
    )
    assert report["passed"]
    assert report["character_count"] == len(text)
    report = validate_metadata(
        stage="description",
        text=DESCRIPTION.rstrip("。"),
        fact_ids=["ai.summary"],
        facts=facts,
        script=SCRIPT,
        mode="llm",
    )
    assert report["passed"]
    assert "sentence_end_missing" in report["warnings"]


@pytest.mark.parametrize(
    ("source", "output", "passed"),
    [
        ("200 亿", "200亿", True),
        ("200 亿 美元", "200亿美元", True),
        ("1,200 亿美元", "1200亿美元", True),
        ("1，200 亿美元", "1,200亿美元", True),
        ("1,200,000 元", "1200000元", True),
        ("3 个百分点", "3个百分点", True),
        ("3 百分点", "3百分点", True),
        ("200 亿美元", "200亿元", False),
        ("200 亿美元", "200亿", False),
        ("200 亿", "200亿美元", False),
        ("1200 亿", "200亿", False),
        ("300 亿", "3亿", False),
        ("3%", "3百分点", False),
        ("3百分点", "3%", False),
        ("3%", "3个百分点", False),
        ("3个百分点", "3%", False),
        ("1,200 亿，另有200名员工", "200亿", False),
        ("300 亿，另有3名员工", "3亿", False),
        ("1.90 万亿", "1.90万亿元", True),
        ("1.90 万亿元", "1.90万亿", True),
        ("1.90 万亿元", "1.90亿元", False),
        ("200 亿美元", "200元", False),
    ],
)
def test_quantity_comparison_uses_normalized_complete_tokens(source, output, passed):
    facts = build_structured_facts("ai_briefing", {"summary": f"OpenAI营收差{source}。"})
    report = validate_metadata(
        stage="description",
        text=f"OpenAI营收差{output}。",
        fact_ids=["ai.summary"],
        facts=facts,
        script=f"OpenAI营收差{source}。",
        mode="llm",
    )
    assert report["passed"] is passed, report
    if not passed:
        assert "unsupported_quantity" in report["errors"]


@pytest.mark.parametrize("text", ["OpenAI营收口径差200亿", "OpenAI年化营收少约200亿"])
def test_real_ai_revenue_title_with_spaced_source_quantity(text):
    facts = build_structured_facts("ai_briefing", {"summary": "OpenAI年化营收逼近500亿美元，比外界预期少约 200 亿。"})
    report = validate_metadata(
        stage="title",
        text=text,
        fact_ids=["ai.summary"],
        facts=facts,
        script="OpenAI年化营收少约200亿。",
        mode="llm",
    )
    assert report["passed"], report


@pytest.mark.parametrize(
    ("text", "passed"),
    [
        ("上证指数上涨1%，沪深300上涨3%。", True),
        ("上证指数报3,000点，上涨1%，沪深300报4000点，上涨3%。", True),
        ("上证指数上涨3%，沪深300上涨1%。", False),
        ("上证指数上涨3%。", False),
        ("上证指数报4000点，上涨1%。", False),
        ("3%，上证指数上涨。", False),
        ("院线上涨2%，银行上涨4%。", True),
        ("院线上涨4%，银行上涨2%。", False),
    ],
)
def test_market_numbers_are_bound_to_subject_not_cited_union(text, passed):
    facts = build_structured_facts(
        "finance",
        {
            "indices": [["上证指数", "3000", "+1%"], ["沪深300", "4000", "+3%"]],
            "sector_rows": [["院线", "+2%"], ["银行", "+4%"]],
        },
    )
    report = validate_metadata(
        stage="description",
        text=text + "注意风险，不构成投资建议。",
        fact_ids=[fact.id for fact in facts.facts],
        facts=facts,
        script="上证指数报3000点，上涨1%，沪深300报4000点，上涨3%，院线上涨2%，银行上涨4%。",
        mode="llm",
    )
    assert report["passed"] is passed, report
    if not passed:
        assert "market_number_binding" in report["errors"]


@pytest.mark.parametrize(
    ("text", "passed"),
    [
        ("2026年10月9日A股收盘，上证指数涨1%，成交1.90万亿。", True),
        ("上证指数报4000点，上涨3%。", False),
        ("上证指数成交1.90万亿，沪深300上涨3%。", True),
        ("院线成交1.90万亿，银行上涨4%。", True),
    ],
)
def test_market_binding_ignores_market_wide_numbers(text, passed):
    facts = build_structured_facts(
        "finance",
        {
            "publication_date": "2026-10-09",
            "indices": [["上证指数", "3000", "+1%"], ["沪深300", "4000", "+3%"]],
            "sector_rows": [["院线", "+2%"], ["银行", "+4%"]],
            "keywords": ["成交1.90万亿"],
        },
    )
    report = validate_metadata(
        stage="description",
        text=text + "注意风险，不构成投资建议。",
        fact_ids=[fact.id for fact in facts.facts],
        facts=facts,
        script="2026年10月9日A股收盘。上证指数报3000点，上涨1%，沪深300报4000点，上涨3%，院线上涨2%，银行上涨4%，成交1.90万亿。",
        mode="llm",
    )
    assert report["passed"] is passed, report
    if not passed:
        assert "market_number_binding" in report["errors"]


@pytest.mark.parametrize(
    ("text", "passed"),
    [
        ("五指数尾盘V型收红", True),
        ("成交1.90万亿", True),
        ("2026-10-09", False),
    ],
)
def test_keyword_context_facts_can_anchor_but_dates_cannot(text, passed):
    facts = build_structured_facts(
        "finance",
        {
            "publication_date": "2026-10-09",
            "keywords": ["五指数早盘低开深水，尾盘V型收红", "成交1.90万亿"],
        },
    )
    script = "五指数早盘低开深水，尾盘V型收红，成交1.90万亿。"
    if text == "2026-10-09":
        fact_ids = ["finance.date"]
    else:
        fact_ids = [fact.id for fact in facts.facts if fact.id != "finance.date"]
    report = validate_metadata(stage="title", text=text, fact_ids=fact_ids, facts=facts, script=script, mode="llm")
    assert report["passed"] is passed, report
    if not passed:
        assert "fact_anchor_missing" in report["errors"]


def test_metadata_rejects_new_named_entities_actions_claim_upgrades_and_direction_reversal():
    facts = build_structured_facts(
        "ai_briefing",
        {"summary": "OpenAI计划发布模型，可能提升工具调用能力。", "focus_sentences": ["微软计划测试新功能。"]},
    )
    script = "OpenAI计划发布模型，可能提升工具调用能力。微软计划测试新功能。"
    common = {
        "stage": "description",
        "fact_ids": ["ai.summary", "ai.focus.1"],
        "facts": facts,
        "script": script,
        "mode": "llm",
    }
    assert (
        "unsupported_entity" in validate_metadata(**common, text="腾讯计划发布模型，可能提升工具调用能力。")["errors"]
    )
    assert (
        "unsupported_action_or_causality"
        in validate_metadata(**common, text="OpenAI收购模型，可能提升工具调用能力。")["errors"]
    )
    assert "certainty_upgrade" in validate_metadata(**common, text="OpenAI可能发布模型，微软已发布新功能。")["errors"]
    assert "certainty_upgrade" not in validate_metadata(**common, text="OpenAI发布模型，微软计划测试新功能。")["errors"]

    market = build_structured_facts("finance", {"indices": [["上证指数", "3000", "+1%"]]})
    report = validate_metadata(
        stage="description",
        text="上证指数并未上涨1%，注意风险，不构成投资建议。",
        fact_ids=["finance.index.1"],
        facts=market,
        script="上证指数上涨1%。",
        mode="llm",
    )
    assert "direction_mismatch" in report["errors"]


def test_manual_metadata_still_skips_fact_checking():
    facts = build_structured_facts("ai_briefing", MATERIAL)
    report = validate_metadata(
        stage="title", text="腾讯收购百度500亿元", fact_ids=[], facts=facts, script=SCRIPT, mode="manual"
    )
    assert report["passed"]
    assert report["warnings"] == ["manual_override_not_fact_checked"]
