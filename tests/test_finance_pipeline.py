from __future__ import annotations

import re
import subprocess
from datetime import date
from pathlib import Path

import pytest

import content_pipeline.finance_pipeline as finance_pipeline
from content_pipeline.errors import ConfigError
from content_pipeline.finance_pipeline import (
    build_90_second_script,
    build_finance_script_material,
    extract_response,
    select_daily_markdown,
    validate_finance_narration,
)
from content_pipeline.job_store import JobStore
from content_pipeline.models import JobStatus, TaskInput
from content_pipeline.orchestrator import Orchestrator
from content_pipeline.script_generation import build_structured_facts, validate_script
from content_pipeline.settings import Settings
from content_pipeline.tools.common import mpt_task_id
from content_pipeline.tools.finance_mpt_client import call_finance_mpt

SAMPLE_MD = """# Cron Job: 每日基金日报

## Prompt

运行 ttskill invoke，不要把这里读成旁白。

## Response

## 🎲 A股收盘简报 · 2026年7月16日

**一句话总结：** 四大指数全线下跌，半导体产业链承压，传媒影视逆势走强。

### 📊 四大指数收盘

| 指数 | 点位 | 涨跌幅 |
|:--|:--:|:--:|
| 上证指数 | 3,882.41 | -1.85% |
| 沪深300 | 4,698.43 | -1.84% |
| 创业板指 | 3,692.46 | -2.95% |
| 科创50 | 1,846.88 | -4.02% |

### 🔄 今日板块轮动

**领涨：**
| 板块 | 涨幅 |
|:--|:--:|
| 院线 | +6.42% |

**领跌：**
| 板块 | 跌幅 |
|:--|:--:|
| 集成电路封测 | -7.87% |

### 🎯 今日关键词

1. **传媒走强** — 暑期档预期升温。
2. **半导体回调** — 高位方向集中获利回吐。
3. **放量下跌** — 后续仍需观察承接力度。

### 🃏 我的解读

今天的下跌更像结构性调整。高估值仓位需要重新评估，不要追涨杀跌。市场波动加大时，应结合自己的风险承受能力控制仓位。我的建议：保持节奏，等待更清晰的确认信号。
"""


SUMMARY_TABLE_MD = """# Cron Job: 每日基金日报

## Response

📄 **2026-07-20 收盘简报已保存到本地**

**文件路径：** `~/hermes/sajin-daily-reports/2026-07-20-a-share-closing-brief.md`

| 模块 | 要点 |
|:----|:------|
| 盘面定性 | 低开高走全线收红，沪深300领涨+1.53%，风格切换明显 |
| 四指数 | 全部收涨：上证+0.85%、沪深300+1.53%、创业板+0.42%、科创50+0.19% |
| 板块轮动 | 资金从电子(-4.81%)抽身，涌入公用事业(+3.83%)、食品饮料(+3.82%)、非银金融(+2.82%)、银行(+2.32%) |
| 估值水位 | 上证和沪深300偏高、创业板中等偏高、科创50极度偏高(98.27%百分位) |
| 黄金债市 | Au99.99报873元/克+0.06%，央行持续增持；债市整体偏弱，信用债较强 |
| 三个信号 | 风格切换、权重归来 / 电子板块高位分化 / 高股息策略再起 |
| 策略建议 | 低位可试探沪深300和银行ETF；电子高位止盈；科创50谨慎持仓 |
"""


