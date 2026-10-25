import json
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from content_pipeline.api.app import create_app
from content_pipeline.content_studio.models import (
    ContentDraft,
    CreateDraftInput,
    GenerateVideoInput,
    ResearchSource,
    UpdateDraftInput,
)
from content_pipeline.content_studio.service import ContentStudioService
from content_pipeline.content_studio.store import ContentDraftStore, DraftConflictError, DraftCorruptError
from content_pipeline.content_studio.ttskill_client import TTSkillClient, TTSkillError
from content_pipeline.finance_pipeline import build_finance_script_material
from content_pipeline.script_generation import build_structured_facts, validate_script
from content_pipeline.settings import Settings

FINANCE_LLM_SCRIPT = (
    "2026年7月16日A股收盘观察。今天四大指数全线回落，上证指数报3882.41点，跌1.85%，"
    "沪深300报4698.43点，跌1.84%，创业板指和科创50跌幅更大，说明高弹性方向承压明显。"
    "板块方面，院线逆势上涨6.42%，传媒影视受暑期档预期带动；集成电路封测下跌7.87%，"
    "半导体高位方向出现获利回吐。今天的关键词是传媒走强、半导体回调和放量下跌。策略上，"
    "面对结构性调整，不要追涨杀跌，高估值仓位需要重新评估。面对市场波动，请结合自身投资期限、"
    "仓位水平和风险承受能力审慎决策。短期盘面只代表当天资金偏好，不能直接外推为中长期趋势，"
    "仍需观察量能变化。以上内容仅为市场信息整理和个人观点，不构成任何投资建议。"
)
FINANCE_DAILY_LLM_SCRIPT = (
    "3832.26点，上证指数上涨0.72%，直接硬拉。A股收盘观察，今天市场风险偏好明显修复。"
    "四大指数同步上涨，上证指数报3832.26点，涨0.72%，"
    "沪深300报4588.20点，涨0.85%，创业板指报3343.96点，涨3.06%，科创50报1635.96点，涨2.99%，"
    "成长方向弹性更强。板块方面，传媒上涨6.00%，计算机上涨5.56%，通信上涨4.26%，AI算力链继续活跃；"
    "银行下跌1.38%，食品饮料下跌0.55%，红利防御方向阶段性回落。今天的关键词是高低切换、科技接力和量能放大。"
    "策略上，已持有科技方向的仓位可以关注节奏，但不宜情绪化追高；空仓观望的资金更适合等待回踩和缩量企稳。"
    "面对市场波动，请结合自身投资期限、仓位水平和风险承受能力审慎决策。以上内容仅为市场信息整理和个人观点，不构成任何投资建议。"
)
AI_LLM_SCRIPT = (
    "2026年8月5日每日AI简报，今天的主线是国产模型集中亮相。Qwen发布新模型并开放权重，"
    "编码和工具调用能力继续提升，开发者生态有望进一步扩大。Kimi也在多个前端编码任务中取得领先，"
    "说明模型竞争正在从参数规模走向真实生产力。海外方面，Nvidia继续建设新一代AI工厂，CPU和GPU部署"
    "会支撑科研与企业应用。端侧小模型同步提速，多家厂商开始把AI能力推向消费级设备。监管层面，"
    "欧盟要求大型平台开放关键接口，智能体身份安全也被企业重点关注。整体看，AI竞争已经不只是单点模型"
    "能力，而是基础设施、应用生态和监管规则的综合较量。接下来，谁能把能力稳定落到产品里，谁就更可能占据主动。"
    "对观众来说，今天最值得关注的不是单个发布会的热度，而是模型能力、算力供给、端侧落地和开放规则正在同时推进。"
)


def stub_content_studio_llm(
    monkeypatch,
    *,
    finance_script: str = FINANCE_LLM_SCRIPT,
    ai_script: str = AI_LLM_SCRIPT,
) -> None:
    def complete(_self, *, label: str, user_prompt: str, **_kwargs):
        if label.endswith("plan"):
            payload = json.loads(user_prompt.split("事实包：\n", 1)[1])
            required = [fact["id"] for fact in payload["facts"] if fact["required"]]
            main = next(
                (fact["id"] for fact in payload["facts"] if fact["kind"] == "index"),
                required[0] if required else payload["facts"][0]["id"],
            )
            if main not in required:
                required.insert(0, main)
            risk_facts = [fact for fact in payload["facts"] if fact["kind"] == "risk" and fact["required"]]
            close_ids = [fact["id"] for fact in risk_facts]
            script = ai_script if label.startswith("ai_briefing") else finance_script
            return json.dumps(
                {
                    "mainline": "围绕核心事实展开",
                    "main_fact_id": main,
                    "opening": script.split("。", 1)[0] + "。",
                    "sections": ["开场行情", "核心事实", "风险收尾"],
                    "required_facts": required,
                    "risk_close": "".join(fact["text"] for fact in risk_facts) or None,
                    "section_fact_ids": {
                        "opening": [main],
                        "main": [fact_id for fact_id in required if fact_id != main and fact_id not in close_ids],
                        "close": close_ids,
                    },
                },
                ensure_ascii=False,
            )
        if not label.endswith("repair"):
            return ai_script if label.startswith("ai_briefing") else finance_script
        raise RuntimeError("test repair unavailable")

    monkeypatch.setattr("content_pipeline.script_generation.client.OpenAICompatibleScriptClient.complete", complete)


def test_create_draft_input_normalizes_focus_assets_and_rejects_extras() -> None:
    request = CreateDraftInput(
        title="  今日金融资讯  ",
        focus_assets=[" 东方财富 ", "", "东方财富", " 贵州茅台 "],
        enabled_source_ids=[" finance-daily ", "gold", "gold", ""],
    )

    assert request.title == "今日金融资讯"
    assert request.focus_assets == ["东方财富", "贵州茅台"]
    assert request.enabled_source_ids == ["finance-daily", "gold"]

    with pytest.raises(ValueError):
        CreateDraftInput(title="今日金融资讯", focus_assets=["x" * 33])
    with pytest.raises(ValueError):
        CreateDraftInput(title="今日金融资讯", unexpected=True)


def test_content_draft_normalizes_text_fields_and_video_task_ids() -> None:
    draft = ContentDraft(
        draft_id="a" * 32,
        template_id="finance_90s",
        title="  标题  ",
        script="  已编辑  ",
        focus_assets=[" 东方财富 ", "东方财富", ""],
        enabled_source_ids=[" finance-daily ", "gold", "gold"],
        research_error="   ",
        video_task_ids=[" " + "b" * 32 + " ", "b" * 32],
    )

    assert draft.title == "标题"
    assert draft.script == "已编辑"
    assert draft.focus_assets == ["东方财富"]
    assert draft.enabled_source_ids == ["finance-daily", "gold"]
    assert draft.research_error is None
    assert draft.video_task_ids == ["b" * 32]


def test_research_source_normalizes_text_fields() -> None:
    source = ResearchSource(
        source_id=" gold ",
        label=" 黄金行情 ",
        skill_id=" TTFUND_GOLD_INFO ",
        fetched_at=" 2026-08-05T00:00:00Z ",
        data_as_of="   ",
        summary="  黄金上涨  ",
        error="   ",
    )

    assert source.source_id == "gold"
    assert source.label == "黄金行情"
    assert source.skill_id == "TTFUND_GOLD_INFO"
    assert source.fetched_at == "2026-08-05T00:00:00Z"
    assert source.data_as_of is None
    assert source.summary == "黄金上涨"
    assert source.error is None


