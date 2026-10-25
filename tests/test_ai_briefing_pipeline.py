from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image

from content_pipeline import ai_briefing_pipeline
from content_pipeline.ai_briefing_pipeline import (
    _sau_file_hash,
    acquire_run_lock,
    build_90_second_briefing_script,
    build_briefing_script_material,
    build_publish_title,
    build_video_title,
    normalize_date,
    normalize_handoff,
    release_run_lock,
    validate_ai_briefing_narration,
)
from content_pipeline.job_store import JobStore
from content_pipeline.models import (
    AdapterResult,
    ErrorCode,
    JobStatus,
    PipelineStep,
    PublishTarget,
    TaskInput,
    VideoValidation,
)
from content_pipeline.orchestrator import Orchestrator
from content_pipeline.settings import Settings
from content_pipeline.tools.narrated_mpt_client import NarratedMptResult

SAMPLE_ARTICLE = """---
标题: 国产模型集中亮相
作者: Wallbreaker
摘要: 世界人工智能大会开幕，国产模型团队集中发布新一代开源模型，编码、长上下文与端侧推理成为竞争焦点。海外方面，芯片厂商继续扩建主权人工智能基础设施，监管机构也要求平台向第三方助手开放更多关键接口。
---

# 国产模型集中亮相

## 今日焦点

### Qwen 新模型发布并开放权重

新模型进一步提升了编码和工具调用能力，并将向开发者开放权重。

### Kimi 登上编码评测榜首

新的混合专家模型在多个前端编码任务中取得领先成绩。

## 前沿动态

### Nvidia 建设新一代 AI 工厂

项目将部署新一代 CPU 和 GPU，服务当地科研和企业。

### 端侧小模型继续提速

多家厂商发布可在消费级设备运行的小模型。

### 欧盟推进平台开放

监管要求大型平台向竞争对手开放关键能力。

## 一句话快讯

- 芯片企业上调年度资本支出
- 企业开始加强智能体身份安全

## 结语

人工智能竞争已经从单一模型能力扩展到基础设施、生态和监管的综合较量。未来的胜负将取决于产品落地与开放生态。
"""