NEW_FORMAT_MD = """# Cron Job: 每日基金日报

## Prompt

运行 ttskill invoke，不要把这里读成旁白。

## Response

# 🎲 砂金 · A股收盘简报 · 2026-10-12（星期一）

**数据日：2026-10-12（周一收盘）· 交易日**

## 📌 顶部摘要

**周五那根深V留的后手，今天开盘就被掀了一半。**

## 📊 五指数表格（今日 2026-10-12 收盘）

| 指数 | 点位 | 涨跌幅 | 成交额 | 年内涨幅 |
|:-----|-----:|:-----:|-----:|:-----:|
| 上证指数 | **3,797.24** | 🟥 **-0.43%**（-16.55点） | 7,149.1亿 | ≈ -4.32% |
| 沪深300 | **4,287.98** | 🟥 **-0.68%**（-29.28点） | 4,055.7亿 | ≈ -7.39% |
| 创业板指 | **2,963.82** | 🟥 **-2.61%**（-79.51点） | 3,923.3亿 | ≈ -7.47% |
| 科创50 | **1,426.72** | 🟥 **-2.10%**（-30.54点） | 646.2亿 | ≈ +6.14% |
| 深证成指 | **12,384.87** | 🟥 **-2.03%**（-256.99点） | 8,229.1亿 | ≈ -8.42% |

## 🔄 今日板块轮动

**🟢 领涨——防御整桌坐回来**

- **银行（今日全场最强）**：**招商银行 +3.24%**（成交 43.6亿）｜**工商银行 +2.68%**（29.7亿）——**今天全数收回。**
- **石油石化 / 煤炭**：**中国石油 +2.59%**（16.8亿）｜**中国神华 +2.02%**（18.8亿）——**红利端同步接棒。**

**🟥 领跌——光模块整桌被端走**

- **光模块 / 通信（全场最惨）**：**中际旭创 -6.41%**（成交 156.2亿）｜**新易盛 -6.10%**——**放量下杀。**
- **半导体 / AI**：**海光信息 -2.77%**（82.9亿）｜**中芯国际 -1.60%**——**成长链整桌失血。**

## ⚖️ 估值水位（09-30 口径 · 未随今日行情更新）

| 指数 | PE-TTM | PE 近10年分位 | 判断 |
|:-----|------:|------:|:-----|
| 上证指数 | 15.53 | **76.28%** | 🟡 中等偏高 |
| 创业板指 | 35.89 | **47.44%** | 🟢 适中 |
| 科创50 | **128.85** | 71.74% | 🟡 中等偏高 |

## 💛 黄金 & 债券

- **内盘金**：EM Au99.99 = 902.9 元/克，-0.17%；Sina Au(T+D) = 902.56，-0.18%。
- **债市**：中债 10Y 收益率 1.669%，较上周五下行约 -4.5BP。

## 🔑 今日关键词

1. **高低切换** — 不是轮动，是搬家。
2. **缩量阴跌** — 没人卖，也没人买。

## ♠️ 我的解读

防御反攻、成长失血。面对市场波动，请结合自身投资经验审慎决策。
"""


def test_selects_today_then_yesterday_and_latest(tmp_path: Path) -> None:
    older = tmp_path / "2026-07-17_10-00-00.md"
    latest = tmp_path / "2026-07-17_18-00-00.md"
    yesterday = tmp_path / "2026-07-16_18-00-00.md"
    stale = tmp_path / "2026-07-10_18-00-00.md"
    for path in (older, latest, yesterday, stale):
        path.write_text(SAMPLE_MD, encoding="utf-8")
    older.touch()
    latest.touch()
    assert select_daily_markdown(tmp_path, today=date(2026, 7, 17)) == latest
    older.unlink()
    latest.unlink()
    assert select_daily_markdown(tmp_path, today=date(2026, 7, 17)) == yesterday
    yesterday.unlink()
    assert select_daily_markdown(tmp_path, today=date(2026, 7, 17)) == stale


def test_auto_selection_skips_markdown_without_response_section(tmp_path: Path) -> None:
    failed = tmp_path / "2026-07-17_19-00-00.md"
    failed.write_text("# Cron Job: 每日基金日报 (FAILED)\n\n## Error\n\n401 auth failed\n", encoding="utf-8")
    yesterday = tmp_path / "2026-07-16_18-00-00.md"
    yesterday.write_text(SAMPLE_MD, encoding="utf-8")
    assert select_daily_markdown(tmp_path, today=date(2026, 7, 17)) == yesterday

    yesterday.write_text("# broken\n", encoding="utf-8")
    stale = tmp_path / "2026-07-10_18-00-00.md"
    stale.write_text(SAMPLE_MD, encoding="utf-8")
    assert select_daily_markdown(tmp_path, today=date(2026, 7, 17)) == stale