def test_update_draft_input_rejects_extra_fields() -> None:
    request = UpdateDraftInput(revision=1, title="  新标题  ", script="  已编辑  ")

    assert request.title == "新标题"
    assert request.script == "  已编辑  "
    with pytest.raises(ValueError):
        UpdateDraftInput(revision=1, title="新标题", script="已编辑", status="ready")
    with pytest.raises(ValueError):
        UpdateDraftInput(revision=1, title="新标题", script="   ")


def test_generate_video_input_rejects_extra_fields() -> None:
    request = GenerateVideoInput(revision=1, dry_run=True, voice_rate=0.9)

    assert request.dry_run is True
    assert request.voice_rate == 0.9
    with pytest.raises(ValueError):
        GenerateVideoInput(revision=1, dry_run=True, unexpected=True)


@pytest.mark.parametrize(
    "payload",
    [
        {
            "draft_id": "a" * 32,
            "template_id": "finance_90s",
            "title": "标题",
            "unexpected": True,
        },
        {
            "draft_id": "a" * 32,
            "template_id": "unknown",
            "title": "标题",
        },
        {
            "draft_id": "a" * 32,
            "template_id": "finance_90s",
            "title": "   ",
        },
        {
            "draft_id": "a" * 32,
            "template_id": "finance_90s",
            "title": "标题",
            "research": [{"source_id": "gold", "label": "黄金", "skill_id": "TTFUND_GOLD_INFO", "unexpected": True}],
        },
        {
            "draft_id": "a" * 32,
            "template_id": "finance_90s",
            "title": "标题",
            "research": [{"source_id": "   ", "label": "黄金", "skill_id": "TTFUND_GOLD_INFO"}],
        },
        {
            "draft_id": "a" * 32,
            "template_id": "finance_90s",
            "title": "标题",
            "video_task_ids": [f"task-{index}" for index in range(21)],
        },
        {
            "draft_id": "a" * 32,
            "template_id": "finance_90s",
            "title": "标题",
            "video_task_ids": ["not-a-task-id"],
        },
    ],
)
def test_content_draft_rejects_unsafe_persisted_payloads(payload: dict) -> None:
    with pytest.raises(ValueError):
        ContentDraft.model_validate(payload)


def test_draft_store_rejects_unsafe_ids_before_path_access(tmp_path: Path) -> None:
    store = ContentDraftStore(tmp_path)
    unsafe_ids = ["../outside", "a" * 31, "A" * 32, " " + "0" * 32 + " "]

    for unsafe_id in unsafe_ids:
        with pytest.raises(FileNotFoundError):
            store.get(unsafe_id)
        with pytest.raises(FileNotFoundError):
            store.mutate(unsafe_id, lambda _draft: None)
        with pytest.raises(FileNotFoundError):
            store.delete(unsafe_id)
        with pytest.raises(FileNotFoundError):
            store.source_dir(unsafe_id)
        with pytest.raises(FileNotFoundError):
            store.write_source(unsafe_id, "valid_source", {"ok": True})

    assert not (tmp_path / "outside").exists()


def test_draft_store_rejects_symlinked_draft_directory(tmp_path: Path) -> None:
    store = ContentDraftStore(tmp_path)
    draft_id = "0" * 32
    outside = tmp_path / "outside"
    outside.mkdir()
    draft = ContentDraft(draft_id=draft_id, template_id="finance_90s", title="外部草稿")
    (outside / "draft.json").write_text(draft.model_dump_json(), encoding="utf-8")
    (tmp_path / "content-drafts" / draft_id).symlink_to(outside, target_is_directory=True)

    with pytest.raises(FileNotFoundError):
        store.get(draft_id)
    with pytest.raises(FileNotFoundError):
        store.source_dir(draft_id)
    with pytest.raises(FileNotFoundError):
        store.write_source(draft_id, "valid_source", {"ok": True})
    with pytest.raises(FileNotFoundError):
        store.save(ContentDraft(draft_id=draft_id, template_id="finance_90s", title="更新外部草稿"))
    with pytest.raises(FileNotFoundError):
        store.delete(draft_id)

    assert store.list() == []
    assert outside.is_dir()
    assert (outside / "draft.json").is_file()
    assert "更新外部草稿" not in (outside / "draft.json").read_text(encoding="utf-8")


def test_draft_store_rejects_symlinked_sources_directory(tmp_path: Path) -> None:
    store = ContentDraftStore(tmp_path)
    draft = store.create(template_id="finance_90s", title="今日金融资讯", focus_assets=[], research=[])
    outside = tmp_path / "outside-sources"
    outside.mkdir()
    sources = tmp_path / "content-drafts" / draft.draft_id / "sources"
    sources.symlink_to(outside, target_is_directory=True)

    with pytest.raises(FileNotFoundError):
        store.source_dir(draft.draft_id)
    with pytest.raises(FileNotFoundError):
        store.write_source(draft.draft_id, "valid_source", {"ok": True})

    assert outside.is_dir()
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("source_id", ["", "-", "_", "a" * 81, "../outside", "bad/source"])
def test_draft_store_rejects_unsafe_source_ids(tmp_path: Path, source_id: str) -> None:
    store = ContentDraftStore(tmp_path)
    draft = store.create(template_id="finance_90s", title="今日金融资讯", focus_assets=[], research=[])

    with pytest.raises(ValueError, match="invalid source id"):
        store.write_source(draft.draft_id, source_id, {"ok": True})

    assert not (tmp_path / "outside.json").exists()
    assert list(store.source_dir(draft.draft_id).glob("*.json")) == []


def test_draft_store_uses_revision_conflicts_and_atomic_json(tmp_path: Path) -> None:
    store = ContentDraftStore(tmp_path)
    draft = store.create(
        template_id="finance_90s",
        title="今日金融资讯",
        focus_assets=["东方财富"],
        research=[ResearchSource(source_id="gold", label="黄金行情", skill_id="TTFUND_GOLD_INFO")],
    )
    original_revision = draft.revision
    draft.script = "已编辑"
    saved = store.save(draft, expected_revision=original_revision)

    assert saved.revision == original_revision + 1
    assert store.get(draft.draft_id).script == "已编辑"
    assert not list((tmp_path / "content-drafts").rglob("*.tmp"))
    with pytest.raises(DraftConflictError):
        store.save(draft, expected_revision=original_revision)


def test_content_collection_does_not_hold_store_lock_or_overwrite_manual_script(tmp_path: Path, monkeypatch) -> None:
    service = ContentStudioService(Settings(data_dir=tmp_path, ai_briefing_dir=tmp_path / "briefings"))
    draft = service.store.create(
        template_id="ai_briefing_90s",
        title="每日 AI 简报",
        focus_assets=[],
        research=service._pending_sources("ai_briefing_90s", []),
    )
    source = ResearchSource(
        source_id="ai-briefing",
        label="最新 AI 简报文章",
        skill_id="AI_BRIEFING_HANDOFF",
        status="succeeded",
        payload={"date": "20260805", "article_markdown": "# AI 简报"},
    )
    monkeypatch.setattr(service, "_collect_ai_briefing_source", lambda _draft_id: source)
    started = threading.Event()
    release = threading.Event()

    def slow_generate(_draft):
        started.set()
        assert release.wait(timeout=2)
        return "自动生成稿。", {"generation_mode": "llm", "attempts": []}

    monkeypatch.setattr(service, "_generate_initial_script", slow_generate)
    worker = threading.Thread(target=service.collect_now, args=(draft.draft_id,))
    worker.start()
    assert started.wait(timeout=1)

    saved = service.update(draft.draft_id, revision=draft.revision, title="人工标题", script="人工编辑稿。")
    release.set()
    worker.join(timeout=2)
    current = service.store.get(draft.draft_id)
    service.shutdown()

    assert not worker.is_alive()
    assert saved.script == "人工编辑稿。"
    assert current.script == "人工编辑稿。"
    assert current.title == "人工标题"
    assert current.research[0].status == "succeeded"


