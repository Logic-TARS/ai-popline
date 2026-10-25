from __future__ import annotations

import json

import pytest

from content_pipeline.script_generation.client import OpenAICompatibleScriptClient
from content_pipeline.script_generation.models import ScriptPlan
from content_pipeline.script_generation.prompts import build_draft_prompts, build_plan_prompts
from content_pipeline.script_generation.quality import validate_script
from content_pipeline.script_generation.service import ScriptGenerationService, build_structured_facts, validate_plan
from content_pipeline.settings import Settings
from content_pipeline.tools import briefing_script_writer

AI_MATERIAL = {
    "date": "20260720",
    "date_text": "2026年7月20日",
    "title": "国产模型集中亮相",
    "summary": "国产模型团队发布开源模型，编码和工具调用成为焦点。",
    "focus_titles": ["Qwen发布新模型", "Kimi编码评测领先"],
    "focus_sentences": ["Qwen发布新模型并开放权重。", "Kimi在多个前端编码任务中取得领先。"],
    "frontier_titles": ["Nvidia建设AI工厂"],
    "frontier_sentences": ["Nvidia将部署CPU和GPU服务科研与企业。"],
    "quick_items": ["端侧小模型继续提速"],
    "fallback_script": "",
}


def _ai_script() -> str:
    script = (
        "Qwen发布新模型并开放权重，国产模型集中亮相。2026年7月20日的AI简报关注编码和工具调用，"
        "开发者生态有望扩大。Kimi在多个前端编码任务中取得领先，"
        "模型竞争正从参数规模转向真实生产力。海外方面，Nvidia将建设AI工厂，部署CPU和GPU服务科研与企业。"
        "端侧小模型也在提速，厂商继续把能力推向消费设备。整体看，模型能力、算力供给和产品落地正在同步推进。"
        "今天的重点不只是单次发布，而是开源生态和工具调用能否稳定进入生产流程。开发者还要继续观察权重开放后的"
        "部署成本、运行稳定性和社区反馈，企业则需要核对实际交付能力。接下来，谁能把模型能力稳定转化为产品体验，"
        "谁就更可能在这一轮竞争中占据主动。"
    )
    return script + (
        "这些变化仍需结合后续测试和实际应用效果持续观察，不能只看发布当天的热度。"
        "对于普通用户，真正有价值的是能力能否稳定、低成本地进入日常工作，而不是宣传中的单项成绩。"
    )


class FakeClient:
    def __init__(self, responses: list[str]):
        self.responses = iter(responses)
        self.calls: list[str] = []
        self.user_prompts: list[str] = []

    def complete(self, *, label: str, user_prompt: str, **_kwargs) -> str:
        self.calls.append(label)
        self.user_prompts.append(user_prompt)
        return next(self.responses)


def test_quality_rejects_directly_repeated_chinese_words_without_flagging_normal_reduplication() -> None:
    facts = build_structured_facts("ai_briefing", AI_MATERIAL)
    repeated = _ai_script().replace("国产模型集中亮相", "国产模型直接直接亮相")
    report = validate_script(repeated, facts)
    assert any(issue.code == "repeated_chinese_word" and issue.evidence == "直接直接" for issue in report.issues)

    normal = _ai_script().replace("国产模型集中亮相", "国产模型慢慢亮相")
    assert all(issue.code != "repeated_chinese_word" for issue in validate_script(normal, facts).issues)


def test_soft_gate_records_the_same_issues_as_warnings_without_failing() -> None:
    facts = build_structured_facts("ai_briefing", AI_MATERIAL)
    short = "Qwen发布新模型。"

    strict = validate_script(short, facts)
    assert strict.gate == "strict"
    assert not strict.passed
    assert {issue.code for issue in strict.errors} == {"length", "required_fact_missing"}

    soft = validate_script(short, facts, gate="soft")
    assert soft.gate == "soft"
    assert soft.passed
    assert [issue.code for issue in soft.issues] == [issue.code for issue in strict.issues]
    assert [(issue.message, issue.evidence) for issue in soft.issues] == [
        (issue.message, issue.evidence) for issue in strict.issues
    ]
    assert all(issue.severity == "warning" for issue in soft.issues)


def test_rejects_unknown_script_quality_gate() -> None:
    facts = build_structured_facts("ai_briefing", AI_MATERIAL)
    with pytest.raises(ValueError, match="unknown script quality gate"):
        validate_script(_ai_script(), facts, gate="lenient")