def test_extracts_response_and_builds_short_script() -> None:
    response = extract_response(SAMPLE_MD)
    assert "ttskill invoke" not in response
    script = build_90_second_script(response, "2026-07-16")
    assert 300 <= len(script) <= 600
    assert "上证指数" in script
    assert "院线" in script
    assert "集成电路封测" in script
    assert "风险承受能力" in script
    assert script.startswith("-1.85%，上证指数报3,882.41点，直接跳水下跌1.85%。")
    assert script.endswith("不构成任何投资建议。")


@pytest.mark.parametrize(
    ("change", "action", "spoken"),
    [
        ("+1.85%", "强势拉升", "上涨1.85%"),
        ("-1.85%", "直接跳水", "下跌1.85%"),
        ("上涨1.85%", "强势拉升", "上涨1.85%"),
        ("下跌1.85%", "直接跳水", "下跌1.85%"),
        ("跌1.85%", "直接跳水", "下跌1.85%"),
        ("持平", "平收横住", "持平"),
        ("平收", "平收横住", "平收"),
    ],
)
def test_daily_markdown_script_matches_opening_action_to_change_direction(
    change: str, action: str, spoken: str
) -> None:
    response = SAMPLE_MD.replace("-1.85%", change, 1)
    script = build_90_second_script(response, "2026-07-16")

    assert script.startswith(f"{change}，上证指数报3,882.41点，{action}{spoken}。")
    assert "平收横住下跌" not in script


def test_parses_emoji_five_index_report_with_prose_sector_bullets() -> None:
    response = extract_response(NEW_FORMAT_MD)
    material = build_finance_script_material(response, "2026-10-12")

    assert [row[0] for row in material["indices"]] == [
        "上证指数",
        "沪深300",
        "创业板指",
        "科创50",
        "深证成指",
    ]
    assert material["indices"][0][2] == "-0.43%（-16.55点）"
    assert [row[0] for row in material["sector_rows"][:2]] == ["银行", "光模块/通信"]
    assert material["sector_rows"][0][1:] == ["招商银行+3.24%", "工商银行+2.68%"]
    assert [
        fact.subject
        for fact in build_structured_facts("finance", material).facts
        if fact.required and fact.kind == "sector"
    ] == [
        "银行",
        "光模块/通信",
    ]
    assert "47.44%" in material["valuation"]
    assert "35.8947" not in material["valuation"]


def test_new_format_rule_script_binds_every_index_and_passes_quality_gate() -> None:
    response = extract_response(NEW_FORMAT_MD)
    material = build_finance_script_material(response, "2026-10-12")
    script = build_90_second_script(response, "2026-10-12")

    validate_finance_narration(script, material)
    report = validate_script(script, build_structured_facts("finance", material), plan=None)

    assert report.passed, [(issue.code, issue.evidence) for issue in report.errors]
    for subject in ("上证指数", "沪深300", "创业板指", "科创50", "深证成指"):
        assert subject in script


def test_finance_llm_narration_validation_rejects_non_compliant_script() -> None:
    response = extract_response(SAMPLE_MD)
    material = build_finance_script_material(response, "2026-07-16")
    valid = (
        "2026年7月16日A股收盘观察。今天四大指数全线回落，上证指数报3882.41点，跌1.85%，"
        "沪深300报4698.43点，跌1.84%，创业板指和科创50跌幅更大，说明高弹性方向承压明显。"
        "板块方面，院线逆势上涨6.42%，传媒影视受暑期档预期带动；集成电路封测下跌7.87%，"
        "半导体高位方向出现获利回吐。今天的关键词是传媒走强、半导体回调和放量下跌。策略上，"
        "面对结构性调整，不要追涨杀跌，高估值仓位需要重新评估。面对市场波动，请结合自身投资期限、"
        "仓位水平和风险承受能力审慎决策。短期盘面只代表当天资金偏好，不能直接外推为中长期趋势，仍需观察量能变化。以上内容仅为市场信息整理和个人观点，不构成任何投资建议。"
    )
    validate_finance_narration(valid, material)
    with pytest.raises(ConfigError, match="investment disclaimer"):
        validate_finance_narration(valid.replace("不构成任何投资建议", "仅供参考"), material)
    with pytest.raises(ConfigError, match="prohibited"):
        validate_finance_narration(valid.replace("不要追涨杀跌", "满仓梭哈必涨"), material)