def test_draft_store_reports_corrupt_draft_file(tmp_path: Path) -> None:
    store = ContentDraftStore(tmp_path)
    draft = store.create(template_id="finance_90s", title="今日金融资讯", focus_assets=[], research=[])
    store._path(draft.draft_id).write_text("{not-json\n", encoding="utf-8")

    with pytest.raises(DraftCorruptError, match="content draft is invalid"):
        store.get(draft.draft_id)

    assert store.list() == []


def test_ttskill_client_rejects_non_allowlisted_skills(tmp_path: Path) -> None:
    client = TTSkillClient(Settings(data_dir=tmp_path))

    with pytest.raises(TTSkillError, match="read-only"):
        client.invoke("ACCOUNT_HOLDING", {})


def test_content_collection_fails_without_daily_markdown(tmp_path: Path, monkeypatch) -> None:
    stub_content_studio_llm(monkeypatch)
    service = ContentStudioService(Settings(data_dir=tmp_path, finance_md_dir=tmp_path / "missing-finance"))
    monkeypatch.setattr(service, "_queue_collection", lambda _draft_id: None)
    draft = service.store.create(
        template_id="finance_90s",
        title="今日金融资讯",
        focus_assets=["东方财富"],
        research=service._pending_sources("finance_90s", ["东方财富"]),
    )

    def invoke(skill_id: str, _body: dict):
        if skill_id == "TTFUND_STOCK_PRICE_QUERY":
            raise TTSkillError("temporary failure")
        if skill_id == "TTFUND_BOND_MARKET":
            return {"data": {"module_1": [{"updateTime": "2026-08-05", "textSummary": "债市偏强"}]}}
        if skill_id == "TTFUND_MACRO_DATA":
            return {
                "data": {
                    "frequencies": [
                        {
                            "频率": "月频数据",
                            "记录": [
                                {
                                    "日期": "2026-07-01",
                                    "CPI:当月同比(%)": 1.0,
                                    "PPI:当月同比(%)": 4.1,
                                    "PMI(%)": 49.2,
                                    "M2:同比(%)": 8.0,
                                }
                            ],
                        }
                    ]
                }
            }
        return {
            "data": {
                "as_of": "2026-08-05",
                "gold_quotes": {
                    "gold_futures_shfe": {
                        "date": "2026-08-05",
                        "close": 910.4,
                        "change_pct": 2.7,
                        "volume": 295465,
                    },
                    "sge_benchmark": {"date": "2026-08-04", "evening_price": 883.88},
                },
                "risk_indicators": {
                    "vix": {"INDICATOR_VAL": 16.5},
                    "dxy": {"INDICATOR_VAL": 99.87},
                    "exchprice": {"EXCHPRICE": 6.7889},
                },
                "news": {
                    "items": [
                        {
                            "publish_date": "2026-08-05",
                            "summary": "美伊局势持续缓和，霍尔木兹海峡重开预期升温。",
                        }
                    ]
                },
            }
        }

    monkeypatch.setattr(service.client, "invoke", invoke)
    service.collect_now(draft.draft_id)
    collected = service.store.get(draft.draft_id)
    service.shutdown()

    assert collected.status == "partial"
    assert collected.research[0].source_id == "finance-daily"
    assert collected.research[0].status == "failed"
    assert {source.source_id for source in collected.research} == {
        "finance-daily",
        "gold",
        "bond",
        "macro-cn",
        "stock-1",
    }
    assert sum(source.status == "succeeded" for source in collected.research) == 3
    assert sum(source.status == "failed" for source in collected.research) == 2
    assert "黄金期货主力收于910.4元每克" in collected.script
    assert collected.script.endswith("不构成投资建议。")
    assert collected.research_error == "部分资料获取失败，可刷新重试。"


def test_content_collection_rejects_single_source_finance_draft(tmp_path: Path, monkeypatch) -> None:
    service = ContentStudioService(Settings(data_dir=tmp_path, finance_md_dir=tmp_path / "missing-finance"))
    monkeypatch.setattr(service, "_queue_collection", lambda _draft_id: None)
    draft = service.store.create(
        template_id="finance_90s",
        title="今日金融资讯",
        focus_assets=[],
        research=service._pending_sources("finance_90s", []),
    )

    def invoke(skill_id: str, _body: dict):
        if skill_id == "TTFUND_BOND_MARKET":
            return {"data": {"module_1": [{"updateTime": "2026-08-05", "textSummary": "债市偏强"}]}}
        raise TTSkillError("temporary failure")

    monkeypatch.setattr(service.client, "invoke", invoke)
    service.collect_now(draft.draft_id)
    collected = service.store.get(draft.draft_id)
    service.shutdown()

    assert collected.status == "failed"
    assert sum(source.status == "succeeded" for source in collected.research) == 1
    assert collected.script == ""
    assert "缺少关键模块" in (collected.research_error or "")


def test_content_collection_does_not_generate_script_when_all_sources_fail(tmp_path: Path, monkeypatch) -> None:
    service = ContentStudioService(Settings(data_dir=tmp_path, finance_md_dir=tmp_path / "missing-finance"))
    monkeypatch.setattr(service, "_queue_collection", lambda _draft_id: None)
    draft = service.store.create(
        template_id="finance_90s",
        title="今日金融资讯",
        focus_assets=[],
        research=service._pending_sources("finance_90s", []),
    )

    monkeypatch.setattr(
        service.client,
        "invoke",
        lambda _skill_id, _body: (_ for _ in ()).throw(TTSkillError("temporary failure")),
    )
    service.collect_now(draft.draft_id)
    collected = service.store.get(draft.draft_id)
    service.shutdown()

    assert collected.status == "failed"
    assert collected.script == ""
    assert collected.research_error == "没有启用或获取到可用于生成初稿的财经资料。"


def test_ai_briefing_template_imports_latest_dated_article(tmp_path: Path, monkeypatch) -> None:
    source_root = tmp_path / "briefings"
    older = source_root / "20260804"
    latest = source_root / "20260805"
    older.mkdir(parents=True)
    latest.mkdir()
    (older / "article.md").write_text("---\n标题: 旧简报\n---\n\n旧内容", encoding="utf-8")
    long_summary = "模型发布、算力建设、应用落地与监管规则同步推进，所有事实仍需按来源日期人工核对。" * 10
    (latest / "article.md").write_text(
        f"---\n标题: 最新 AI 简报\n摘要: {long_summary}\n---\n\n# 最新 AI 简报\n\n正文。",
        encoding="utf-8",
    )
    (latest / "video_handoff.json").write_text(
        '{"title":"最新 AI 简报","created_at":"2026-08-05T09:00:00"}', encoding="utf-8"
    )
    ai_script = (
        "最新AI简报，今天的重点是模型发布、算力建设、应用落地与监管规则同步推进。"
        "模型发布层面，多家团队继续把能力更新转向真实生产场景，编码、检索和工具调用成为观察重点。"
        "算力建设层面，训练和推理需求仍在增长，云端集群与端侧设备会共同决定应用体验。"
        "应用落地层面，企业更关注稳定性、成本和可控流程，而不是单次演示的热度。"
        "监管规则层面，平台开放、数据安全和智能体身份治理正在成为长期议题。"
        "整体看，AI行业已经进入能力、基础设施、产品和规则同时竞争的阶段。"
        "对内容生产者来说，真正重要的是把来源、日期、产品名称和结论边界讲清楚，"
        "不要把未经验证的传闻包装成确定判断。对企业来说，模型能力只是起点，"
        "还要看工作流接入、权限控制、成本上限和交付稳定性，以及团队协作。"
        "接下来需要持续核对来源日期，避免把短期发布节奏误读为长期确定性。"
    )
    stub_content_studio_llm(monkeypatch, ai_script=ai_script)
    service = ContentStudioService(Settings(data_dir=tmp_path / "output", ai_briefing_dir=source_root))
    draft = service.store.create(
        template_id="ai_briefing_90s",
        title="每日 AI 简报",
        focus_assets=[],
        research=service._pending_sources("ai_briefing_90s", []),
    )

    service.collect_now(draft.draft_id)
    collected = service.store.get(draft.draft_id)
    service.shutdown()

    assert collected.status == "ready"
    assert collected.research[0].data_as_of == "20260805"
    assert collected.research[0].payload["title"] == "最新 AI 简报"
    assert "模型发布、算力建设" in collected.research[0].payload["article_markdown"]
    assert collected.script == ai_script
    assert collected.title == "最新 AI 简报"