def test_soft_gate_keeps_finance_compliance_checks_blocking() -> None:
    facts = build_structured_facts(
        "finance",
        {
            "indices": [["上证指数", "3882.41", "-1.85%"]],
            "risk_note": "面对市场波动，请结合风险承受能力审慎决策。",
            "disclaimer": "不构成投资建议。",
        },
    )
    instruction = validate_script(
        "建议买入。面对市场波动，请结合风险承受能力审慎决策。不构成投资建议。", facts, gate="soft"
    )
    assert not instruction.passed
    assert "finance_prohibited" in {issue.code for issue in instruction.errors}

    missing_disclaimer = validate_script("建议观望市场表现。", facts, gate="soft")
    assert not missing_disclaimer.passed
    assert {"finance_disclaimer", "finance_risk_warning"} <= {issue.code for issue in missing_disclaimer.errors}


def test_service_runs_plan_draft_and_targeted_repair() -> None:
    plan = json.dumps(
        {
            "mainline": "国产模型竞争转向生产力",
            "main_fact_id": "ai.summary",
            "opening": "日期和主线",
            "sections": ["国产模型", "海外算力", "落地观察"],
            "required_facts": ["ai.summary", "ai.focus.1", "ai.focus.2"],
            "risk_close": None,
        },
        ensure_ascii=False,
    )
    client = FakeClient([plan, "短稿。", _ai_script()])
    result = ScriptGenerationService(Settings(_env_file=None), client=client).generate(
        content_type="ai_briefing",
        material=AI_MATERIAL,
        fallback_script=None,
        repair_attempts=1,
    )

    assert result.audit.generation_mode == "llm_repaired"
    assert [attempt.stage for attempt in result.audit.attempts] == ["plan", "draft", "repair"]
    assert result.audit.quality_report.passed is True
    assert client.calls == ["ai_briefing script plan", "ai_briefing script draft", "ai_briefing script repair"]
    assert '"code": "length"' in client.user_prompts[-1]


def test_ai_weak_opening_is_repaired_with_targeted_instruction() -> None:
    plan = json.dumps(
        {
            "mainline": "国产模型竞争转向生产力",
            "main_fact_id": "ai.summary",
            "opening": "主体和变化先行",
            "sections": ["国产模型", "海外算力"],
            "required_facts": ["ai.summary", "ai.focus.1", "ai.focus.2"],
            "risk_close": None,
        },
        ensure_ascii=False,
    )
    weak_script = "2026年7月20日每日AI简报，今天聚焦国产模型。" + _ai_script()
    client = FakeClient([plan, weak_script, _ai_script()])
    result = ScriptGenerationService(Settings(_env_file=None), client=client).generate(
        content_type="ai_briefing",
        material=AI_MATERIAL,
        fallback_script=None,
        repair_attempts=1,
    )

    assert result.audit.quality_report.passed is True
    assert "ai_weak_opening" in client.user_prompts[-1]
    assert "仅重排首句为主体、具体变化和最强事实锚点" in client.user_prompts[-1]


def test_service_retries_invalid_plan_json_once_with_a_corrective_prompt() -> None:
    valid_plan = json.dumps(
        {
            "mainline": "国产模型竞争转向生产力",
            "main_fact_id": "ai.summary",
            "opening": "主体和变化先行",
            "sections": ["国产模型", "海外算力"],
            "required_facts": ["ai.summary", "ai.focus.1", "ai.focus.2"],
            "risk_close": None,
        },
        ensure_ascii=False,
    )
    client = FakeClient(["not-json", valid_plan, _ai_script()])
    result = ScriptGenerationService(Settings(_env_file=None), client=client).generate(
        content_type="ai_briefing", material=AI_MATERIAL, fallback_script=None, repair_attempts=0
    )

    assert [attempt.stage for attempt in result.audit.attempts] == ["plan", "plan", "draft"]
    assert result.audit.attempts[0].output == "not-json"
    assert result.audit.attempts[0].error == "script plan LLM returned invalid JSON"
    assert "上一次输出未通过结构或计划校验" in client.user_prompts[1]


def test_service_retries_plan_contract_failure_but_not_network_failure() -> None:
    invalid_plan = json.dumps(
        {
            "mainline": "国产模型竞争转向生产力",
            "main_fact_id": "ai.summary",
            "opening": "主体和变化先行",
            "sections": ["国产模型", "海外算力"],
            "required_facts": ["ai.focus.1", "ai.focus.2"],
            "risk_close": None,
        },
        ensure_ascii=False,
    )
    valid_plan = invalid_plan.replace('["ai.focus.1", "ai.focus.2"]', '["ai.summary", "ai.focus.1", "ai.focus.2"]')
    client = FakeClient([invalid_plan, valid_plan, _ai_script()])
    result = ScriptGenerationService(Settings(_env_file=None), client=client).generate(
        content_type="ai_briefing", material=AI_MATERIAL, fallback_script=None, repair_attempts=0
    )
    assert result.audit.attempts[0].error == "script plan main_fact_id must also appear in required_facts"
    assert len(client.calls) == 3

    class NetworkFailingClient:
        def __init__(self) -> None:
            self.calls = 0

        def complete(self, **_kwargs) -> str:
            self.calls += 1
            raise RuntimeError("network unavailable")

    failing = NetworkFailingClient()
    with pytest.raises(Exception, match="script generation failed"):
        ScriptGenerationService(Settings(_env_file=None), client=failing).generate(
            content_type="ai_briefing", material=AI_MATERIAL, fallback_script=None
        )
    assert failing.calls == 1