def test_finance_llm_narration_rejects_historical_trade_advice_despite_material_numbers() -> None:
    material = {
        "publication_date": "2026-07-21",
        "summary": "科技主线活跃，市场风险偏好回升。",
        "indices": [["上证指数", "3882.41", "-1.85%"]],
        "index_summary": "沪深300上涨1.53%，创业板上涨0.42%。",
        "sector_rows": [["公用事业", "+3.83%"], ["电子", "-4.81%"]],
        "sector_summary": "食品饮料上涨3.82%，非银金融上涨2.82%。",
        "keywords": ["科创50谨慎持仓，98.27%百分位需要降温观察。"],
        "strategy": ["低位可试探沪深300和银行ETF，电子高位止盈。"],
        "fallback_script": (
            "A股收盘观察。四大指数方面，上证指数报3882.41点，跌1.85%。"
            "板块方面，公用事业上涨3.83%。"
            "面对市场波动，请结合自身投资期限、仓位水平和风险承受能力审慎决策。"
            "以上内容仅为市场信息整理和个人观点，不构成任何投资建议。"
        ),
    }
    script = (
        "2026年7月21日A股收盘观察。今天指数表现分化，上证指数报3882.41点，跌1.85%，"
        "沪深300上涨1.53%，创业板上涨0.42%，说明权重与成长方向节奏并不完全一致。"
        "板块方面，公用事业上涨3.83%，食品饮料上涨3.82%，非银金融上涨2.82%，电子下跌4.81%，"
        "资金在防御和高位科技之间切换。估值线索上，科创50处在98.27%百分位，谨慎持仓比追高更重要。"
        "策略上，低位可试探沪深300和银行ETF，电子高位止盈，但具体操作仍要看个人仓位。"
        "如果后续成交没有继续配合，短期反弹仍可能出现反复，不能把单日表现直接外推为趋势反转。"
        "面对市场波动，请结合自身投资期限、仓位水平和风险承受能力审慎决策。"
        "以上内容仅为市场信息整理和个人观点，不构成任何投资建议。"
    )

    with pytest.raises(ConfigError, match="prohibited investment language"):
        validate_finance_narration(script, material)


def test_finance_llm_narration_rejects_numbers_outside_material() -> None:
    response = extract_response(SAMPLE_MD)
    material = build_finance_script_material(response, "2026-07-16")
    script = (
        "2026年7月16日A股收盘观察。今天四大指数全线回落，上证指数报3882.41点，跌1.85%，"
        "沪深300报4698.43点，跌1.84%，创业板指和科创50跌幅更大，说明高弹性方向承压明显。"
        "板块方面，院线逆势上涨6.42%，传媒影视受暑期档预期带动；集成电路封测下跌7.87%，"
        "另有不存在于素材的99.99%作为测试数字。今天的关键词是传媒走强、半导体回调和放量下跌。"
        "策略上，面对结构性调整，不要追涨杀跌，高估值仓位需要重新评估，短线信号不能直接外推为长期趋势。"
        "后续仍需观察量能变化和承接力度。"
        "面对市场波动，请结合自身投资期限、仓位水平和风险承受能力审慎决策。"
        "以上内容仅为市场信息整理和个人观点，不构成任何投资建议。"
    )

    with pytest.raises(ConfigError, match="not present in the source material"):
        validate_finance_narration(script, material)


def test_builds_script_from_level_two_headings() -> None:
    response = extract_response(SAMPLE_MD.replace("### ", "## "))
    script = build_90_second_script(response, "2026-07-16")
    assert 300 <= len(script) <= 600
    assert "上证指数" in script
    assert "院线" in script
    assert "风险承受能力" in script


def test_builds_script_from_bold_pseudo_headings() -> None:
    markdown = re.sub(r"(?m)^###\s+(.+)$", r"**\1**", SAMPLE_MD)
    script = build_90_second_script(extract_response(markdown), "2026-07-16")
    assert 300 <= len(script) <= 600
    assert "上证指数" in script
    assert "集成电路封测" in script
    assert "风险承受能力" in script