def test_ai_briefing_auto_script_rejects_overlong_draft(tmp_path: Path, monkeypatch) -> None:
    source_root = tmp_path / "briefings"
    latest = source_root / "20260805"
    latest.mkdir(parents=True)
    long_summary = "模型发布、算力建设、应用落地与监管规则同步推进，所有事实仍需按来源日期人工核对。" * 10
    (latest / "article.md").write_text(
        f"---\n标题: 最新 AI 简报\n摘要: {long_summary}\n---\n\n# 最新 AI 简报\n\n正文。",
        encoding="utf-8",
    )
    ai_script = (
        "最新AI简报，今天的重点是模型发布、算力建设、应用落地与监管规则同步推进。"
        "模型发布层面，多家团队继续把能力更新转向真实生产场景，编码、检索和工具调用成为观察重点。"
        "算力建设层面，训练和推理需求仍在增长，云端集群与端侧设备会共同决定应用体验。"
        "应用落地层面，企业更关注稳定性、成本和可控流程，而不是单次演示的热度。"
        "监管规则层面，平台开放、数据安全和智能体身份治理正在成为长期议题。"
        "整体看，AI行业已经进入能力、基础设施、产品和规则同时竞争的阶段。"
        "对内容生产者来说，真正重要的是把来源、日期、产品名称和结论边界讲清楚，不要把未经验证的传闻包装成确定判断。"
        "对企业来说，模型能力只是起点，还要看工作流接入、权限控制、成本上限和交付稳定性，以及团队协作。"
        "这段初稿故意超过生成视频前的字数限制，用来确认资料采集完成后不会因为长度被拒绝。"
        "编辑仍然可以在生成视频前裁剪表达，把重点压缩到适合九十秒口播的范围里。"
    )
    ai_script += "后续还需要继续核对来源日期、产品名称、事实边界和风险表述，避免把短期发布节奏误读为长期确定性。" * 5
    stub_content_studio_llm(monkeypatch, ai_script=ai_script)
    service = ContentStudioService(Settings(data_dir=tmp_path / "output", ai_briefing_dir=source_root))
    draft = service.store.create(
        template_id="ai_briefing_90s",
        title="每日 AI 简报",
        focus_assets=[],
        research=service._pending_sources("ai_briefing_90s", []),
    )

    service.collect_now(draft.draft_id)
    collected = service.store.get(draft.draft_id)
    video_errors = service.validate_for_video(collected)
    service.shutdown()

    assert 350 <= len(collected.script) <= 500
    assert collected.status == "ready"
    assert collected.script_generation["generation_mode"] == "validated_rule_fallback"
    assert video_errors == []


@pytest.mark.parametrize("script", ["短稿。", "长稿。" * 350])
def test_content_studio_allows_scripts_outside_recommended_length(tmp_path: Path, script: str) -> None:
    service = ContentStudioService(Settings(data_dir=tmp_path))
    draft = ContentDraft(
        draft_id="a" * 32,
        template_id="ai_briefing_90s",
        title="每日 AI 简报",
        script=script,
        status="ready",
        research=[
            ResearchSource(
                source_id="ai-briefing",
                label="最新 AI 简报文章",
                skill_id="AI_BRIEFING_HANDOFF",
                status="succeeded",
            )
        ],
    )

    errors = service.validate_for_video(draft)
    service.shutdown()

    assert errors == []


@pytest.mark.parametrize(
    ("script", "expected_error"),
    [
        ("短稿待补充", "口播稿仍包含未完成的占位内容"),
        ("市场观察" * 140, "口播稿必须包含风险提示"),
    ],
)
def test_content_studio_still_blocks_placeholders_and_missing_finance_disclaimer(
    tmp_path: Path, script: str, expected_error: str
) -> None:
    service = ContentStudioService(Settings(data_dir=tmp_path))
    draft = ContentDraft(
        draft_id="a" * 32,
        template_id="finance_90s",
        title="今日金融资讯",
        script=script,
        status="ready",
        research=[
            ResearchSource(
                source_id="finance-daily",
                label="当天基金日报",
                skill_id="FINANCE_DAILY_MARKDOWN",
                status="succeeded",
            )
        ],
    )

    errors = service.validate_for_video(draft)
    service.shutdown()

    assert any(expected_error in error for error in errors)
    assert all("300–600" not in error for error in errors)


@pytest.mark.parametrize(
    ("template_id", "script", "expected_error"),
    [
        ("finance_90s", "走势变化仍需独立判断，不构成投资建议。", "财经口播稿必须包含市场风险提示"),
        ("finance_90s", "建议买入。面对市场波动，请审慎决策。不构成投资建议。", "财经口播稿包含确定性或诱导性投资表述"),
        ("ai_briefing_90s", "# 标题\n这是人工审核稿。", "正文不能包含 Markdown"),
        ("ai_briefing_90s", "以下是口播稿：内容仍在核对", "正文不能包含解释性前缀"),
        ("ai_briefing_90s", "人工审核稿（尚未结束。", "正文包含不配对的括号或引号"),
    ],
)
def test_content_studio_blocks_reviewed_safety_and_format_errors(
    tmp_path: Path, template_id: str, script: str, expected_error: str
) -> None:
    service = ContentStudioService(Settings(data_dir=tmp_path))
    draft = ContentDraft(
        draft_id="c" * 32,
        template_id=template_id,
        title="人工审核稿",
        script=script,
        status="ready",
        research=[ResearchSource(source_id="source", label="资料", skill_id="AI_BRIEFING_HANDOFF", status="succeeded")],
    )

    errors = service.validate_for_video(draft)
    service.shutdown()

    assert any(expected_error in error for error in errors)


def test_content_studio_still_blocks_empty_script_and_collecting_status(tmp_path: Path) -> None:
    service = ContentStudioService(Settings(data_dir=tmp_path))
    empty_draft = ContentDraft(
        draft_id="a" * 32,
        template_id="ai_briefing_90s",
        title="每日 AI 简报",
        script="",
        status="ready",
        research=[],
    )
    collecting_draft = ContentDraft(
        draft_id="b" * 32,
        template_id="ai_briefing_90s",
        title="每日 AI 简报",
        script="短稿",
        status="collecting",
        research=[
            ResearchSource(
                source_id="ai-briefing",
                label="最新 AI 简报文章",
                skill_id="AI_BRIEFING_HANDOFF",
                status="succeeded",
            )
        ],
    )

    empty_errors = service.validate_for_video(empty_draft)
    collecting_errors = service.validate_for_video(collecting_draft)
    service.shutdown()

    assert "口播稿不能为空" in empty_errors
    assert "最新资料仍在采集中，请完成资料核对后再生成视频" in collecting_errors