def test_service_uses_only_validated_rule_fallback() -> None:
    material = {**AI_MATERIAL, "fallback_script": _ai_script()}
    result = ScriptGenerationService(Settings(_env_file=None), client=FakeClient(["not-json"])).generate(
        content_type="ai_briefing",
        material=material,
        fallback_script=_ai_script(),
        failure_policy="validated_rule_fallback",
    )
    assert result.audit.generation_mode == "validated_rule_fallback"
    assert result.audit.fallback_reason

    with pytest.raises(Exception, match="script generation failed"):
        ScriptGenerationService(Settings(_env_file=None), client=FakeClient(["not-json"])).generate(
            content_type="ai_briefing",
            material=material,
            fallback_script="短稿。",
            failure_policy="validated_rule_fallback",
        )


def test_ai_opening_validation_requires_subject_change_and_anchor() -> None:
    facts = build_structured_facts("ai_briefing", AI_MATERIAL)
    weak = _ai_script().replace(
        "Qwen发布新模型并开放权重，国产模型集中亮相。",
        "北京时间今天凌晨，旧金山举行了一场发布会。",
    )
    assert any(issue.code == "ai_weak_opening" for issue in validate_script(weak, facts).issues)

    dated_but_specific = _ai_script().replace(
        "Qwen发布新模型并开放权重，国产模型集中亮相。",
        "2026年7月20日，Qwen发布新模型并开放权重。",
    )
    assert all(issue.code != "ai_weak_opening" for issue in validate_script(dated_but_specific, facts).issues)


def test_ai_strong_attraction_words_are_not_prohibited() -> None:
    facts = build_structured_facts("ai_briefing", AI_MATERIAL)
    script = _ai_script().replace("国产模型集中亮相", "国产模型集中亮相，很猛")
    assert all(issue.code != "ai_prohibited" for issue in validate_script(script, facts).issues)


def test_ai_opening_requires_selected_source_number_and_repairs_it() -> None:
    material = {
        **AI_MATERIAL,
        "summary": "微软与英伟达联手，把1万亿参数级AI搬进Windows。",
        "focus_sentences": ["微软与英伟达把1万亿参数模型带进Windows设备端。"],
    }
    facts = build_structured_facts("ai_briefing", material)
    missing = _ai_script().replace(
        "Qwen发布新模型并开放权重，国产模型集中亮相。", "微软与英伟达联手，把万亿参数级AI搬进Windows。"
    )
    assert any(issue.code == "ai_opening_number_missing" for issue in validate_script(missing, facts).issues)

    hooked = _ai_script().replace(
        "Qwen发布新模型并开放权重，国产模型集中亮相。",
        "1万亿参数进Windows，微软英伟达直接推本地AI。",
    )
    codes = {issue.code for issue in validate_script(hooked, facts).issues}
    assert "ai_opening_number_missing" not in codes
    assert "ai_opening_hook_weak" not in codes


def test_ai_opening_date_does_not_satisfy_number_hook() -> None:
    material = {
        **AI_MATERIAL,
        "summary": "微软与英伟达联手，把1万亿参数级AI搬进Windows。",
    }
    facts = build_structured_facts("ai_briefing", material)
    script = _ai_script().replace(
        "Qwen发布新模型并开放权重，国产模型集中亮相。",
        "2026年10月8日，微软与英伟达把万亿参数级AI搬进Windows。",
    )
    assert any(issue.code == "ai_opening_number_missing" for issue in validate_script(script, facts).issues)


def test_ai_opening_without_informative_number_is_not_required() -> None:
    facts = build_structured_facts("ai_briefing", AI_MATERIAL)
    assert all(issue.code != "ai_opening_number_missing" for issue in validate_script(_ai_script(), facts).issues)


def test_ai_prompt_does_not_require_number_hook_when_selected_facts_only_have_dates() -> None:
    material = {
        "date_text": "2026年7月20日",
        "summary": "国产模型团队发布开源模型。",
        "focus_sentences": ["Qwen发布新模型并开放权重。", "Kimi测试新的编码能力。"],
    }
    facts = build_structured_facts("ai_briefing", material)
    plan = ScriptPlan(
        mainline="国产模型发布",
        main_fact_id="ai.summary",
        opening="主体和动作先行",
        sections=["发布", "测试"],
        required_facts=["ai.summary", "ai.focus.1", "ai.focus.2"],
    )
    prompt = build_draft_prompts(facts, plan)[1]

    assert "若没有非日期业务数字" in prompt
    assert "指定首句钩子" not in prompt