def test_builds_script_from_summary_table_without_path_leakage() -> None:
    response = extract_response(SUMMARY_TABLE_MD)
    script = build_90_second_script(response, "2026-07-20")
    assert 300 <= len(script) <= 600
    assert "沪深300领涨" in script
    assert "公用事业" in script
    assert "高股息策略" in script
    assert "风险承受能力" in script
    assert "文件路径" not in script
    assert "sajin-daily-reports" not in script


def test_build_finance_script_material_keeps_fields_when_fallback_script_fails(monkeypatch) -> None:
    response = extract_response(
        """# Cron Job: 每日基金日报

## Response

## A股收盘简报

一句话总结：大盘急跌，资金切向防御。

## 四指数行情
| 指数 | 点位 | 涨跌幅 |
| --- | --- | --- |
| 上证指数 | 3894.42 | -2.40% |
| 沪深300 | 4588.70 | -2.90% |
| 创业板指 | 3473.49 | -6.26% |
| 科创50 | 1667.52 | -6.89% |

## 今日板块轮动
| 板块（推断） | 代表个股 | 涨幅 |
| --- | --- | --- |
| 石油石化 | 中国石油 | +2.66% |
| 银行 | 招商银行 | +2.25% |
| 半导体 | 中芯国际 | -5.16% |

## 今日关键词
1. 放量杀跌。

## 我的解读
控制仓位，等待承接。
"""
    )
    monkeypatch.setattr(
        finance_pipeline,
        "build_90_second_script",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ConfigError("fallback failed")),
    )

    material = build_finance_script_material(response, "2026-08-19")

    assert material["fallback_script"] == ""
    assert "fallback_error" in material
    assert material["indices"]
    assert material["sector_rows"]


def test_rejects_path_only_or_insufficient_response() -> None:
    with pytest.raises(ConfigError, match="file reference"):
        extract_response(
            "# Cron Job: 每日基金日报\n\n## Response\n\n报告已保存。文件路径：`~/hermes/sajin-daily-reports/report.md`"
        )


def test_finance_orchestrator_skips_gemini_and_generates_dry_run(tmp_path: Path, monkeypatch) -> None:
    source_dir = tmp_path / "fund-daily"
    source_dir.mkdir()
    (source_dir / "2026-07-16_18-00-00.md").write_text(SAMPLE_MD, encoding="utf-8")
    settings = Settings(
        data_dir=tmp_path / "output",
        profiles_dir=Path("profiles"),
        finance_md_dir=source_dir,
        mpt_dir=tmp_path / "mpt",
        pipeline_defaults_file=tmp_path / "missing-defaults.yaml",
    )
    monkeypatch.setattr(
        "content_pipeline.pipelines.anime.call_gemini_skill",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("Gemini must not be called for finance")),
    )
    orchestrator = Orchestrator(settings=settings, store=JobStore(settings.data_dir))
    task_id = orchestrator.submit(
        TaskInput(
            description="生成每日金融视频",
            content_type="finance",
            params={"date": "2026-07-16", "dry_run": True},
        )
    )
    orchestrator.run(task_id)
    snapshot = orchestrator.store.get(task_id)
    assert snapshot.status == JobStatus.SUCCEEDED
    assert snapshot.artifacts.images == []
    assert snapshot.artifacts.source_document is not None
    assert snapshot.artifacts.video is not None
    assert snapshot.artifacts.subtitle is not None