def test_finance_template_prefers_today_markdown_before_realtime_sources(tmp_path: Path, monkeypatch) -> None:
    today = datetime.now().date().isoformat()
    source_root = tmp_path / "fund-daily"
    source_root.mkdir()
    (source_root / f"{today}_18-00-00.md").write_text(
        """
## Prompt
不要读这里

## Response
一句话总结：科技成长放量走强，红利防御回落，资金风险偏好修复。

## 四指数表格
| 指数 | 点位 | 涨跌幅 |
| --- | --- | --- |
| 上证指数 | 3832.26 | +0.72% |
| 沪深300 | 4588.20 | +0.85% |
| 创业板指 | 3343.96 | +3.06% |
| 科创50 | 1635.96 | +2.99% |

## 今日板块轮动
领涨方向：
- 传媒 +6.00%，计算机 +5.56%，通信 +4.26%。
领跌方向：
- 银行 -1.38%，食品饮料 -0.55%。

## 今日关键词
1. 高低切换：红利退场，科技接力。
2. AI算力链全线点火。
3. 量能放大，反弹有底气。

## 我的解读
策略上：
- 已上科技车的，持有但不追高。
- 空仓观望的，等回踩和缩量企稳。
""",
        encoding="utf-8",
    )
    stub_content_studio_llm(monkeypatch, finance_script=FINANCE_DAILY_LLM_SCRIPT)
    service = ContentStudioService(Settings(data_dir=tmp_path / "output", finance_md_dir=source_root))
    monkeypatch.setattr(service, "_queue_collection", lambda _draft_id: None)
    invoked: list[str] = []

    def invoke(skill_id: str, _body: dict):
        invoked.append(skill_id)
        if skill_id == "TTFUND_BOND_MARKET":
            return {"data": {"module_1": [{"updateTime": today, "textSummary": "债市偏强"}]}}
        if skill_id == "TTFUND_MACRO_DATA":
            return {"data": {"frequencies": [{"频率": "月频数据", "记录": [{"日期": today, "PMI(%)": 49.2}]}]}}
        return {"data": {"as_of": today, "gold_quotes": {"sge_benchmark": {"evening_price": 883.88}}}}

    monkeypatch.setattr(service.client, "invoke", invoke)
    draft = service.store.create(
        template_id="finance_90s",
        title="今日金融资讯",
        focus_assets=[],
        research=service._pending_sources("finance_90s", []),
    )

    service.collect_now(draft.draft_id)
    collected = service.store.get(draft.draft_id)
    service.shutdown()

    assert collected.status == "ready"
    assert [source.source_id for source in collected.research] == ["finance-daily", "gold", "bond", "macro-cn"]
    assert set(invoked) == {"TTFUND_GOLD_INFO", "TTFUND_BOND_MARKET", "TTFUND_MACRO_DATA"}
    assert collected.script == FINANCE_DAILY_LLM_SCRIPT
    assert collected.script_generation["generation_mode"] == "llm"
    assert collected.script_generation["quality_report"]["passed"] is True
    assert "不要读这里" not in collected.script


def test_finance_template_can_disable_realtime_sources(tmp_path: Path, monkeypatch) -> None:
    today = datetime.now().date().isoformat()
    source_root = tmp_path / "fund-daily"
    source_root.mkdir()
    (source_root / f"{today}_18-00-00.md").write_text(
        """
## Response
一句话总结：科技成长放量走强，红利防御回落，资金风险偏好修复。

## 四指数表格
| 指数 | 点位 | 涨跌幅 |
| --- | --- | --- |
| 上证指数 | 3832.26 | +0.72% |
| 沪深300 | 4588.20 | +0.85% |
| 创业板指 | 3343.96 | +3.06% |
| 科创50 | 1635.96 | +2.99% |

## 今日板块轮动
领涨方向：
- 传媒 +6.00%，计算机 +5.56%，通信 +4.26%。
领跌方向：
- 银行 -1.38%，食品饮料 -0.55%。

## 我的解读
策略上：
- 已上科技车的，持有但不追高。
""",
        encoding="utf-8",
    )
    stub_content_studio_llm(monkeypatch, finance_script=FINANCE_DAILY_LLM_SCRIPT)
    service = ContentStudioService(Settings(data_dir=tmp_path / "output", finance_md_dir=source_root))
    monkeypatch.setattr(service, "_queue_collection", lambda _draft_id: None)
    monkeypatch.setattr(
        service.client,
        "invoke",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("disabled sources must not run")),
    )
    draft = service.create(
        CreateDraftInput(
            title="今日金融资讯",
            enabled_source_ids=["finance-daily"],
        )
    )
    service.collect_now(draft.draft_id)
    collected = service.store.get(draft.draft_id)
    service.shutdown()

    assert collected.status == "ready"
    assert collected.enabled_source_ids == ["finance-daily"]
    assert [source.source_id for source in collected.research] == ["finance-daily"]
    assert collected.research_error is None
    assert collected.script == FINANCE_DAILY_LLM_SCRIPT
    assert collected.script_generation["generation_mode"] == "llm"
    assert collected.script_generation["quality_report"]["passed"] is True


def test_finance_refresh_updates_enabled_sources(tmp_path: Path, monkeypatch) -> None:
    source_root = tmp_path / "fund-daily"
    source_root.mkdir()
    service = ContentStudioService(Settings(data_dir=tmp_path / "output", finance_md_dir=source_root))
    monkeypatch.setattr(service, "_queue_collection", lambda _draft_id: None)
    draft = service.create(CreateDraftInput(title="今日金融资讯", enabled_source_ids=["finance-daily", "gold"]))

    refreshed = service.refresh(draft.draft_id, draft.revision, enabled_source_ids=["finance-daily"])
    service.shutdown()

    assert refreshed.enabled_source_ids == ["finance-daily"]
    assert refreshed.status == "collecting"
    assert [source.source_id for source in refreshed.research] == ["finance-daily"]