def test_ai_opening_reports_warning_for_late_or_long_number_hook() -> None:
    material = {**AI_MATERIAL, "summary": "微软与英伟达联手，把1万亿参数级AI搬进Windows。"}
    facts = build_structured_facts("ai_briefing", material)
    script = _ai_script().replace(
        "Qwen发布新模型并开放权重，国产模型集中亮相。",
        "微软与英伟达这次在Windows设备端的本地AI布局里，带来了1万亿参数模型。",
    )
    warning = next(issue for issue in validate_script(script, facts).issues if issue.code == "ai_opening_hook_weak")
    assert warning.severity == "warning"


def test_finance_quality_allows_negated_full_position_but_blocks_advice() -> None:
    material = {
        "publication_date": "2026-07-16",
        "summary": "指数回落，板块分化。",
        "indices": [["上证指数", "3882.41", "-1.85%"]],
        "sector_rows": [["院线", "+6.42%"]],
        "risk_note": "面对市场波动，请结合风险承受能力审慎决策。",
        "disclaimer": "不构成投资建议。",
    }
    facts = build_structured_facts("finance", material)
    base = (
        "2026年7月16日市场观察。指数方面，上证指数报3882.41点，下跌1.85%，市场短线承压。"
        "板块方面，院线上涨6.42%，资金在防御与成长方向之间切换。不要满仓追涨，更不能把单日行情外推成长期趋势。"
        "后续需要观察成交变化、板块持续性和指数承接力度，并结合个人期限与仓位安排做判断。"
        "面对市场波动，请结合自身风险承受能力审慎决策。以上内容仅为市场信息整理，不构成投资建议。"
    )
    script = base + "短期波动可能反复，任何操作都应建立在独立核对数据和理解风险的基础上。" * 3
    report = validate_script(script, facts)
    assert all(issue.code != "finance_prohibited" for issue in report.issues)

    blocked = validate_script(script.replace("不要满仓", "建议满仓"), facts)
    assert any(issue.code == "finance_prohibited" for issue in blocked.issues)


@pytest.mark.parametrize("cue", ["建议", "应当", "可以", "立即"])
@pytest.mark.parametrize("action", ["买入", "卖出", "加仓", "减仓", "清仓", "满仓", "梭哈", "抄底", "止盈", "上车"])
def test_finance_quality_blocks_affirmative_trade_instructions(cue: str, action: str) -> None:
    facts = build_structured_facts(
        "finance",
        {
            "indices": [["上证指数", "3882.41", "-1.85%"]],
            "risk_note": "面对市场波动，请结合风险承受能力审慎决策。",
            "disclaimer": "不构成投资建议。",
        },
    )
    report = validate_script(f"{cue}{action}。面对市场波动，请结合风险承受能力审慎决策。不构成投资建议。", facts)

    assert any(issue.code == "finance_prohibited" and issue.evidence == action for issue in report.errors)


@pytest.mark.parametrize("negation", ["不要", "不宜", "不可", "避免", "切勿", "不能", "别"])
def test_finance_quality_allows_negated_trade_instruction_but_not_another_violation(negation: str) -> None:
    facts = build_structured_facts(
        "finance",
        {
            "indices": [["上证指数", "3882.41", "-1.85%"]],
            "risk_note": "面对市场波动，请结合风险承受能力审慎决策。",
            "disclaimer": "不构成投资建议。",
        },
    )
    allowed = f"{negation}买入，也{negation}止盈。面对市场波动，请结合风险承受能力审慎决策。不构成投资建议。"
    assert all(issue.code != "finance_prohibited" for issue in validate_script(allowed, facts).errors)

    mixed = allowed + "建议清仓。"
    assert any(issue.code == "finance_prohibited" for issue in validate_script(mixed, facts).errors)