def test_finance_orchestrator_uses_valid_llm_script(tmp_path: Path, monkeypatch) -> None:
    source_dir = tmp_path / "fund-daily"
    source_dir.mkdir()
    (source_dir / "2026-07-16_18-00-00.md").write_text(SAMPLE_MD, encoding="utf-8")
    llm_script = (
        "-1.85%，上证指数报3882.41点，直接跳水下跌。2026年7月16日A股收盘观察。今天四大指数全线回落，"
        "沪深300报4698.43点，跌1.84%，创业板指报3692.46点，跌2.95%，"
        "科创50报1846.88点，跌4.02%，说明高弹性方向承压明显。"
        "板块方面，院线逆势上涨6.42%，传媒影视受暑期档预期带动；集成电路封测下跌7.87%，"
        "半导体高位方向出现获利回吐。今天的关键词是传媒走强、半导体回调和放量下跌。策略上，"
        "面对结构性调整，不要追涨杀跌，高估值仓位需要重新评估。面对市场波动，请结合自身投资期限、"
        "仓位水平和风险承受能力审慎决策。短期盘面只代表当天资金偏好，不能直接外推为中长期趋势，仍需观察量能变化。以上内容仅为市场信息整理和个人观点，不构成任何投资建议。"
    )
    monkeypatch.setattr(
        "content_pipeline.script_generation.client.OpenAICompatibleScriptClient.complete",
        lambda _self, *, label, **_kwargs: (
            '{"mainline":"指数回落与板块分化","main_fact_id":"finance.index.1","opening":"日期和盘面","sections":["指数","板块","风险"],"required_facts":["finance.index.1","finance.index.2","finance.index.3","finance.index.4","finance.sector.1","finance.sector.2","finance.risk_note","finance.disclaimer"],"risk_close":"不构成投资建议"}'
            if label.endswith("plan")
            else '{"text":"上证指数回落观察","fact_ids":["finance.index.1"]}'
            if label.endswith("metadata title")
            else '{"text":"上证指数下跌1.85%，四大指数全线回落，院线逆势走强，半导体方向承压。'
            '后续仍需观察市场承接情况，注意市场波动风险，审慎决策，不构成投资建议。",'
            '"fact_ids":["finance.index.1","finance.sector.1","finance.sector.2"]}'
            if label.endswith("metadata description")
            else llm_script
        ),
    )
    settings = Settings(
        data_dir=tmp_path / "output",
        finance_md_dir=source_dir,
        mpt_dir=tmp_path / "mpt",
        pipeline_defaults_file=tmp_path / "missing-defaults.yaml",
    )
    orchestrator = Orchestrator(settings=settings, store=JobStore(settings.data_dir))
    task_id = orchestrator.submit(
        TaskInput(
            description="生成每日金融视频",
            content_type="finance",
            params={"date": "2026-07-16", "dry_run": True, "script_writer": "llm"},
        )
    )
    orchestrator.run(task_id)
    snapshot = orchestrator.store.get(task_id)
    assert snapshot.status == JobStatus.SUCCEEDED
    assert snapshot.artifacts.narration_script == llm_script
    assert snapshot.artifacts.upload_result["script_writer"] == "llm"
    assert snapshot.artifacts.script_plan_path and snapshot.artifacts.script_plan_path.is_file()
    assert snapshot.artifacts.script_attempts_path and snapshot.artifacts.script_attempts_path.is_file()
    assert snapshot.artifacts.script_quality_report_path and snapshot.artifacts.script_quality_report_path.is_file()
    event_names = {event["event"] for event in orchestrator.store.read_events(task_id)}
    assert {"script_plan", "script_draft", "script_quality_checked", "script_plan_generated"} <= event_names
    quality_events = [
        event for event in orchestrator.store.read_events(task_id) if event["event"] == "script_quality_checked"
    ]
    assert quality_events and {event["payload"]["gate"] for event in quality_events} == {"soft"}
    assert snapshot.artifacts.script_generation_audit["quality_report"]["gate"] == "soft"
    assert snapshot.artifacts.script_generation_audit["quality_report"]["passed"] is True