def test_finance_template_falls_back_to_rule_script_when_llm_misses_required_market_terms(
    tmp_path: Path, monkeypatch
) -> None:
    today = datetime.now().date().isoformat()
    source_root = tmp_path / "fund-daily"
    source_root.mkdir()
    (source_root / f"{today}_18-00-00.md").write_text(
        """
## Response
一句话总结：科技成长放量走强，红利防御回落，资金风险偏好修复。

## 四指数表格
| 指数 | 点位 | 涨跌幅 |
| --- | --- | --- |
| 上证指数 | 3832.26 | +0.72% |
| 沪深300 | 4588.20 | +0.85% |
| 创业板指 | 3343.96 | +3.06% |
| 科创50 | 1635.96 | +2.99% |

## 今日板块轮动
领涨方向：
- 传媒 +6.00%，计算机 +5.56%，通信 +4.26%。
领跌方向：
- 银行 -1.38%，食品饮料 -0.55%。

## 今日关键词
1. 高低切换：红利退场，科技接力。
2. AI算力链全线点火。
3. 量能放大，反弹有底气。

## 我的解读
策略上：
- 已上科技车的，持有但不追高。
- 空仓观望的，等回踩和缩量企稳。
""",
        encoding="utf-8",
    )
    invalid_llm_script = (
        "A股收盘观察。今天主要股指同步上涨，成长方向弹性更强。行业轮动上，传媒、计算机、通信继续活跃，"
        "银行和食品饮料回落。策略上不宜情绪化追高。面对市场波动，请结合自身风险承受能力审慎决策。"
        "以上内容仅为市场信息整理，不构成投资建议。"
    )
    stub_content_studio_llm(monkeypatch, finance_script=invalid_llm_script)
    service = ContentStudioService(Settings(data_dir=tmp_path / "output", finance_md_dir=source_root))
    monkeypatch.setattr(
        service.client,
        "invoke",
        lambda _skill_id, _body: (_ for _ in ()).throw(TTSkillError("temporary failure")),
    )
    draft = service.store.create(
        template_id="finance_90s",
        title="今日金融资讯",
        focus_assets=[],
        research=service._pending_sources("finance_90s", []),
    )

    service.collect_now(draft.draft_id)
    collected = service.store.get(draft.draft_id)
    service.shutdown()

    assert collected.status == "ready"
    assert collected.research_error is None
    assert collected.script != invalid_llm_script
    assert "指数" in collected.script
    assert "板块" in collected.script
    assert any(marker in collected.script for marker in ("风险承受能力", "审慎决策"))
    assert "不构成" in collected.script and "投资建议" in collected.script
    assert "资料已就绪，但自动初稿生成失败" not in (collected.research_error or "")


def test_finance_template_rejects_invalid_minimal_script_when_rule_fallback_fails(tmp_path: Path, monkeypatch) -> None:
    today = datetime.now().date().isoformat()
    source_root = tmp_path / "fund-daily"
    source_root.mkdir()
    (source_root / f"{today}_18-00-00.md").write_text(
        """
## Response
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
""",
        encoding="utf-8",
    )
    invalid_llm_script = (
        "A股收盘观察。今天主要股指普遍承压，防御方向相对抗跌。行业上石油、银行表现较强，"
        "半导体承压。面对市场波动，请结合自身风险承受能力审慎决策。以上内容仅为市场信息整理，不构成投资建议。"
    )
    stub_content_studio_llm(monkeypatch, finance_script=invalid_llm_script)
    service = ContentStudioService(Settings(data_dir=tmp_path / "output", finance_md_dir=source_root))
    minimal_scripts: list[tuple[str, dict[str, object]]] = []
    build_minimal = service._build_minimal_finance_daily_script

    def capture_minimal(material: dict[str, object]) -> str:
        script = build_minimal(material)
        minimal_scripts.append((script, material))
        return script

    def material_without_rule_fallback(response: str, publication_date: str) -> dict[str, object]:
        built = build_finance_script_material(response, publication_date)
        return {**built, "fallback_script": "", "fallback_error": "rule fallback unavailable"}

    monkeypatch.setattr(service, "_build_minimal_finance_daily_script", capture_minimal)
    monkeypatch.setattr(
        "content_pipeline.content_studio.service.build_finance_script_material",
        material_without_rule_fallback,
    )
    monkeypatch.setattr(
        service.client,
        "invoke",
        lambda _skill_id, _body: (_ for _ in ()).throw(TTSkillError("temporary failure")),
    )
    draft = service.store.create(
        template_id="finance_90s",
        title="今日金融资讯",
        focus_assets=[],
        research=service._pending_sources("finance_90s", []),
    )

    service.collect_now(draft.draft_id)
    collected = service.store.get(draft.draft_id)
    service.shutdown()

    assert collected.status == "failed"
    assert "资料已就绪，但自动初稿生成失败" in collected.research_error
    assert collected.script == ""
    assert collected.research[0].status == "succeeded"
    assert len(minimal_scripts) == 1
    minimal_script, material = minimal_scripts[0]
    assert "指数方面" in minimal_script
    assert "板块方面" in minimal_script
    assert "上证指数报3894.42点" in minimal_script
    assert "石油石化中国石油" in minimal_script
    assert "风险承受能力" in minimal_script
    assert "不构成" in minimal_script and "投资建议" in minimal_script
    report = validate_script(minimal_script, build_structured_facts("finance", material))
    assert not report.passed
    assert {issue.code for issue in report.errors} == {
        "finance_opening_market_anchor_missing",
        "finance_weak_opening",
    }


def test_finance_template_uses_yesterday_markdown_when_today_is_missing(tmp_path: Path, monkeypatch) -> None:
    yesterday = (datetime.now().date() - timedelta(days=1)).isoformat()
    source_root = tmp_path / "fund-daily"
    source_root.mkdir()
    (source_root / f"{yesterday}_18-00-00.md").write_text(
        """
## Response
一句话总结：科技成长放量走强，红利防御回落，资金风险偏好修复。

## 四指数表格
| 指数 | 点位 | 涨跌幅 |
| --- | --- | --- |
| 上证指数 | 3832.26 | +0.72% |
| 沪深300 | 4588.20 | +0.85% |
| 创业板指 | 3343.96 | +3.06% |
| 科创50 | 1635.96 | +2.99% |

## 今日板块轮动
领涨方向：
- 传媒 +6.00%，计算机 +5.56%，通信 +4.26%。
领跌方向：
- 银行 -1.38%，食品饮料 -0.55%。

## 今日关键词
1. 高低切换：红利退场，科技接力。
2. AI算力链全线点火。
3. 量能放大，反弹有底气。

## 我的解读
策略上：
- 已上科技车的，持有但不追高。
- 空仓观望的，等回踩和缩量企稳。
""",
        encoding="utf-8",
    )
    stub_content_studio_llm(monkeypatch, finance_script=FINANCE_DAILY_LLM_SCRIPT)
    service = ContentStudioService(Settings(data_dir=tmp_path / "output", finance_md_dir=source_root))
    monkeypatch.setattr(service, "_queue_collection", lambda _draft_id: None)
    monkeypatch.setattr(
        service.client,
        "invoke",
        lambda _skill_id, _body: (_ for _ in ()).throw(TTSkillError("temporary failure")),
    )
    draft = service.store.create(
        template_id="finance_90s",
        title="今日金融资讯",
        focus_assets=[],
        research=service._pending_sources("finance_90s", []),
    )

    service.collect_now(draft.draft_id)
    collected = service.store.get(draft.draft_id)
    service.shutdown()

    assert collected.status == "ready"
    assert collected.research_error is None
    assert [source.source_id for source in collected.research] == ["finance-daily", "gold", "bond", "macro-cn"]
    assert collected.research[0].data_as_of == yesterday
    assert collected.research[0].status == "succeeded"
    assert sum(source.status == "failed" for source in collected.research) == 3
    assert collected.script == FINANCE_DAILY_LLM_SCRIPT
    assert collected.script_generation["generation_mode"] == "llm"
    assert collected.script_generation["quality_report"]["passed"] is True