def test_finance_opening_and_high_tension_words_require_bound_market_fact() -> None:
    material = {
        "indices": [["创业板指", "3692.46", "-2.95%"]],
        "risk_note": "面对市场波动，请结合风险承受能力审慎决策。",
        "disclaimer": "不构成投资建议。",
    }
    facts = build_structured_facts("finance", material)
    filler = "盘面分化仍需结合成交、估值和持续性观察，单日表现不能直接外推为长期趋势。" * 5
    valid = (
        "-2.95%，创业板指报3692.46点，直接跳水下跌。"
        + filler
        + "面对市场波动，请结合风险承受能力审慎决策。不构成投资建议。"
    )
    valid_codes = {issue.code for issue in validate_script(valid, facts).issues}
    assert not {"finance_opening_market_anchor_missing", "finance_hype_unbound"} & valid_codes

    unbound = valid.replace("直接跳水下跌", "市场彻底颠覆，抄底机会来了")
    unbound_codes = {issue.code for issue in validate_script(unbound, facts).issues}
    assert "finance_hype_unbound" in unbound_codes

    weak = valid.replace("-2.95%，创业板指报3692.46点，直接跳水下跌", "2026年7月16日A股收盘观察")
    weak_report = validate_script(weak, facts)
    weak_codes = {issue.code for issue in weak_report.issues}
    assert {"finance_weak_opening", "finance_opening_market_anchor_missing"} <= weak_codes


def test_finance_hype_ignores_distant_trade_language_but_blocks_nearby_inference() -> None:
    material = {
        "indices": [["创业板指", "3692.46", "-2.95%"]],
        "risk_note": "面对市场波动，请结合风险承受能力审慎决策。",
        "disclaimer": "不构成投资建议。",
    }
    facts = build_structured_facts("finance", material)
    filler = "盘面分化仍需结合成交、估值和持续性观察，单日表现不能直接外推为长期趋势。" * 5
    script = (
        "-2.95%，创业板指报3692.46点，直接跳水下跌。"
        + filler
        + "不要把单日波动当作抄底机会，也不要据此交易。"
        + "面对市场波动，请结合风险承受能力审慎决策。不构成投资建议。"
    )
    codes = {issue.code for issue in validate_script(script, facts).issues}
    assert "finance_hype_unbound" not in codes

    nearby_inference = script.replace("直接跳水下跌", "直接跳水，抄底机会来了")
    nearby_codes = {issue.code for issue in validate_script(nearby_inference, facts).issues}
    assert "finance_hype_unbound" in nearby_codes


def test_plan_rejects_unknown_and_missing_required_fact_ids() -> None:
    facts = build_structured_facts("ai_briefing", AI_MATERIAL)
    valid = ScriptPlan(
        mainline="国产模型竞争",
        main_fact_id="ai.summary",
        opening="直接开场",
        sections=["焦点", "影响"],
        required_facts=["ai.summary", "ai.focus.1", "ai.focus.2"],
    )
    validate_plan(valid, facts)

    with pytest.raises(Exception, match="unknown fact ids"):
        validate_plan(valid.model_copy(update={"required_facts": [*valid.required_facts, "ai.unknown"]}), facts)
    with pytest.raises(Exception, match="omitted required fact ids"):
        validate_plan(valid.model_copy(update={"required_facts": ["ai.summary"]}), facts)


def test_plan_requires_main_fact_in_declared_required_and_sections() -> None:
    facts = build_structured_facts("ai_briefing", AI_MATERIAL)
    plan = ScriptPlan(
        mainline="国产模型竞争",
        main_fact_id="ai.focus.1",
        opening="直接开场",
        sections=["焦点", "影响"],
        required_facts=["ai.summary", "ai.focus.1", "ai.focus.2"],
    )
    with pytest.raises(Exception, match="main_fact_id must also appear"):
        validate_plan(plan.model_copy(update={"required_facts": ["ai.summary", "ai.focus.2"]}), facts)
    with pytest.raises(Exception, match="unknown fact ids"):
        validate_plan(
            plan.model_copy(
                update={
                    "main_fact_id": "ai.unknown",
                    "required_facts": ["ai.summary", "ai.focus.1", "ai.focus.2", "ai.unknown"],
                }
            ),
            facts,
        )
    with pytest.raises(Exception, match="sections must include main_fact_id"):
        validate_plan(plan.model_copy(update={"section_fact_ids": {"main": ["ai.summary", "ai.focus.2"]}}), facts)


def test_prompts_and_number_allowlist_exclude_fallback_material() -> None:
    marker = "规则兜底秘密文本98765"
    material = {**AI_MATERIAL, "fallback_script": marker, "fallback_error": "private failure 54321"}
    facts = build_structured_facts("ai_briefing", material)
    plan = ScriptPlan(
        mainline="国产模型竞争",
        main_fact_id="ai.summary",
        opening="直接开场",
        sections=["焦点", "影响"],
        required_facts=["ai.summary", "ai.focus.1", "ai.focus.2"],
    )
    plan_prompt = build_plan_prompts(facts)[1]
    draft_prompt = build_draft_prompts(facts, plan)[1]

    assert marker not in plan_prompt
    assert marker not in draft_prompt
    assert "98765" not in facts.source_numbers
    assert "54321" not in facts.source_numbers