def test_finance_orchestrator_fails_on_invalid_llm_script(tmp_path: Path, monkeypatch) -> None:
    source_dir = tmp_path / "fund-daily"
    source_dir.mkdir()
    (source_dir / "2026-07-16_18-00-00.md").write_text(SAMPLE_MD, encoding="utf-8")
    monkeypatch.setattr(
        "content_pipeline.script_generation.client.OpenAICompatibleScriptClient.complete",
        lambda _self, *, label, **_kwargs: (
            '{"mainline":"指数回落与板块分化","main_fact_id":"finance.index.1","opening":"日期和盘面","sections":["指数","板块"],"required_facts":["finance.index.1","finance.index.2","finance.index.3","finance.index.4","finance.sector.1","finance.sector.2","finance.risk_note","finance.disclaimer"],"risk_close":"不构成投资建议"}'
            if label.endswith("plan")
            else "保证收益，满仓梭哈。"
        ),
    )
    settings = Settings(
        data_dir=tmp_path / "output",
        finance_md_dir=source_dir,
        mpt_dir=tmp_path / "mpt",
        pipeline_defaults_file=tmp_path / "missing-defaults.yaml",
    )
    orchestrator = Orchestrator(settings=settings, store=JobStore(settings.data_dir))
    task_id = orchestrator.submit(
        TaskInput(
            description="生成每日金融视频",
            content_type="finance",
            params={
                "date": "2026-07-16",
                "dry_run": True,
                "script_writer": "llm",
                "quality_gate": "strict",
            },
        )
    )
    orchestrator.run(task_id)
    snapshot = orchestrator.store.get(task_id)
    assert snapshot.status == JobStatus.FAILED
    assert snapshot.artifacts.narration_script is None
    assert snapshot.artifacts.video is None
    assert snapshot.artifacts.upload_result["script_writer"] == "llm_failed"
    assert snapshot.artifacts.upload_result["script_writer_error"]
    event_names = [event["event"] for event in orchestrator.store.read_events(task_id)]
    assert "script_generation_failed" in event_names
    assert "script_quality_checked" in event_names


def test_finance_orchestrator_rejects_bad_response_before_mpt_or_upload(tmp_path: Path, monkeypatch) -> None:
    source_dir = tmp_path / "fund-daily"
    source_dir.mkdir()
    (source_dir / "2026-07-20_18-00-00.md").write_text(
        "# Cron Job: 每日基金日报\n\n## Response\n\n报告已保存。文件路径：`~/hermes/sajin-daily-reports/report.md`",
        encoding="utf-8",
    )
    settings = Settings(
        data_dir=tmp_path / "output",
        finance_md_dir=source_dir,
        mpt_dir=tmp_path / "mpt",
        pipeline_defaults_file=tmp_path / "missing-defaults.yaml",
    )
    monkeypatch.setattr(
        "content_pipeline.finance_pipeline.call_finance_mpt",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("MPT must not be called")),
    )
    monkeypatch.setattr(
        "content_pipeline.publishing.call_sau_target",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("upload must not be called")),
    )
    orchestrator = Orchestrator(settings=settings, store=JobStore(settings.data_dir))
    task_id = orchestrator.submit(
        TaskInput(
            description="生成并发布每日金融视频",
            content_type="finance",
            publish=True,
            params={"date": "2026-07-20", "dry_run": True},
        )
    )
    orchestrator.run(task_id)
    snapshot = orchestrator.store.get(task_id)
    assert snapshot.status == JobStatus.FAILED
    assert snapshot.current_step is not None
    assert snapshot.current_step.value == "source_scan"
    assert snapshot.artifacts.video is None
    assert snapshot.artifacts.publish_results == {}


def test_finance_publish_targets_are_independent(tmp_path: Path, monkeypatch) -> None:
    source_dir = tmp_path / "fund-daily"
    source_dir.mkdir()
    (source_dir / "2026-07-16_18-00-00.md").write_text(SAMPLE_MD, encoding="utf-8")
    settings = Settings(
        data_dir=tmp_path / "output",
        finance_md_dir=source_dir,
        mpt_dir=tmp_path / "mpt",
        pipeline_defaults_file=tmp_path / "missing-defaults.yaml",
    )

    def fake_upload(*, target, **kwargs):
        if target.platform == "douyin":
            return {"success": True, "visibility": "private"}
        raise RuntimeError("kuaishou failed")

    monkeypatch.setattr("content_pipeline.publishing.call_sau_target", fake_upload)
    orchestrator = Orchestrator(settings=settings, store=JobStore(settings.data_dir))
    task_id = orchestrator.submit(
        TaskInput(
            description="生成并发布每日金融视频",
            content_type="finance",
            publish=True,
            params={"date": "2026-07-16", "dry_run": True},
        )
    )
    orchestrator.run(task_id)
    snapshot = orchestrator.store.get(task_id)
    assert snapshot.status == JobStatus.PARTIAL
    assert snapshot.artifacts.publish_results["douyin"]["success"] is True
    assert snapshot.artifacts.publish_results["kuaishou"]["success"] is False
    assert "tencent" not in snapshot.artifacts.publish_results


