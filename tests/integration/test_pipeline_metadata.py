from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image
from test_ai_briefing_pipeline import SAMPLE_ARTICLE, _make_day, _mock_real_mpt
from test_finance_pipeline import SAMPLE_MD

from content_pipeline import ai_briefing_pipeline, finance_pipeline
from content_pipeline.job_store import JobStore
from content_pipeline.models import AdapterResult, ErrorCode, JobStatus, TaskInput
from content_pipeline.orchestrator import Orchestrator
from content_pipeline.settings import Settings

AI_TITLE = "国产模型集中亮相"
AI_DESCRIPTION = (
    "国产模型集中发布新一代开源模型，编码与工具调用成为关注重点。海外基础设施建设、"
    "端侧应用和平台开放规则也在同步推进，需要持续观察实际部署效果。"
)
FINANCE_TITLE = "上证指数回落观察"
FINANCE_DESCRIPTION = (
    "上证指数下跌1.85%，四大指数全线回落，院线逆势走强，半导体方向承压。"
    "后续仍需观察市场承接情况，注意市场波动风险，审慎决策，不构成投资建议。"
)


def setup_pipeline(tmp_path: Path, kind: str) -> tuple[Orchestrator, dict, Path]:
    source = tmp_path / "source"
    if kind == "ai_briefing":
        day = _make_day(source)
        assert (day / "article.md").read_text(encoding="utf-8") == SAMPLE_ARTICLE
        params = {"date": "20260720", "handoff_wait_seconds": 0}
    else:
        source.mkdir()
        (source / "2026-07-16.md").write_text(SAMPLE_MD, encoding="utf-8")
        day = source
        params = {"date": "2026-07-16"}
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "jobs",
        ai_briefing_dir=source,
        finance_md_dir=source,
        mpt_dir=tmp_path / "mpt",
        pipeline_defaults_file=tmp_path / "missing.yaml",
    )
    return Orchestrator(settings=settings, store=JobStore(settings.data_dir)), params, day


@pytest.mark.parametrize("kind", ["ai_briefing", "finance"])
def test_final_metadata_reaches_video_cover_publish_and_artifacts(tmp_path, monkeypatch, kind):
    orchestrator, params, day = setup_pipeline(tmp_path, kind)
    title = AI_TITLE if kind == "ai_briefing" else FINANCE_TITLE
    description = AI_DESCRIPTION if kind == "ai_briefing" else FINANCE_DESCRIPTION
    ids = ["ai.summary"] if kind == "ai_briefing" else ["finance.index.1", "finance.sector.1", "finance.sector.2"]
    calls = []
    downstream = []

    def complete(_self, *, label, **kwargs):
        calls.append(label)
        return json.dumps({"text": title if label.endswith("title") else description, "fact_ids": ids})

    monkeypatch.setattr("content_pipeline.script_generation.client.OpenAICompatibleScriptClient.complete", complete)

    def publish(**kwargs):
        downstream.append(("publish", kwargs["title"], kwargs["description"]))
        return JobStatus.SUCCEEDED, None

    module = ai_briefing_pipeline if kind == "ai_briefing" else finance_pipeline
    monkeypatch.setattr(module, "publish_generated_video", publish)
    if kind == "ai_briefing":

        def cover(*, params, output_path, **kwargs):
            downstream.append(("cover", params.title))
            output_path.parent.mkdir(parents=True)
            Image.new("RGB", (1080, 1920)).save(output_path)
            return AdapterResult(ok=True, tool="fake", code=ErrorCode.OK, artifacts={"cover_path": str(output_path)})

        monkeypatch.setattr(module, "run_cover_forge_adapter", cover)
        _mock_real_mpt(tmp_path, monkeypatch, [])
        original = module.call_narrated_mpt

        def video(**kwargs):
            downstream.append(("video", kwargs["title"]))
            return original(**kwargs)

        monkeypatch.setattr(module, "call_narrated_mpt", video)
    else:
        original = module.call_finance_mpt

        def video(**kwargs):
            downstream.append(("video", kwargs["title"]))
            return original(**kwargs)

        monkeypatch.setattr(module, "call_finance_mpt", video)
        params["dry_run"] = True
    params.update(title_writer="llm", description_writer="llm")
    task_id = orchestrator.submit(
        TaskInput(description="元数据集成验证", content_type=kind, publish=False, params=params)
    )
    orchestrator.run(task_id)
    snapshot = orchestrator.store.get(task_id)
    assert snapshot.status == JobStatus.SUCCEEDED, snapshot.error
    assert calls == [f"{kind} metadata title", f"{kind} metadata description"]
    assert ("video", title) in downstream
    assert ("publish", title, description) in downstream
    assert snapshot.artifacts.final_title == title
    assert snapshot.artifacts.final_description == description
    assert snapshot.artifacts.metadata_audit_path.is_file()
    assert snapshot.artifacts.metadata_audit.status == "succeeded"
    assert snapshot.artifacts.metadata_audit.title.quality_passed is True
    persisted = json.loads(orchestrator.store.status_path(task_id).read_text(encoding="utf-8"))
    assert {"facts", "attempts", "prompt_sha256", "final_text"}.isdisjoint(json.dumps(persisted))
    if kind == "ai_briefing":
        assert ("cover", title) in downstream
        external = json.loads((day / "video_status.json").read_text(encoding="utf-8"))
        assert external["title"] == title
        assert external["steps"]["metadata_generation"] == "ok"