def test_finance_anchor_detects_direction_subject_and_unit_mismatches() -> None:
    material = {
        "summary": "指数回落，板块分化。",
        "indices": [["上证指数", "3882.41", "-1.85%"], ["沪深300", "4698.43", "+0.25%"]],
        "sector_rows": [["院线", "+6.42%"], ["半导体", "-7.87%"]],
        "risk_note": "面对市场波动，请结合风险承受能力审慎决策。",
        "disclaimer": "不构成投资建议。",
    }
    facts = build_structured_facts("finance", material)
    filler = "短期波动不能外推为长期趋势，后续还需观察成交、承接和持续性。" * 4
    wrong = (
        "指数方面，上证指数报4698.43点，上涨1.85%；沪深300报3882.41点，下跌0.25%。"
        "板块方面，院线下跌6.42个百分点，半导体上涨7.87%。"
        + filler
        + "面对市场波动，请结合自身风险承受能力审慎决策。以上内容不构成投资建议。"
    )
    report = validate_script(wrong, facts)
    codes = {issue.code for issue in report.issues}
    assert "finance_number_binding" in codes or "finance_direction_mismatch" in codes
    assert "finance_unit_mismatch" in codes

    valid = wrong.replace("上证指数报4698.43点，上涨1.85%", "上证指数报3882.41点，下跌1.85%")
    valid = valid.replace("沪深300报3882.41点，下跌0.25%", "沪深300报4698.43点，上涨0.25%")
    valid = valid.replace("院线下跌6.42个百分点", "院线上涨6.42%")
    valid = valid.replace("半导体上涨7.87%", "半导体下跌7.87%")
    valid_codes = {issue.code for issue in validate_script(valid, facts).issues}
    assert not {"finance_number_binding", "finance_direction_mismatch", "finance_unit_mismatch"} & valid_codes


def test_finance_anchor_accepts_spelled_out_separator_in_composite_subject() -> None:
    material = {
        "summary": "指数回落，防御走强。",
        "indices": [["上证指数", "3797.24", "-0.43%"]],
        "sector_rows": [["银行", "招商银行+3.24%"], ["光模块/通信", "中际旭创-6.41%"]],
        "risk_note": "面对市场波动，请结合风险承受能力审慎决策。",
        "disclaimer": "不构成投资建议。",
    }
    facts = build_structured_facts("finance", material)
    plan = ScriptPlan(
        mainline="指数回落，防御走强",
        main_fact_id="finance.index.1",
        opening="上证指数报3797.24点，下跌0.43%。",
        sections=["指数", "板块"],
        required_facts=["finance.index.1", "finance.sector.1", "finance.sector.2"],
    )
    filler = "短期波动不能外推为长期趋势，后续还需观察成交、承接和持续性。" * 8
    script = (
        "上证指数报3797.24点，下跌0.43%。"
        "板块方面，银行上涨，招商银行上涨3.24%；光模块和通信方向下跌，中际旭创下跌6.41%。"
        + filler
        + "面对市场波动，请结合风险承受能力审慎决策。以上内容不构成投资建议。"
    )
    codes = {issue.code for issue in validate_script(script, facts, plan=plan).errors}
    assert "required_fact_missing" not in codes

    one_sided = script.replace("光模块和通信方向下跌，中际旭创下跌6.41%", "光模块方向下跌，中际旭创下跌6.41%")
    assert "required_fact_missing" in {issue.code for issue in validate_script(one_sided, facts, plan=plan).errors}


def test_finance_opening_and_hype_accept_spelled_out_separator_in_composite_subject() -> None:
    material = {
        "indices": [["上证指数", "3797.24", "-0.43%"]],
        "sector_rows": [["光模块/通信", "中际旭创-6.41%", "156.2亿"]],
        "risk_note": "面对市场波动，请结合风险承受能力审慎决策。",
        "disclaimer": "不构成投资建议。",
    }
    facts = build_structured_facts("finance", material)
    opening = "光模块和通信跳水下跌6.41%，成交156.2亿。"
    plan = ScriptPlan(
        mainline="成长失血",
        main_fact_id="finance.sector.1",
        opening=opening,
        sections=["板块", "风险"],
        required_facts=["finance.sector.1"],
    )
    filler = "短期波动不能外推为长期趋势，后续还需观察成交、承接和持续性。" * 8
    script = (
        opening
        + "上证指数报3797.24点，下跌0.43%。"
        + filler
        + "面对市场波动，请结合风险承受能力审慎决策。以上内容不构成投资建议。"
    )
    codes = {issue.code for issue in validate_script(script, facts, plan=plan).errors}
    assert not {"required_fact_missing", "finance_opening_market_anchor_missing", "finance_weak_opening"} & codes
    assert "finance_hype_unbound" not in codes