def test_finance_collection_repairs_old_draft_missing_daily_source(tmp_path: Path, monkeypatch) -> None:
    today = datetime.now().date().isoformat()
    source_root = tmp_path / "fund-daily"
    source_root.mkdir()
    (source_root / f"{today}_18-00-00.md").write_text(
        """
## Response
一句话总结：科技成长放量走强，红利防御回落，资金风险偏好修复。

## 四指数表格
| 指数 | 点位 | 涨跌幅 |
| --- | --- | --- |
| 上证指数 | 3832.26 | +0.72% |
| 沪深300 | 4588.20 | +0.85% |
| 创业板指 | 3343.96 | +3.06% |
| 科创50 | 1635.96 | +2.99% |

## 今日板块轮动
领涨方向：
- 传媒 +6.00%，计算机 +5.56%，通信 +4.26%。
领跌方向：
- 银行 -1.38%，食品饮料 -0.55%。

## 今日关键词
1. 高低切换：红利退场，科技接力。
2. AI算力链全线点火。
3. 量能放大，反弹有底气。

## 我的解读
策略上：
- 已上科技车的，持有但不追高。
- 空仓观望的，等回踩和缩量企稳。
""",
        encoding="utf-8",
    )
    stub_content_studio_llm(monkeypatch, finance_script=FINANCE_DAILY_LLM_SCRIPT)
    service = ContentStudioService(Settings(data_dir=tmp_path / "output", finance_md_dir=source_root))
    monkeypatch.setattr(service, "_queue_collection", lambda _draft_id: None)
    monkeypatch.setattr(
        service.client,
        "invoke",
        lambda _skill_id, _body: (_ for _ in ()).throw(TTSkillError("temporary failure")),
    )
    draft = service.store.create(
        template_id="finance_90s",
        title="今日金融资讯",
        focus_assets=[],
        research=[ResearchSource(source_id="gold", label="黄金行情", skill_id="TTFUND_GOLD_INFO")],
    )

    service.collect_now(draft.draft_id)
    collected = service.store.get(draft.draft_id)
    service.shutdown()

    assert collected.status == "ready"
    assert collected.research_error is None
    assert [source.source_id for source in collected.research] == ["finance-daily", "gold", "bond", "macro-cn"]
    assert collected.research[0].status == "succeeded"
    assert collected.research[0].data_as_of == today
    assert sum(source.status == "failed" for source in collected.research) == 3
    assert collected.script == FINANCE_DAILY_LLM_SCRIPT
    assert collected.script_generation["generation_mode"] == "llm"
    assert collected.script_generation["quality_report"]["passed"] is True


def test_content_api_creates_draft_and_queues_dry_run_video(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ContentStudioService, "_queue_collection", lambda _service, _draft_id: None)
    settings = Settings(
        data_dir=tmp_path / "output",
        mpt_dir=tmp_path / "mpt",
        web_allowed_hosts="testserver",
        web_allowed_origins="http://testserver",
        web_auth_required=True,
        web_admin_token="admin-" + "a" * 32,
        web_api_token="api-" + "b" * 32,
        web_session_secret="session-" + "c" * 32,
    )
    app = create_app(settings)
    headers = {"Authorization": "Bearer " + "api-" + "b" * 32}
    with TestClient(app, base_url="http://testserver", client=("127.0.0.1", 50000)) as client:
        assert client.get("/content/bootstrap").status_code == 401
        content_bootstrap = client.get("/content/bootstrap", headers=headers)
        assert {item["template_id"] for item in content_bootstrap.json()["templates"]} == {
            "finance_90s",
            "ai_briefing_90s",
        }
        template_descriptions = {
            item["template_id"]: item["description"] for item in content_bootstrap.json()["templates"]
        }
        assert "自动生成可审阅" in template_descriptions["finance_90s"]
        assert "自动生成可审阅" in template_descriptions["ai_briefing_90s"]
        created = client.post(
            "/content/drafts",
            headers=headers,
            json={"template_id": "finance_90s", "title": "今日金融资讯", "focus_assets": []},
        )
        draft = created.json()

        def mark_ready(current) -> None:
            current.status = "ready"
            current.research[0].status = "succeeded"

        ready = app.state.content_studio.store.mutate(draft["draft_id"], mark_ready)
        script = ("市场数据需要核对来源日期和统计口径，短期变化不能直接外推为长期趋势。" * 10)[:360]
        script += "面对市场波动，请结合自身风险承受能力审慎决策。以上内容仅为市场信息整理，不构成投资建议。"
        saved = client.patch(
            f"/content/drafts/{draft['draft_id']}",
            headers=headers,
            json={"revision": ready.revision, "title": "今日金融资讯", "script": script},
        )
        generated = client.post(
            f"/content/drafts/{draft['draft_id']}/video",
            headers=headers,
            json={"revision": saved.json()["revision"], "dry_run": True},
        )
        task_id = generated.json()["task_id"]
        for _ in range(30):
            snapshot = client.get(f"/status/{task_id}", headers=headers).json()
            if snapshot["status"] not in {"queued", "running"}:
                break
            time.sleep(0.02)

    assert created.status_code == 202
    assert saved.status_code == 200
    assert generated.status_code == 202
    assert snapshot["status"] == "succeeded"
    assert snapshot["task"]["content_type"] == "script_video"