@pytest.mark.parametrize("kind", ["ai_briefing", "finance"])
@pytest.mark.parametrize("failed_stage", ["title", "description"])
def test_metadata_failure_persists_audit_and_blocks_all_media(tmp_path, monkeypatch, kind, failed_stage):
    orchestrator, params, day = setup_pipeline(tmp_path, kind)
    calls = []

    def complete(_self, *, label, **kwargs):
        calls.append(label)
        if label.endswith("title") and failed_stage == "description":
            title = AI_TITLE if kind == "ai_briefing" else FINANCE_TITLE
            ids = ["ai.summary"] if kind == "ai_briefing" else ["finance.index.1"]
            return json.dumps({"text": title, "fact_ids": ids})
        return "not valid JSON"

    monkeypatch.setattr("content_pipeline.script_generation.client.OpenAICompatibleScriptClient.complete", complete)
    for module, name in (
        (ai_briefing_pipeline, "run_cover_forge_adapter"),
        (ai_briefing_pipeline, "call_narrated_mpt"),
        (finance_pipeline, "call_finance_mpt"),
        (ai_briefing_pipeline, "publish_generated_video"),
        (finance_pipeline, "publish_generated_video"),
    ):
        monkeypatch.setattr(module, name, lambda **kwargs: pytest.fail("metadata failure must block downstream"))
    params.update(title_writer="llm", description_writer="llm", metadata_repair_attempts=1)
    task_id = orchestrator.submit(
        TaskInput(description="元数据失败验证", content_type=kind, publish=False, params=params)
    )
    orchestrator.run(task_id)
    snapshot = orchestrator.store.get(task_id)
    assert snapshot.status == JobStatus.FAILED
    assert snapshot.artifacts.video is None
    assert snapshot.artifacts.narration_script
    audit = json.loads(snapshot.artifacts.metadata_audit_path.read_text(encoding="utf-8"))
    assert audit["status"] == "failed"
    assert audit[failed_stage]["status"] == "failed"
    assert snapshot.artifacts.metadata_audit.status == "failed"
    assert getattr(snapshot.artifacts.metadata_audit, failed_stage).error_codes == ["invalid_json_contract"]
    persisted = json.loads(orchestrator.store.status_path(task_id).read_text(encoding="utf-8"))
    assert {"facts", "attempts", "prompt_sha256", "final_text"}.isdisjoint(json.dumps(persisted))
    assert len(audit[failed_stage]["attempts"]) == 2
    assert len(calls) == (2 if failed_stage == "title" else 3)
    if kind == "ai_briefing":
        external = json.loads((day / "video_status.json").read_text(encoding="utf-8"))
        assert external["steps"]["metadata_generation"] == "failed"


@pytest.mark.parametrize("kind", ["ai_briefing", "finance"])
@pytest.mark.parametrize("fallback", [False, True])
def test_rule_metadata_preserves_legacy_title_before_downstream_adaptation(tmp_path, monkeypatch, kind, fallback):
    orchestrator, params, day = setup_pipeline(tmp_path, kind)
    if kind == "ai_briefing":
        expected = "国产模型集中亮相：编码与工具调用成为竞争焦点，端侧应用继续提速"
        handoff_path = day / "video_handoff.json"
        handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
        handoff["title"] = expected
        handoff_path.write_text(json.dumps(handoff, ensure_ascii=False), encoding="utf-8")
    else:
        expected = "每日基金日报 · 2026-07-16"
    captured = []
    module = ai_briefing_pipeline if kind == "ai_briefing" else finance_pipeline
    method = "call_narrated_mpt" if kind == "ai_briefing" else "call_finance_mpt"
    original = getattr(module, method)

    def video(**kwargs):
        captured.append(kwargs["title"])
        return original(**kwargs)

    monkeypatch.setattr(module, method, video)
    if fallback:
        monkeypatch.setattr(
            "content_pipeline.script_generation.client.OpenAICompatibleScriptClient.complete",
            lambda *_args, **_kwargs: "invalid JSON",
        )
        params.update(title_writer="llm", metadata_failure_policy="validated_rule_fallback", metadata_repair_attempts=0)
    params["dry_run"] = True
    task_id = orchestrator.submit(
        TaskInput(description="规则兼容验证", content_type=kind, publish=False, params=params)
    )
    orchestrator.run(task_id)
    snapshot = orchestrator.store.get(task_id)
    assert snapshot.status == JobStatus.SUCCEEDED, snapshot.error
    assert snapshot.artifacts.final_title == expected
    assert snapshot.artifacts.metadata_audit.title.generation_mode == (
        "validated_rule_fallback" if fallback else "rule"
    )
    assert captured == [ai_briefing_pipeline.build_video_title(expected) if kind == "ai_briefing" else expected]