def test_ai_status_upgrade_is_detected_per_fact() -> None:
    material = {
        **AI_MATERIAL,
        "focus_sentences": ["Qwen计划下月发布新模型。", "Kimi正在测试新的编码能力。"],
    }
    facts = build_structured_facts("ai_briefing", material)
    script = _ai_script().replace("Qwen发布新模型", "Qwen已发布新模型").replace("Kimi在", "Kimi已上线并在")
    report = validate_script(script, facts)
    assert any(issue.code == "ai_status_upgrade" for issue in report.issues)


def test_plan_parser_discards_model_wrapper_fields() -> None:
    plan = {
        "type": "json_object",
        "mainline": "国产模型竞争",
        "main_fact_id": "ai.summary",
        "opening": "直接开场",
        "sections": ["焦点", "影响"],
        "required_facts": ["ai.summary", "ai.focus.1", "ai.focus.2"],
        "risk_close": None,
    }
    client = FakeClient([json.dumps(plan, ensure_ascii=False), _ai_script()])
    result = ScriptGenerationService(Settings(_env_file=None), client=client).generate(
        content_type="ai_briefing",
        material=AI_MATERIAL,
        fallback_script=None,
        repair_attempts=0,
    )
    assert result.audit.generation_mode == "llm"


def test_plan_parser_truncates_verbose_risk_close() -> None:
    plan = {
        "mainline": "国产模型竞争",
        "main_fact_id": "ai.summary",
        "opening": "直接开场",
        "sections": ["焦点", "影响"],
        "required_facts": ["ai.summary", "ai.focus.1", "ai.focus.2"],
        "risk_close": "风" * 500,
    }
    client = FakeClient([json.dumps(plan, ensure_ascii=False), _ai_script()])
    result = ScriptGenerationService(Settings(_env_file=None), client=client).generate(
        content_type="ai_briefing",
        material=AI_MATERIAL,
        fallback_script=None,
        repair_attempts=0,
    )
    assert result.audit.plan and len(result.audit.plan.risk_close or "") == 400


def test_plan_parser_accepts_section_objects() -> None:
    plan = {
        "mainline": "国产模型竞争",
        "main_fact_id": "ai.summary",
        "opening": "直接开场",
        "sections": [
            {"key": "opening", "title": "开场", "fact_ids": ["ai.summary"]},
            {"key": "main", "title": "焦点", "fact_ids": ["ai.focus.1", "ai.focus.2"]},
        ],
        "required_facts": ["ai.summary", "ai.focus.1", "ai.focus.2"],
        "risk_close": None,
    }
    client = FakeClient([json.dumps(plan, ensure_ascii=False), _ai_script()])
    result = ScriptGenerationService(Settings(_env_file=None), client=client).generate(
        content_type="ai_briefing",
        material=AI_MATERIAL,
        fallback_script=None,
        repair_attempts=0,
    )
    assert result.audit.plan and result.audit.plan.section_fact_ids["main"] == ["ai.focus.1", "ai.focus.2"]


def test_plan_parser_accepts_json_wrapped_in_model_prose() -> None:
    plan = {
        "mainline": "国产模型竞争",
        "main_fact_id": "ai.summary",
        "opening": "直接开场",
        "sections": ["焦点", "影响"],
        "required_facts": ["ai.summary", "ai.focus.1", "ai.focus.2"],
        "risk_close": None,
    }
    client = FakeClient([f"计划如下：\n{json.dumps(plan, ensure_ascii=False)}\n请查收。", _ai_script()])
    result = ScriptGenerationService(Settings(_env_file=None), client=client).generate(
        content_type="ai_briefing",
        material=AI_MATERIAL,
        fallback_script=None,
        repair_attempts=0,
    )
    assert result.audit.generation_mode == "llm"


def test_failed_quality_does_not_append_duplicate_draft_error_attempt() -> None:
    plan = json.dumps(
        {
            "mainline": "国产模型竞争",
            "main_fact_id": "ai.summary",
            "opening": "日期和主线",
            "sections": ["焦点", "影响"],
            "required_facts": ["ai.summary", "ai.focus.1", "ai.focus.2"],
            "risk_close": None,
        },
        ensure_ascii=False,
    )
    attempts = []
    service = ScriptGenerationService(Settings(_env_file=None), client=FakeClient([plan, "短稿。"]))
    with pytest.raises(Exception, match="script generation failed"):
        service.generate(
            content_type="ai_briefing",
            material=AI_MATERIAL,
            fallback_script=None,
            repair_attempts=0,
            on_attempt=attempts.append,
        )
    assert [attempt.stage for attempt in attempts] == ["plan", "draft"]
    assert attempts[-1].error is None
    assert attempts[-1].quality_report and not attempts[-1].quality_report.passed