def test_content_video_generation_collects_finance_sources_when_markdown_is_missing(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(ContentStudioService, "_queue_collection", lambda _service, _draft_id: None)
    settings = Settings(
        data_dir=tmp_path / "output",
        mpt_dir=tmp_path / "mpt",
        finance_md_dir=tmp_path / "missing-finance",
        web_allowed_hosts="testserver",
        web_allowed_origins="http://testserver",
        web_auth_required=True,
        web_admin_token="admin-" + "a" * 32,
        web_api_token="api-" + "b" * 32,
        web_session_secret="session-" + "c" * 32,
    )
    app = create_app(settings)
    app.state.orchestrator.run = lambda _task_id: None
    headers = {"Authorization": "Bearer " + "api-" + "b" * 32}

    def invoke(skill_id: str, _body: dict):
        if skill_id == "TTFUND_BOND_MARKET":
            return {"data": {"module_1": [{"updateTime": "2026-08-05", "textSummary": "债市偏强"}]}}
        if skill_id == "TTFUND_MACRO_DATA":
            return {
                "data": {
                    "frequencies": [
                        {
                            "频率": "月频数据",
                            "记录": [
                                {
                                    "日期": "2026-07-01",
                                    "CPI:当月同比(%)": 1.0,
                                    "PPI:当月同比(%)": 4.1,
                                    "PMI(%)": 49.2,
                                    "M2:同比(%)": 8.0,
                                }
                            ],
                        }
                    ]
                }
            }
        return {
            "data": {
                "as_of": "2026-08-05",
                "gold_quotes": {
                    "gold_futures_shfe": {"date": "2026-08-05", "close": 910.4, "change_pct": 2.7, "volume": 295465},
                    "sge_benchmark": {"date": "2026-08-04", "evening_price": 883.88},
                },
                "risk_indicators": {
                    "vix": {"INDICATOR_VAL": 16.5},
                    "dxy": {"INDICATOR_VAL": 99.87},
                    "exchprice": {"EXCHPRICE": 6.7889},
                },
                "news": {"items": [{"publish_date": "2026-08-05", "summary": "美伊局势持续缓和。"}]},
            }
        }

    monkeypatch.setattr(app.state.content_studio.client, "invoke", invoke)
    with TestClient(app, base_url="http://testserver", client=("127.0.0.1", 50000)) as client:
        created = client.post(
            "/content/drafts",
            headers=headers,
            json={"template_id": "finance_90s", "title": "今日金融资讯", "focus_assets": []},
        )
        draft = created.json()
        generated = client.post(
            f"/content/drafts/{draft['draft_id']}/video",
            headers=headers,
            json={"revision": draft["revision"], "dry_run": True},
        )
        collected = client.get(f"/content/drafts/{draft['draft_id']}", headers=headers).json()

    assert created.status_code == 202
    assert generated.status_code == 202
    assert collected["status"] == "partial"
    assert collected["research_error"] == "部分资料获取失败，可刷新重试。"
    assert [source["source_id"] for source in collected["research"]][0] == "finance-daily"
    assert collected["research"][0]["status"] == "failed"
    assert {source["source_id"] for source in collected["research"]} == {
        "finance-daily",
        "gold",
        "bond",
        "macro-cn",
    }
    assert {source["source_id"] for source in collected["research"] if source["status"] == "succeeded"} == {
        "gold",
        "bond",
        "macro-cn",
    }
    assert "黄金期货主力收于910.4元每克" in collected["script"]
    assert "不构成投资建议" in collected["script"]


def test_content_api_deletes_single_and_multiple_drafts(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ContentStudioService, "_queue_collection", lambda _service, _draft_id: None)
    settings = Settings(
        data_dir=tmp_path / "output",
        web_allowed_hosts="testserver",
        web_allowed_origins="http://testserver",
        web_auth_required=True,
        web_admin_token="admin-" + "a" * 32,
        web_api_token="api-" + "b" * 32,
        web_session_secret="session-" + "c" * 32,
    )
    app = create_app(settings)
    headers = {"Authorization": "Bearer " + "api-" + "b" * 32}
    with TestClient(app, base_url="http://testserver", client=("127.0.0.1", 50000)) as client:
        first = client.post(
            "/content/drafts",
            headers=headers,
            json={"template_id": "finance_90s", "title": "第一份", "focus_assets": []},
        ).json()
        second = client.post(
            "/content/drafts",
            headers=headers,
            json={"template_id": "finance_90s", "title": "第二份", "focus_assets": []},
        ).json()
        third = client.post(
            "/content/drafts",
            headers=headers,
            json={"template_id": "finance_90s", "title": "第三份", "focus_assets": []},
        ).json()
        for draft_id in (first["draft_id"], second["draft_id"], third["draft_id"]):
            app.state.content_studio.store.mutate(draft_id, lambda draft: setattr(draft, "status", "failed"))

        deleted = client.delete(f"/content/drafts/{first['draft_id']}", headers=headers)
        missing = client.get(f"/content/drafts/{first['draft_id']}", headers=headers)
        batch = client.post(
            "/content/drafts/batch-delete",
            headers=headers,
            json={
                "draft_ids": [
                    " " + second["draft_id"] + " ",
                    second["draft_id"],
                    third["draft_id"],
                    first["draft_id"],
                ]
            },
        )
        listed = client.get("/content/drafts", headers=headers).json()["drafts"]

    assert deleted.status_code == 200
    assert deleted.json() == {"draft_id": first["draft_id"], "deleted": True}
    assert missing.status_code == 404
    assert batch.status_code == 200
    assert set(batch.json()["deleted"]) == {second["draft_id"], third["draft_id"]}
    assert batch.json()["not_found"] == [first["draft_id"]]
    assert {draft["draft_id"] for draft in listed}.isdisjoint(
        {first["draft_id"], second["draft_id"], third["draft_id"]}
    )


def test_content_api_blocks_deleting_collecting_drafts(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ContentStudioService, "_queue_collection", lambda _service, _draft_id: None)
    settings = Settings(
        data_dir=tmp_path / "output",
        web_allowed_hosts="testserver",
        web_allowed_origins="http://testserver",
        web_auth_required=True,
        web_admin_token="admin-" + "a" * 32,
        web_api_token="api-" + "b" * 32,
        web_session_secret="session-" + "c" * 32,
    )
    app = create_app(settings)
    headers = {"Authorization": "Bearer " + "api-" + "b" * 32}
    with TestClient(app, base_url="http://testserver", client=("127.0.0.1", 50000)) as client:
        draft = client.post(
            "/content/drafts",
            headers=headers,
            json={"template_id": "finance_90s", "title": "采集中", "focus_assets": []},
        ).json()
        single = client.delete(f"/content/drafts/{draft['draft_id']}", headers=headers)
        batch = client.post(
            "/content/drafts/batch-delete",
            headers=headers,
            json={"draft_ids": [draft["draft_id"]]},
        )

    assert single.status_code == 409
    assert batch.status_code == 200
    assert batch.json()["blocked"] == [draft["draft_id"]]
    assert app.state.content_studio.store.get(draft["draft_id"]).status == "collecting"


def test_content_api_reports_corrupt_draft_without_leaking_path(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ContentStudioService, "_queue_collection", lambda _service, _draft_id: None)
    settings = Settings(
        data_dir=tmp_path / "output",
        web_allowed_hosts="testserver",
        web_allowed_origins="http://testserver",
        web_auth_required=True,
        web_admin_token="admin-" + "a" * 32,
        web_api_token="api-" + "b" * 32,
        web_session_secret="session-" + "c" * 32,
    )
    app = create_app(settings)
    headers = {"Authorization": "Bearer " + "api-" + "b" * 32}
    with TestClient(app, base_url="http://testserver", client=("127.0.0.1", 50000)) as client:
        draft = client.post(
            "/content/drafts",
            headers=headers,
            json={"template_id": "finance_90s", "title": "坏草稿", "focus_assets": []},
        ).json()
        app.state.content_studio.store._path(draft["draft_id"]).write_text("{not-json\n", encoding="utf-8")
        response = client.get(f"/content/drafts/{draft['draft_id']}", headers=headers)
        listed = client.get("/content/drafts", headers=headers)

    assert response.status_code == 500
    assert response.json()["detail"] == {
        "code": "content_draft_invalid",
        "message": "content draft is invalid",
    }
    assert str(tmp_path) not in response.text
    assert listed.status_code == 200
    assert listed.json() == {"drafts": []}


def test_content_api_batch_delete_reports_corrupt_draft_as_failed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ContentStudioService, "_queue_collection", lambda _service, _draft_id: None)
    settings = Settings(
        data_dir=tmp_path / "output",
        web_allowed_hosts="testserver",
        web_allowed_origins="http://testserver",
        web_auth_required=True,
        web_admin_token="admin-" + "a" * 32,
        web_api_token="api-" + "b" * 32,
        web_session_secret="session-" + "c" * 32,
    )
    app = create_app(settings)
    headers = {"Authorization": "Bearer " + "api-" + "b" * 32}
    with TestClient(app, base_url="http://testserver", client=("127.0.0.1", 50000)) as client:
        draft = client.post(
            "/content/drafts",
            headers=headers,
            json={"template_id": "finance_90s", "title": "坏草稿", "focus_assets": []},
        ).json()
        app.state.content_studio.store._path(draft["draft_id"]).write_text("{not-json\n", encoding="utf-8")
        response = client.post(
            "/content/drafts/batch-delete",
            headers=headers,
            json={"draft_ids": [draft["draft_id"]]},
        )

    assert response.status_code == 200
    assert response.json() == {
        "deleted": [],
        "blocked": [],
        "not_found": [],
        "failed": [draft["draft_id"]],
    }
    assert str(tmp_path) not in response.text


def test_content_api_rejects_invalid_batch_delete_draft_ids(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "output",
        web_allowed_hosts="testserver",
        web_allowed_origins="http://testserver",
        web_auth_required=True,
        web_admin_token="admin-" + "a" * 32,
        web_api_token="api-" + "b" * 32,
        web_session_secret="session-" + "c" * 32,
    )
    app = create_app(settings)
    headers = {"Authorization": "Bearer " + "api-" + "b" * 32}
    with TestClient(app, base_url="http://testserver", client=("127.0.0.1", 50000)) as client:
        response = client.post(
            "/content/drafts/batch-delete",
            headers=headers,
            json={"draft_ids": ["not-a-draft-id"]},
        )

    assert response.status_code == 422