def _run_finance_publish(tmp_path: Path, monkeypatch, publish_targets: list[dict[str, object]]):
    source_dir = tmp_path / "fund-daily"
    source_dir.mkdir()
    (source_dir / "2026-07-16_18-00-00.md").write_text(SAMPLE_MD, encoding="utf-8")
    settings = Settings(
        data_dir=tmp_path / "output",
        finance_md_dir=source_dir,
        mpt_dir=tmp_path / "mpt",
        pipeline_defaults_file=tmp_path / "missing-defaults.yaml",
    )

    def fake_upload(*, target, **kwargs):
        return {"success": True, "visibility": target.visibility}

    monkeypatch.setattr("content_pipeline.publishing.call_sau_target", fake_upload)
    orchestrator = Orchestrator(settings=settings, store=JobStore(settings.data_dir))
    task_id = orchestrator.submit(
        TaskInput(
            description="生成并发布每日金融视频",
            content_type="finance",
            publish=True,
            publish_targets=publish_targets,
            params={"date": "2026-07-16", "dry_run": True},
        )
    )
    orchestrator.run(task_id)
    return orchestrator.store.get(task_id)


def test_finance_publish_aggregates_public_visibility(tmp_path: Path, monkeypatch) -> None:
    snapshot = _run_finance_publish(
        tmp_path,
        monkeypatch,
        [
            {"platform": "douyin", "account": "金融破壁人", "visibility": "public"},
            {"platform": "kuaishou", "account": "破壁人", "visibility": "public"},
        ],
    )

    assert snapshot.status == JobStatus.SUCCEEDED
    assert snapshot.artifacts.publish_results["douyin"]["visibility"] == "public"
    assert snapshot.artifacts.upload_result["visibility"] == "public"


def test_finance_publish_aggregates_mixed_visibility(tmp_path: Path, monkeypatch) -> None:
    snapshot = _run_finance_publish(
        tmp_path,
        monkeypatch,
        [
            {"platform": "douyin", "account": "金融破壁人", "visibility": "public"},
            {"platform": "kuaishou", "account": "破壁人"},
        ],
    )

    assert snapshot.status == JobStatus.SUCCEEDED
    assert snapshot.artifacts.upload_result["visibility"] == "mixed"


def test_finance_mpt_uses_pexels_vertical_voice_and_subtitles(tmp_path: Path, monkeypatch) -> None:
    mpt_dir = tmp_path / "mpt"
    task_dir = mpt_dir / "storage" / "tasks" / mpt_task_id("finance-20260716")
    task_dir.mkdir(parents=True)
    captured = []

    def fake_run_command(command, **kwargs):
        captured.extend(command)
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "final-1.mp4").write_bytes(b"video")
        (task_dir / "subtitle.srt").write_text(
            "1\n00:00:00,000 --> 00:00:05,000\n这是一段有效的每日金融视频旁白字幕内容。\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("content_pipeline.tools.narrated_mpt_client.run_command", fake_run_command)
    result = call_finance_mpt(
        publication_date="2026-07-16",
        title="每日基金日报",
        script="这是一段有效的金融市场旁白，不是文件路径。",
        output_dir=tmp_path / "output",
        settings=Settings(mpt_dir=mpt_dir, mpt_python=Path("python")),
    )
    assert result.video.is_file()
    assert captured[captured.index("--video-source") + 1] == "pexels"
    assert captured[captured.index("--video-aspect") + 1] == "9:16"
    assert captured[captured.index("--voice-name") + 1] == "zh-CN-YunxiNeural"
    assert captured[captured.index("--voice-rate") + 1] == "1.0"
    assert "--subtitle-enabled" in captured