def test_service_soft_gate_accepts_a_draft_that_the_strict_gate_rejects() -> None:
    plan = json.dumps(
        {
            "mainline": "国产模型竞争",
            "main_fact_id": "ai.summary",
            "opening": "日期和主线",
            "sections": ["焦点", "影响"],
            "required_facts": ["ai.summary", "ai.focus.1", "ai.focus.2"],
            "risk_close": None,
        },
        ensure_ascii=False,
    )
    service = ScriptGenerationService(Settings(_env_file=None), client=FakeClient([plan, "短稿。"]))
    with pytest.raises(Exception, match="script generation failed"):
        service.generate(
            content_type="ai_briefing", material=AI_MATERIAL, fallback_script=None, repair_attempts=0, gate="strict"
        )

    result = ScriptGenerationService(Settings(_env_file=None), client=FakeClient([plan, "短稿。"])).generate(
        content_type="ai_briefing", material=AI_MATERIAL, fallback_script=None, repair_attempts=0, gate="soft"
    )
    report = result.audit.quality_report
    assert report.gate == "soft"
    assert report.passed
    assert report.errors == []
    assert {issue.code for issue in report.warnings} >= {"length", "required_fact_missing"}


def test_validate_rule_honours_the_quality_gate() -> None:
    service = ScriptGenerationService(Settings(_env_file=None), client=FakeClient([]))
    with pytest.raises(Exception, match="required_fact_missing"):
        service.validate_rule(content_type="ai_briefing", material=AI_MATERIAL, script="短稿。", gate="strict")

    result = service.validate_rule(content_type="ai_briefing", material=AI_MATERIAL, script="短稿。", gate="soft")
    assert result.script == "短稿。"
    assert result.audit.quality_report.gate == "soft"
    assert result.audit.quality_report.passed


def test_openai_client_uses_script_settings_and_router_fallback(monkeypatch) -> None:
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"choices":[{"message":{"content":"ok"}}]}'

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data)
        captured["auth"] = request.headers.get("Authorization")
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = OpenAICompatibleScriptClient(
        Settings(
            _env_file=None,
            script_llm_base_url="https://script.example/v1",
            script_llm_api_key="script-key",
            script_llm_model="script-model",
            router_llm_base_url="https://router.example/v1",
            router_llm_model="router-model",
        ),
        timeout=9,
    )
    assert client.complete(system_prompt="system", user_prompt="user") == "ok"
    assert captured["url"] == "https://script.example/v1/chat/completions"
    assert captured["body"]["model"] == "script-model"
    assert captured["auth"] == "Bearer script-key"
    assert captured["timeout"] == 9


def test_openai_client_uses_configured_timeout_by_default(monkeypatch) -> None:
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"choices":[{"message":{"content":"ok"}}]}'

    def fake_urlopen(_request, timeout):
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    settings = Settings(
        _env_file=None,
        script_llm_base_url="https://script.example/v1",
        script_llm_model="model",
        script_llm_timeout_seconds=37,
    )
    OpenAICompatibleScriptClient(settings).complete(system_prompt="system", user_prompt="user")
    assert captured["timeout"] == 37
    with pytest.raises(ValueError):
        Settings(_env_file=None, script_llm_timeout_seconds=4)
    with pytest.raises(ValueError):
        Settings(_env_file=None, script_llm_timeout_seconds=601)


def test_legacy_briefing_writer_exports_remain_compatible() -> None:
    assert briefing_script_writer.__all__ == [
        "FINANCE_PROMPT_VERSION",
        "PROMPT_VERSION",
        "rewrite_ai_briefing_script_with_llm",
        "rewrite_finance_script_with_llm",
    ]
    assert briefing_script_writer.PROMPT_VERSION
    assert briefing_script_writer.FINANCE_PROMPT_VERSION


def test_ai_attribution_accepts_synonymous_reporting_markers() -> None:
    material = {
        "summary": "亚马逊被曝洽谈以约 70 亿美元收购 Decart，DeepSeek 的融资规模被传至 120 亿美元。",
    }
    facts = build_structured_facts("ai_briefing", material)
    summary = next(fact for fact in facts.facts if fact.id == "ai.summary")
    assert {"被曝", "被传"} <= set(summary.attribution_markers)

    filler = "只有约五千人能接触实验室内部系统，认知漏斗的落差就摆在这里。"
    kept = "DeepSeek 的融资规模被传至 120 亿美元，亚马逊被曝洽谈收购 Decart。" + filler * 10
    kept_codes = {issue.code for issue in validate_script(kept, facts).issues}
    assert "ai_attribution_missing" not in kept_codes

    dropped = "DeepSeek 的融资规模达到 120 亿美元，亚马逊洽谈收购 Decart。" + filler * 10
    dropped_codes = {issue.code for issue in validate_script(dropped, facts).issues}
    assert "ai_attribution_missing" in dropped_codes