def _make_day(root: Path, date: str = "20260720") -> Path:
    day = root / date
    day.mkdir(parents=True)
    article = day / "article.md"
    text = day / "article.txt"
    article.write_text(SAMPLE_ARTICLE, encoding="utf-8")
    text.write_text("每日AI简报正文。" * 100, encoding="utf-8")
    (day / "video_handoff.json").write_text(
        json.dumps(
            {
                "date": date,
                "title": "国产模型集中亮相",
                "article_path": str(article),
                "text_path": str(text),
                "output_dir": str(day / "video"),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return day


def test_normalize_date_never_falls_back() -> None:
    assert normalize_date("2026-07-20") == "20260720"
    with pytest.raises(Exception, match="date must be"):
        normalize_date("yesterday")


def test_builds_clean_90_second_script() -> None:
    script = build_90_second_briefing_script(SAMPLE_ARTICLE, "20260720")
    assert 350 <= len(script) <= 500
    assert "Qwen" in script
    assert "Nvidia" in script
    assert "![" not in script
    assert "article.md" not in script
    assert script.endswith("。")


def test_llm_narration_validation_rejects_bad_format() -> None:
    material = build_briefing_script_material(SAMPLE_ARTICLE, "20260720")
    valid = (
        "Qwen发布新模型并开放权重，国产模型集中亮相。2026年7月20日的AI简报关注编码和工具调用，"
        "开发者生态有望进一步扩大。Kimi也在多个前端编码任务中取得领先，"
        "说明模型竞争正在从参数规模走向真实生产力。海外方面，Nvidia继续建设新一代AI工厂，CPU和GPU部署"
        "会支撑科研与企业应用。端侧小模型同步提速，多家厂商开始把AI能力推向消费级设备。监管层面，"
        "欧盟要求大型平台开放关键接口，智能体身份安全也被企业重点关注。整体看，AI竞争已经不只是单点模型"
        "能力，而是基础设施、应用生态和监管规则的综合较量。接下来，谁能把能力稳定落到产品里，谁就更可能占据主动。对观众来说，今天最值得关注的不是单个发布会的热度，而是模型能力、算力供给、端侧落地和开放规则正在同时推进。这会影响下一阶段的产品竞争，也需要持续观察实际部署效果。"
    )
    validate_ai_briefing_narration(valid, material)
    with pytest.raises(Exception, match="markdown"):
        validate_ai_briefing_narration("# 标题\n" + valid, material)
    with pytest.raises(Exception, match="complete Chinese sentence"):
        validate_ai_briefing_narration(valid.rstrip("。"), material)


def test_sau_file_hash_matches_partial_sha256_contract(tmp_path: Path) -> None:
    video = tmp_path / "briefing.mp4"
    video.write_bytes(b"header" + b"x" * (2 * 1024 * 1024) + b"footer")
    assert len(_sau_file_hash(video)) == 64


def test_publish_title_prefers_short_complete_lead() -> None:
    title = "WAIC 2026 开幕即高潮：Qwen 2.4万亿参数开路，Kimi K3封王，国产模型军团全线出击"
    assert build_publish_title(title) == "WAIC 2026 开幕即高潮"
    assert len(build_publish_title("没有标点但是特别特别长的人工智能每日新闻标题示例")) <= 20
    assert build_video_title(title) == "WAIC 2026 开幕即高潮"
    assert len(build_video_title("没有标点但是特别特别长的人工智能每日新闻标题示例")) <= 16


def test_handoff_accepts_legacy_sampo_and_rejects_external_output(tmp_path: Path) -> None:
    day = _make_day(tmp_path)
    handoff_path = day / "video_handoff.json"
    raw = json.loads(handoff_path.read_text(encoding="utf-8"))
    raw["consumer_profile"] = "sampo"
    normalized = normalize_handoff(raw, date="20260720", handoff_path=handoff_path, day_dir=day)
    assert normalized["consumer_profile"] == "sampo"
    assert normalized["runner"] == "ai-pipeline"
    raw["output_dir"] = str(tmp_path / "outside")
    with pytest.raises(Exception, match="output_dir"):
        normalize_handoff(raw, date="20260720", handoff_path=handoff_path, day_dir=day)


def test_run_lock_rejects_live_process(tmp_path: Path) -> None:
    lock = tmp_path / "handoff.lock.json"
    acquire_run_lock(lock, "20260720")
    try:
        with pytest.raises(Exception, match="already_running"):
            acquire_run_lock(lock, "20260720")
    finally:
        release_run_lock(lock)
    assert not lock.exists()


def test_ai_briefing_dry_run_dual_writes_status_without_legacy_video(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "briefings"
    monkeypatch.setattr(
        ai_briefing_pipeline,
        "run_cover_forge_adapter",
        lambda **_kwargs: pytest.fail("dry_run must not call Cover-Forge"),
    )
    day = _make_day(source)
    settings = Settings(
        data_dir=tmp_path / "output",
        ai_briefing_dir=source,
        mpt_dir=tmp_path / "mpt",
        pipeline_defaults_file=tmp_path / "missing.yaml",
    )
    orchestrator = Orchestrator(settings=settings, store=JobStore(settings.data_dir))
    task_id = orchestrator.submit(
        TaskInput(
            description="生成每日 AI 资讯视频",
            content_type="ai_briefing",
            params={"date": "20260720", "handoff_wait_seconds": 0, "dry_run": True},
        )
    )
    orchestrator.run(task_id)
    snapshot = orchestrator.store.get(task_id)
    assert snapshot.status == JobStatus.SUCCEEDED
    assert snapshot.artifacts.narration_script and len(snapshot.artifacts.narration_script) >= 350
    assert snapshot.artifacts.video and snapshot.artifacts.video.is_file()
    assert not (day / "video" / "briefing.mp4").exists()
    external = json.loads((day / "video_status.json").read_text(encoding="utf-8"))
    assert external["status"] == "succeeded"
    assert external["runner"] == "ai-pipeline"
    assert external["steps"]["cover_generation"] == "skipped"
    assert external["steps"]["publish_douyin"] == "skipped"
    assert snapshot.artifacts.images == []
    assert snapshot.artifacts.validation.images is None


def test_ai_briefing_llm_writer_uses_valid_script(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "briefings"
    day = _make_day(source)
    llm_script = (
        "Qwen发布新模型并开放权重，国产模型集中亮相。2026年7月20日的AI简报关注编码和工具调用，"
        "开发者生态有望进一步扩大。Kimi也在多个前端编码任务中取得领先，"
        "说明模型竞争正在从参数规模走向真实生产力。海外方面，Nvidia继续建设新一代AI工厂，CPU和GPU部署"
        "会支撑科研与企业应用。端侧小模型同步提速，多家厂商开始把AI能力推向消费级设备。监管层面，"
        "欧盟要求大型平台开放关键接口，智能体身份安全也被企业重点关注。整体看，AI竞争已经不只是单点模型"
        "能力，而是基础设施、应用生态和监管规则的综合较量。接下来，谁能把能力稳定落到产品里，谁就更可能占据主动。对观众来说，今天最值得关注的不是单个发布会的热度，而是模型能力、算力供给、端侧落地和开放规则正在同时推进。这会影响下一阶段的产品竞争，也需要持续观察实际部署效果。"
    )
    monkeypatch.setattr(
        "content_pipeline.script_generation.client.OpenAICompatibleScriptClient.complete",
        lambda _self, *, label, **_kwargs: (
            '{"mainline":"国产模型竞争","main_fact_id":"ai.summary","opening":"日期和主线","sections":["国内","海外"],"required_facts":["ai.summary","ai.focus.1","ai.focus.2"],"risk_close":null}'
            if label.endswith("plan")
            else '{"text":"国产模型集中亮相","fact_ids":["ai.summary"]}'
            if label.endswith("metadata title")
            else '{"text":"国产模型集中发布新一代开源模型，编码与工具调用成为关注重点。海外基础设施建设、端侧应用和平台开放规则也在同步推进，需要持续观察实际部署效果。","fact_ids":["ai.summary"]}'
            if label.endswith("metadata description")
            else llm_script
        ),
    )
    settings = Settings(
        data_dir=tmp_path / "output",
        ai_briefing_dir=source,
        mpt_dir=tmp_path / "mpt",
        pipeline_defaults_file=tmp_path / "missing.yaml",
    )
    orchestrator = Orchestrator(settings=settings, store=JobStore(settings.data_dir))
    task_id = orchestrator.submit(
        TaskInput(
            description="生成每日 AI 资讯视频",
            content_type="ai_briefing",
            params={"date": "20260720", "handoff_wait_seconds": 0, "dry_run": True, "script_writer": "llm"},
        )
    )
    orchestrator.run(task_id)
    snapshot = orchestrator.store.get(task_id)
    assert snapshot.status == JobStatus.SUCCEEDED
    assert snapshot.artifacts.narration_script == llm_script
    external = json.loads((day / "video_status.json").read_text(encoding="utf-8"))
    assert external["script_writer"] in {"llm", "llm_repaired"}
    assert external["script_attempts"] >= 2
    assert external["script_quality_report"]["passed"] is True
    assert snapshot.artifacts.script_plan_path and snapshot.artifacts.script_plan_path.is_file()
    assert snapshot.artifacts.script_attempts_path and snapshot.artifacts.script_attempts_path.is_file()
    assert snapshot.artifacts.script_quality_report_path and snapshot.artifacts.script_quality_report_path.is_file()
    events = orchestrator.store.read_events(task_id)
    event_names = {event["event"] for event in events}
    assert {"script_plan", "script_draft", "script_quality_checked", "script_plan_generated"} <= event_names
    assert llm_script not in json.dumps(events, ensure_ascii=False)


def test_ai_briefing_llm_writer_fails_on_invalid_script(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "briefings"
    day = _make_day(source)
    monkeypatch.setattr(
        "content_pipeline.script_generation.client.OpenAICompatibleScriptClient.complete",
        lambda _self, *, label, **_kwargs: (
            '{"mainline":"国产模型竞争","main_fact_id":"ai.summary","opening":"日期和主线","sections":["国内","海外"],"required_facts":["ai.summary","ai.focus.1","ai.focus.2"],"risk_close":null}'
            if label.endswith("plan")
            else "# 坏稿"
        ),
    )
    settings = Settings(
        data_dir=tmp_path / "output",
        ai_briefing_dir=source,
        mpt_dir=tmp_path / "mpt",
        pipeline_defaults_file=tmp_path / "missing.yaml",
    )
    orchestrator = Orchestrator(settings=settings, store=JobStore(settings.data_dir))
    task_id = orchestrator.submit(
        TaskInput(
            description="生成每日 AI 资讯视频",
            content_type="ai_briefing",
            params={
                "date": "20260720",
                "handoff_wait_seconds": 0,
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
    external = json.loads((day / "video_status.json").read_text(encoding="utf-8"))
    assert external["script_writer"] == "llm_failed"
    assert external["script_writer_error"]


def test_publish_targets_are_fixed_and_independent(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "briefings"
    _make_day(source)
    settings = Settings(
        data_dir=tmp_path / "output",
        ai_briefing_dir=source,
        mpt_dir=tmp_path / "mpt",
        pipeline_defaults_file=tmp_path / "missing.yaml",
    )

    used_titles: list[str] = []

    def fake_upload(*, target, title, **kwargs):
        used_titles.append(title)
        if target.platform == "douyin":
            return {"success": True, "visibility": "private"}
        raise RuntimeError("kuaishou failed")

    monkeypatch.setattr("content_pipeline.publishing.call_sau_target", fake_upload)
    orchestrator = Orchestrator(settings=settings, store=JobStore(settings.data_dir))
    task_id = orchestrator.submit(
        TaskInput(
            description="生成并发布每日 AI 资讯视频",
            content_type="ai_briefing",
            publish=True,
            params={"date": "20260720", "handoff_wait_seconds": 0, "dry_run": True},
        )
    )
    orchestrator.run(task_id)
    snapshot = orchestrator.store.get(task_id)
    assert snapshot.status == JobStatus.PARTIAL
    assert snapshot.artifacts.publish_results["douyin"]["success"] is True
    assert snapshot.artifacts.publish_results["kuaishou"]["success"] is False
    assert "tencent" not in snapshot.artifacts.publish_results
    assert used_titles == ["国产模型集中亮相", "国产模型集中亮相"]


def test_explicit_publish_targets_must_match_defaults(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "briefings"
    _make_day(source)
    settings = Settings(
        data_dir=tmp_path / "output",
        ai_briefing_dir=source,
        mpt_dir=tmp_path / "mpt",
        pipeline_defaults_file=tmp_path / "missing.yaml",
    )

    def fake_upload(*, target, **kwargs):
        return {"success": True, "visibility": "private"}

    monkeypatch.setattr("content_pipeline.publishing.call_sau_target", fake_upload)
    orchestrator = Orchestrator(settings=settings, store=JobStore(settings.data_dir))
    task_id = orchestrator.submit(
        TaskInput(
            description="生成并发布每日 AI 资讯视频",
            content_type="ai_briefing",
            publish=True,
            publish_targets=[
                PublishTarget(platform="douyin", account="金融破壁人"),
                PublishTarget(platform="kuaishou", account="搞AI的罗辑同学"),
            ],
            params={"date": "20260720", "handoff_wait_seconds": 0, "dry_run": True},
        )
    )
    orchestrator.run(task_id)
    snapshot = orchestrator.store.get(task_id)

    assert snapshot.status == JobStatus.SUCCEEDED
    assert set(snapshot.artifacts.publish_results) == {"douyin", "kuaishou"}
    assert snapshot.artifacts.upload_result["delivery_states"] == {
        "douyin": "private",
        "kuaishou": "private",
    }
    assert snapshot.artifacts.upload_result["visibility"] == "private"


def _mock_real_mpt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, calls: list[str]) -> None:
    video_validation = VideoValidation(
        duration_seconds=90.0,
        width=1080,
        height=1920,
        aspect_ratio=0.5625,
        video_codec="h264",
        audio_codec="aac",
        decoded_video_frames=1,
        decoded_audio_frames=1,
    )

    def fake_mpt(*, output_dir: Path, **_kwargs) -> NarratedMptResult:
        calls.append("video")
        output_dir.mkdir(parents=True, exist_ok=True)
        task_dir = tmp_path / "mpt-task"
        task_dir.mkdir(exist_ok=True)
        video = task_dir / "final.mp4"
        subtitle = task_dir / "subtitle.srt"
        manifest = task_dir / "generation_manifest.json"
        video.write_bytes(b"mock-video")
        subtitle.write_text(
            "1\n00:00:00,000 --> 00:01:30,000\n这是有效的测试字幕内容，用于验证真实简报流水线。\n", encoding="utf-8"
        )
        manifest.write_text("{}", encoding="utf-8")
        return NarratedMptResult(video=video, subtitle=subtitle, task_dir=task_dir, manifest=manifest)

    monkeypatch.setattr(ai_briefing_pipeline, "call_narrated_mpt", fake_mpt)
    monkeypatch.setattr(ai_briefing_pipeline, "validate_video", lambda *_args, **_kwargs: video_validation)


def test_ai_briefing_real_cover_uses_handoff_title_before_video(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "briefings"
    day = _make_day(source)
    calls: list[str] = []
    captured = {}

    def fake_cover(*, params, output_path: Path, **_kwargs) -> AdapterResult:
        calls.append("cover")
        captured["params"] = params
        output_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1080, 1920), color="navy").save(output_path)
        return AdapterResult(
            ok=True,
            tool="Cover-Forge",
            code=ErrorCode.OK,
            artifacts={"cover_path": str(output_path)},
        )

    monkeypatch.setattr(ai_briefing_pipeline, "run_cover_forge_adapter", fake_cover)
    _mock_real_mpt(tmp_path, monkeypatch, calls)
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "output",
        ai_briefing_dir=source,
        pipeline_defaults_file=tmp_path / "missing.yaml",
    )
    orchestrator = Orchestrator(settings=settings, store=JobStore(settings.data_dir))
    task_id = orchestrator.submit(
        TaskInput(
            description="生成每日 AI 资讯视频",
            content_type="ai_briefing",
            params={"date": "20260720", "handoff_wait_seconds": 0},
        )
    )
    orchestrator.run(task_id)
    snapshot = orchestrator.store.get(task_id)

    cover_path = orchestrator.store.job_dir(task_id) / "cover" / "cover.png"
    assert snapshot.status == JobStatus.SUCCEEDED
    assert calls == ["cover", "video"]
    assert captured["params"].title == "国产模型集中亮相"
    assert captured["params"].size == "story"
    assert captured["params"].template == "ai-poster"
    assert captured["params"].title_position == "center"
    assert snapshot.artifacts.images == [cover_path]
    assert snapshot.artifacts.validation.images is not None
    assert snapshot.artifacts.validation.images.files[0].width == 1080
    assert snapshot.artifacts.validation.images.files[0].height == 1920
    steps = [
        event["payload"]["step"]
        for event in orchestrator.store.read_events(task_id)
        if event["event"] == "step_started"
    ]
    assert steps == ["route", "source_scan", "image", "video"]
    external = json.loads((day / "video_status.json").read_text(encoding="utf-8"))
    assert external["steps"]["cover_generation"] == "ok"
    assert external["cover_path"] == str(cover_path)
    assert external["cover_sha256"] == hashlib.sha256(cover_path.read_bytes()).hexdigest()


def test_ai_briefing_cover_uses_explicit_title_and_selected_dimensions(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "briefings"
    _make_day(source)
    captured = {}
    calls: list[str] = []

    def fake_cover(*, params, output_path: Path, **_kwargs) -> AdapterResult:
        calls.append("cover")
        captured["params"] = params
        output_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1920, 1080), color="black").save(output_path)
        return AdapterResult(
            ok=True,
            tool="Cover-Forge",
            code=ErrorCode.OK,
            artifacts={"cover_path": str(output_path)},
        )

    monkeypatch.setattr(ai_briefing_pipeline, "run_cover_forge_adapter", fake_cover)
    _mock_real_mpt(tmp_path, monkeypatch, calls)
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "output",
        ai_briefing_dir=source,
        pipeline_defaults_file=tmp_path / "missing.yaml",
    )
    orchestrator = Orchestrator(settings=settings, store=JobStore(settings.data_dir))
    explicit_title = " 显式简报标题：" + "长" * 100
    task_id = orchestrator.submit(
        TaskInput(
            description="生成每日 AI 资讯视频",
            content_type="ai_briefing",
            params={
                "date": "20260720",
                "handoff_wait_seconds": 0,
                "title": explicit_title,
                "cover_size": "landscape",
                "cover_template": "default",
                "cover_title_position": "left",
            },
        )
    )
    orchestrator.run(task_id)

    assert orchestrator.store.get(task_id).status == JobStatus.SUCCEEDED
    assert captured["params"].title == "显式简报标题"
    assert captured["params"].size == "landscape"
    assert captured["params"].template == "default"
    assert captured["params"].title_position == "left"


def test_ai_briefing_video_pins_normal_voice_rate(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "briefings"
    _make_day(source)
    calls: list[str] = []
    _mock_real_mpt(tmp_path, monkeypatch, calls)

    captured: dict = {}
    inner = ai_briefing_pipeline.call_narrated_mpt

    def capturing_mpt(*args, **kwargs):
        captured.update(kwargs)
        return inner(*args, **kwargs)

    monkeypatch.setattr(ai_briefing_pipeline, "call_narrated_mpt", capturing_mpt)
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "output",
        ai_briefing_dir=source,
        pipeline_defaults_file=tmp_path / "missing.yaml",
    )
    orchestrator = Orchestrator(settings=settings, store=JobStore(settings.data_dir))
    task_id = orchestrator.submit(
        TaskInput(
            description="生成每日 AI 资讯视频",
            content_type="ai_briefing",
            params={"date": "20260720", "handoff_wait_seconds": 0},
        )
    )
    orchestrator.run(task_id)

    assert orchestrator.store.get(task_id).status == JobStatus.SUCCEEDED
    assert captured["voice_rate"] == 1.0


@pytest.mark.parametrize("failure", ["adapter", "dimensions"])
def test_ai_briefing_cover_failure_blocks_video_and_marks_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    source = tmp_path / "briefings"
    day = _make_day(source)

    def fake_cover(*, output_path: Path, **_kwargs) -> AdapterResult:
        if failure == "adapter":
            return AdapterResult(
                ok=False,
                tool="Cover-Forge",
                code=ErrorCode.TIMEOUT,
                message="sidecar timed out",
            )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (100, 100), color="red").save(output_path)
        return AdapterResult(
            ok=True,
            tool="Cover-Forge",
            code=ErrorCode.OK,
            artifacts={"cover_path": str(output_path)},
        )

    monkeypatch.setattr(ai_briefing_pipeline, "run_cover_forge_adapter", fake_cover)
    if failure == "adapter":
        monkeypatch.setattr(
            ai_briefing_pipeline,
            "generate_local_cover",
            lambda **_kwargs: AdapterResult(
                ok=False,
                tool="LocalCover",
                code=ErrorCode.EXTERNAL_TOOL_FAILED,
                message="local fallback failed",
            ),
        )
    monkeypatch.setattr(
        ai_briefing_pipeline,
        "call_narrated_mpt",
        lambda **_kwargs: pytest.fail("cover failure must block MPT"),
    )
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "output",
        ai_briefing_dir=source,
        pipeline_defaults_file=tmp_path / "missing.yaml",
    )
    orchestrator = Orchestrator(settings=settings, store=JobStore(settings.data_dir))
    task_id = orchestrator.submit(
        TaskInput(
            description="生成每日 AI 资讯视频",
            content_type="ai_briefing",
            params={"date": "20260720", "handoff_wait_seconds": 0},
        )
    )
    orchestrator.run(task_id)
    snapshot = orchestrator.store.get(task_id)

    assert snapshot.status == JobStatus.FAILED
    assert snapshot.current_step == PipelineStep.IMAGE
    assert snapshot.artifacts.video is None
    external = json.loads((day / "video_status.json").read_text(encoding="utf-8"))
    assert external["status"] == "failed"
    assert external["steps"]["cover_generation"] == "failed"
    if failure == "adapter":
        assert "cover generation failed [TIMEOUT]" in (snapshot.error or "")
    else:
        assert "do not match story 1080x1920" in (snapshot.error or "")


def test_ai_briefing_cover_falls_back_to_local_generation(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "briefings"
    day = _make_day(source)
    calls: list[str] = []

    def fake_cover(**_kwargs) -> AdapterResult:
        calls.append("cover")
        return AdapterResult(
            ok=False,
            tool="Cover-Forge",
            code=ErrorCode.EXTERNAL_TOOL_FAILED,
            message="Cover-Forge is unavailable: connection refused",
        )

    monkeypatch.setattr(ai_briefing_pipeline, "run_cover_forge_adapter", fake_cover)
    _mock_real_mpt(tmp_path, monkeypatch, calls)
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "output",
        ai_briefing_dir=source,
        pipeline_defaults_file=tmp_path / "missing.yaml",
    )
    orchestrator = Orchestrator(settings=settings, store=JobStore(settings.data_dir))
    task_id = orchestrator.submit(
        TaskInput(
            description="生成每日 AI 资讯视频",
            content_type="ai_briefing",
            params={"date": "20260720", "handoff_wait_seconds": 0},
        )
    )
    orchestrator.run(task_id)
    snapshot = orchestrator.store.get(task_id)

    cover_path = orchestrator.store.job_dir(task_id) / "cover" / "cover.png"
    assert snapshot.status == JobStatus.SUCCEEDED
    assert calls == ["cover", "video"]
    assert cover_path.is_file()
    with Image.open(cover_path) as generated:
        assert (generated.width, generated.height) == (1080, 1920)
    external = json.loads((day / "video_status.json").read_text(encoding="utf-8"))
    assert external["steps"]["cover_generation"] == "ok"
    assert external["cover_generator"] == "local"
    assert "Cover-Forge is unavailable" in external["cover_forge_error"]
