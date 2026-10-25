import json
import subprocess
from importlib.resources import files
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from content_pipeline.api.app import create_app, task_fingerprint
from content_pipeline.models import JobStatus, PipelineStep, TaskInput
from content_pipeline.settings import Settings

ADMIN_TOKEN = "admin-" + "a" * 32
API_TOKEN = "api-" + "b" * 32
SESSION_SECRET = "session-" + "c" * 32


def _settings(tmp_path: Path, **overrides) -> Settings:
    profiles = tmp_path / "profiles"
    profiles.mkdir(parents=True, exist_ok=True)
    values = {
        "data_dir": tmp_path / "sensitive-output-path",
        "profiles_dir": profiles,
        "web_allowed_hosts": "testserver",
        "web_allowed_origins": "http://testserver",
        "web_auth_required": True,
        "web_admin_token": ADMIN_TOKEN,
        "web_api_token": API_TOKEN,
        "web_session_secret": SESSION_SECRET,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _client(tmp_path: Path, **overrides) -> TestClient:
    return TestClient(
        create_app(_settings(tmp_path, **overrides)),
        base_url="http://testserver",
        client=("127.0.0.1", 50000),
    )


def _bearer() -> dict[str, str]:
    return {"Authorization": f"Bearer {API_TOKEN}"}


def test_console_shell_and_static_assets_are_public_but_data_is_protected(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        page = client.get("/")
        favicon = client.get("/static/favicon.svg")
        css = client.get("/static/styles.css")
        javascript = client.get("/static/app.js")
        content_javascript = client.get("/static/content-studio.js")
        scheduler_javascript = client.get("/static/scheduler.js")
        bootstrap = client.get("/ui/bootstrap")

    assert page.status_code == 200
    assert favicon.status_code == 200
    assert favicon.headers["content-type"].startswith("image/svg+xml")
    assert "AI Pipeline · 内容控制台" in page.text
    assert '<link rel="icon" href="/static/favicon.svg" type="image/svg+xml">' in page.text
    assert 'id="login-view"' in page.text
    assert 'id="app-shell"' in page.text
    assert 'id="template-cards"' in page.text
    assert "选择内容类型" in page.text
    assert "让内容从灵感到成片" in page.text
    assert 'class="hero-orbit"' in page.text
    assert 'class="stat-icon"' in page.text
    assert "生成金融口播稿" not in page.text
    assert '<script src="/static/app.js" defer></script>' in page.text
    assert '<script src="/static/scheduler.js" defer></script>' in page.text
    assert 'class="modal settings-modal"' in page.text
    assert 'id="settings-tabs"' in page.text
    assert 'role="tablist"' in page.text
    assert page.text.count('role="tab"') == 4
    assert 'aria-controls="settings-panel-publish"' in page.text
    assert 'aria-controls="settings-panel-maintenance"' in page.text
    assert 'id="scan-cache-button"' in page.text
    assert 'id="clean-cache-button"' in page.text
    assert 'id="cache-cleanup-rows"' in page.text
    assert 'id="publish-policy-content-nav"' in page.text
    assert 'id="publish-policy-summary"' in page.text
    assert 'id="account-status-source"' in page.text
    assert 'id="refresh-account-status"' in page.text
    assert page.text.count('class="button ghost compact theme-toggle"') == 1
    assert 'class="icon-button theme-toggle auth-theme-toggle"' in page.text
    assert 'id="content-voice-rate"' in page.text
    assert "朗读速度" in page.text
    assert css.status_code == 200
    assert "--surface" in css.text
    assert "--brand-gradient" in css.text
    assert "--workspace-panel-height" in css.text
    assert "height: var(--workspace-panel-height)" in css.text
    assert ".field-label { display: inline-flex; align-items: baseline; gap: 0.2rem; }" in css.text
    assert (
        ".jobs-panel, .detail-panel, .draft-library, .studio-editor-panel { height: auto; max-height: none; }"
        in css.text
    )
    assert "#studio-editor:not([hidden])" in css.text
    assert "studio-action-controls" in css.text
    assert 'html[data-theme="light"]' in css.text
    assert "@media (prefers-contrast: more)" in css.text
    assert "@media (prefers-reduced-motion: reduce)" in css.text
    assert "@media (forced-colors: active)" in css.text
    assert ".settings-workspace" in css.text
    assert ".settings-modal .switch-row input:checked { background: var(--primary-cyan); }" in css.text
    assert ".switch-row input:checked { background: var(--red); }" in css.text
    assert ".policy-content-nav" in css.text
    assert ".defaults-card" in css.text
    assert ".account-card" in css.text
    assert ".cache-cleanup-row" in css.text
    assert "grid-template-rows: auto minmax(0, 1fr)" in css.text
    assert javascript.status_code == 200
    assert '<span class="field-label">${escapeHtml(field.label)}${requiredLabel}</span>' in javascript.text
    assert '<span class="field-label">视频标题<span class="required">*</span></span>' in page.text
    assert "localStorage.setItem(THEME_STORAGE_KEY, resolved)" in javascript.text
    assert 'FORM_MEMORY_STORAGE_KEY = "ai-pipeline-form-memory-v1"' in javascript.text
    assert "function rememberCurrentTaskForm()" in javascript.text
    assert "rememberedField(taskParamScope(contentType), field.name)" in javascript.text
    assert "publish_settings" in javascript.text
    assert 'response.status === 401 && errorCode === "authentication_required"' in javascript.text
    assert 'payload.detail?.code === "recent_authentication_required"' in javascript.text
    assert "发布前需要重新验证管理员令牌，请重新登录。" in javascript.text
    assert "请退出后重新登录" not in javascript.text
    assert 'apiRequest("/ui/bootstrap")' in javascript.text
    assert "ready.pipeline_defaults_available" in javascript.text
    assert "默认参数" in javascript.text
    assert content_javascript.status_code == 200
    assert 'studio.request("/content/bootstrap")' in content_javascript.text
    assert "function renderTemplateCards()" in content_javascript.text
    assert "function templateIcon(templateId)" in content_javascript.text
    assert "data-template-id" in content_javascript.text
    assert "创建 AI 简报口播初稿" in content_javascript.text
    assert "studio.request(`/content/drafts/${draft.draft_id}/video`" in content_javascript.text
    assert 'STUDIO_FORM_MEMORY_STORAGE_KEY = "ai-pipeline-studio-form-memory-v1"' in content_javascript.text
    assert "function rememberedVoiceRate()" in content_javascript.text
    assert "function parseVoiceRate()" in content_javascript.text
    assert "rememberVoiceRate(voiceRate)" in content_javascript.text
    assert "voice_rate: voiceRate" in content_javascript.text
    assert "正在拉取资料并生成视频" in content_javascript.text
    assert "超出 ${lengthLabel} 字建议范围，仍可生成和发布" in content_javascript.text
    assert 'byId("generate-content-video").disabled = !(disclaimerOkay && complete);' in content_javascript.text
    assert "lengthOkay && disclaimerOkay && complete" not in content_javascript.text
    assert ".script-counter.warning" in css.text
    assert ".script-check.warning" in css.text
    assert "var(--warning-text)" in css.text
    assert 'studio.request(`/content/drafts/${draftId}`, { method: "DELETE" })' in content_javascript.text
    assert (
        'studio.request("/content/drafts/batch-delete", { method: "POST", body: { draft_ids: draftIds } })'
        in content_javascript.text
    )
    assert "studio-publish-toggle" not in page.text
    assert "handleStudioPublishToggle" not in content_javascript.text
    assert 'id="job-publish-dialog"' in page.text
    assert 'id="delete-selected-drafts"' in page.text
    assert 'id="select-all-jobs"' in page.text
    assert 'id="delete-selected-jobs"' in page.text
    assert 'apiRequest("/jobs?limit=100")' in javascript.text
    assert 'apiRequest(`/jobs/${taskId}`, { method: "DELETE" })' in javascript.text
    assert 'apiRequest("/jobs/batch-delete", { method: "POST", body: { task_ids: taskIds } })' in javascript.text
    assert 'apiRequest(`/jobs/${taskId}`, { method: "PATCH", body: { display_name: displayName } })' in javascript.text
    assert 'id="rename-job-dialog"' in page.text
    assert "apiRequest(`/jobs/${taskId}/events?limit=100`)" in javascript.text
    assert "apiRequest(`/jobs/${taskId}/artifacts`)" in javascript.text
    assert "apiRequest(`/jobs/${taskId}/publish-readiness`)" in javascript.text
    assert "submitDeferredPublication" in javascript.text
    assert "function publicationStatusBadge(summary)" in javascript.text
    assert "function renderPublicationOverview(summary)" in javascript.text
    assert 'class="publication-summary-card state-${escapeHtml(summary.state)}"' in javascript.text
    assert "<th>生成状态</th><th>发布状态</th>" in page.text
    assert '<progress class="progress-bar" role="progressbar"' in javascript.text
    assert "function renderJobProgress(snapshot, contentType)" in javascript.text
    assert "function renderValidationSummary(validation)" in javascript.text
    assert "function compactEvents(events)" in javascript.text
    assert 'elements["publish-confirm-input"].value.trim() !== "确认发布"' in javascript.text
    assert 'elements["job-publish-confirm-input"].value.trim() !== "确认发布"' in javascript.text
    assert scheduler_javascript.status_code == 200
    assert "function activateSettingsTab(name, focus = false)" in scheduler_javascript.text
    assert '["ArrowRight", "ArrowDown"]' in scheduler_javascript.text
    assert "function updatePublishPolicySummary()" in scheduler_javascript.text
    assert "function validateDefaultsRow(row)" in scheduler_javascript.text
    assert 'activateSettingsTab("defaults")' in scheduler_javascript.text
    assert "function accountState(item, source)" in scheduler_javascript.text
    assert 'if (source !== "bridge")' in scheduler_javascript.text
    assert "function refreshAccountStatus()" in scheduler_javascript.text
    assert "function renderCacheCleanup(data)" in scheduler_javascript.text
    assert "async function scanCacheFiles()" in scheduler_javascript.text
    assert "async function cleanCacheFiles()" in scheduler_javascript.text
    assert (
        'scheduler.request("/maintenance/cache-cleanup", { method: "POST", body: { apply: false } })'
        in scheduler_javascript.text
    )
    assert (
        'scheduler.request("/maintenance/cache-cleanup", { method: "POST", body: { apply: true } })'
        in scheduler_javascript.text
    )
    assert 'submit.textContent = "保存中…"' in scheduler_javascript.text
    assert bootstrap.status_code == 401
    assert "default-src 'self'" in page.headers["content-security-policy"]
    assert "script-src 'self'" in page.headers["content-security-policy"]


def test_packaged_web_assets_are_available() -> None:
    static = files("content_pipeline.web").joinpath("static")

    assert static.joinpath("index.html").is_file()
    assert static.joinpath("styles.css").is_file()
    assert static.joinpath("app.js").is_file()
    assert static.joinpath("content-studio.js").is_file()
    assert static.joinpath("scheduler.js").is_file()


def test_job_status_returns_persisted_safe_progress(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    task_id = app.state.orchestrator.submit(
        TaskInput(
            description="口播进度",
            content_type="script_video",
            params={"title": "资讯", "script": "市场信息。" * 80},
        )
    )
    app.state.orchestrator.store.set_progress(
        task_id,
        percent=45,
        phase="获取视频素材",
        message="MoneyPrinterTurbo 正在获取视频素材",
        is_estimate=True,
    )
    with TestClient(app, base_url="http://testserver", client=("127.0.0.1", 50000)) as client:
        response = client.get(f"/status/{task_id}", headers=_bearer())
        events = client.get(f"/jobs/{task_id}/events", headers=_bearer())

    assert response.status_code == 200
    assert response.json()["progress"] == {
        "percent": 45,
        "phase": "获取视频素材",
        "message": "MoneyPrinterTurbo 正在获取视频素材",
        "updated_at": response.json()["progress"]["updated_at"],
        "is_estimate": True,
    }
    progress_events = [event for event in events.json()["events"] if event["event"] == "progress_updated"]
    assert progress_events[-1]["payload"]["percent"] == 45


def test_ui_bootstrap_describes_supported_pipelines_without_sensitive_settings(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.get("/ui/bootstrap", headers=_bearer())

    assert response.status_code == 200
    payload = response.json()
    assert payload["api_contract_version"] == 2
    assert payload["features"] == {
        "content_draft_crud": True,
        "content_draft_bulk_delete": True,
        "job_rename": True,
        "job_delete": True,
        "job_bulk_delete": True,
        "task_schedules": True,
    }
    pipelines = {pipeline["content_type"]: pipeline for pipeline in payload["pipelines"]}
    assert set(pipelines) == {
        "anime",
        "finance",
        "ai_briefing",
        "ai_art",
        "xhs_image_note",
        "grouped_anime",
        "japanese",
        "script_video",
    }
    assert {field["name"] for field in pipelines["script_video"]["fields"]} >= {
        "title",
        "script",
        "dry_run",
        "voice_rate",
    }
    script_field = next(field for field in pipelines["script_video"]["fields"] if field["name"] == "script")
    assert script_field["help"] == "建议 300–600 字；超出建议范围仍可生成和发布，金融内容应包含必要的风险提示。"
    assert {field["name"] for field in pipelines["finance"]["fields"]} >= {
        "date",
        "dry_run",
        "script_writer",
    }
    assert {field["name"] for field in pipelines["ai_briefing"]["fields"]} >= {
        "date",
        "dry_run",
        "script_writer",
        "cover_size",
        "cover_template",
        "cover_title_position",
        "cover_background_image",
        "cover_background_scale",
        "cover_background_position_x",
        "cover_background_position_y",
    }
    voice_rate_field = next(field for field in pipelines["script_video"]["fields"] if field["name"] == "voice_rate")
    assert voice_rate_field["minimum"] == 0.55
    assert voice_rate_field["maximum"] == 1.2
    ai_briefing_pipeline = pipelines["ai_briefing"]
    cover_fields = {field["name"]: field for field in ai_briefing_pipeline["fields"]}
    assert cover_fields["cover_size"]["default"] == "story"
    assert {option["value"] for option in cover_fields["cover_size"]["options"]} == {
        "landscape",
        "portrait",
        "story",
    }
    assert cover_fields["cover_template"]["default"] == "ai-poster"
    assert cover_fields["cover_title_position"]["default"] == "center"
    assert cover_fields["cover_background_scale"]["minimum"] == 0.25
    assert cover_fields["cover_background_scale"]["maximum"] == 3.0
    assert "封面、视频和发布标题" in cover_fields["title"]["help"]
    assert ai_briefing_pipeline["external_tools"] == ["Cover-Forge", "MoneyPrinterTurbo"]
    assert {field["name"] for field in pipelines["ai_art"]["fields"]} >= {
        "source_dir",
        "process_name",
        "title",
        "group_size",
    }
    ai_art_fields = {field["name"]: field for field in pipelines["ai_art"]["fields"]}
    ai_art_process = ai_art_fields["process_name"]
    assert ai_art_process["type"] == "select"
    assert {option["value"] for option in ai_art_process["options"]} >= {"动漫图像比例更改", "日语视觉化"}
    for _content_type, pipeline in pipelines.items():
        assert set(pipeline["task_defaults"]) == {"description", "topic"}
        assert pipeline["task_defaults"]["description"]
        assert pipeline["task_defaults"]["topic"]
    xhs_pipeline = pipelines["xhs_image_note"]
    assert xhs_pipeline["task_defaults"] == {
        "description": "优化小红书 3:4 图片",
        "topic": "小红书图片优化",
    }
    assert xhs_pipeline["publish_targets"] == []
    assert xhs_pipeline["external_tools"] == ["Photo-Process"]
    xhs_fields = {field["name"]: field for field in xhs_pipeline["fields"]}
    assert set(xhs_fields) >= {"source_dir", "process_name"}
    assert {"title", "note", "tags", "schedule", "debug", "headed", "dry_run"}.isdisjoint(xhs_fields)
    xhs_process = xhs_fields["process_name"]
    assert xhs_process["required"] is True
    assert xhs_process["type"] == "select"
    assert {option["value"] for option in xhs_process["options"]} >= {"动漫图像比例更改", "日语视觉化"}
    japanese_fields = {field["name"]: field for field in pipelines["japanese"]["fields"]}
    assert japanese_fields["process_name"]["type"] == "select"
    assert {option["value"] for option in japanese_fields["process_name"]["options"]} >= {
        "动漫图像比例更改",
        "日语视觉化",
    }
    assert payload["auth_required"] is True
    assert payload["publish_enabled"] is False
    serialized = response.text
    assert ADMIN_TOKEN not in serialized
    assert API_TOKEN not in serialized
    assert SESSION_SECRET not in serialized
    assert "sensitive-output-path" not in serialized


def test_ui_bootstrap_exposes_metadata_controls_only_for_briefing_and_finance(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.get("/ui/bootstrap", headers=_bearer())

    assert response.status_code == 200
    metadata_names = {
        "title_writer",
        "description_writer",
        "metadata_failure_policy",
        "metadata_repair_attempts",
        "quality_gate",
    }
    for pipeline in response.json()["pipelines"]:
        fields = {field["name"]: field for field in pipeline["fields"]}
        if pipeline["content_type"] not in {"finance", "ai_briefing"}:
            assert metadata_names.isdisjoint(fields)
            continue
        for name in ("title_writer", "description_writer"):
            assert fields[name]["type"] == "select"
            assert fields[name]["default"] == "follow_script"
            assert {option["value"] for option in fields[name]["options"]} == {"follow_script", "rule", "llm"}
        assert fields["metadata_failure_policy"]["default"] == "fail"
        assert {option["value"] for option in fields["metadata_failure_policy"]["options"]} == {
            "fail",
            "validated_rule_fallback",
        }
        assert fields["metadata_repair_attempts"]["default"] == 1
        assert fields["metadata_repair_attempts"]["minimum"] == 0
        assert fields["metadata_repair_attempts"]["maximum"] == 2
        assert fields["quality_gate"]["type"] == "select"
        assert fields["quality_gate"]["default"] == "soft"
        assert {option["value"] for option in fields["quality_gate"]["options"]} == {"soft", "strict"}
        assert fields["description"]["default"] == ""
        assert "手填非空" in fields["title"]["help"]
        assert "跳过" in fields["title"]["help"]
        assert "手填非空" in fields["description"]["help"]
        assert "跳过" in fields["description"]["help"]
        assert "仍会真实调用大模型" in fields["dry_run"]["help"]


@pytest.mark.parametrize("content_type", ["finance", "ai_briefing"])
def test_metadata_ui_round_trip_respects_defaults_and_manual_overrides(tmp_path: Path, content_type: str) -> None:
    defaults = tmp_path / "pipeline.defaults.yaml"
    defaults.write_text(
        f"""
enabled: true
pipelines:
  {content_type}:
    params:
      script_writer: llm
      title_writer: rule
      description_writer: llm
      metadata_failure_policy: validated_rule_fallback
      metadata_repair_attempts: 2
      description: ""
""",
        encoding="utf-8",
    )
    with _client(tmp_path, pipeline_defaults_file=defaults) as client:
        bootstrap = client.get("/ui/bootstrap", headers=_bearer())
        pipeline = next(item for item in bootstrap.json()["pipelines"] if item["content_type"] == content_type)
        fields = {field["name"]: field for field in pipeline["fields"]}
        params = {field["name"]: field["default"] for field in pipeline["fields"] if field["default"] is not None}
        params.update(title="人工标题", description="人工发布简介")
        response = client.post(
            "/validate-task",
            headers=_bearer(),
            json={"description": "元数据表单预检", "content_type": content_type, "publish": False, "params": params},
        )
        jobs = client.get("/jobs", headers=_bearer())

    assert fields["title_writer"]["default"] == "rule"
    assert fields["description_writer"]["default"] == "llm"
    assert fields["metadata_failure_policy"]["default"] == "validated_rule_fallback"
    assert fields["metadata_repair_attempts"]["default"] == 2
    assert fields["description"]["default"] == ""
    assert response.status_code == 200, response.text
    normalized = response.json()["task"]["params"]
    for name in ("title_writer", "description_writer", "metadata_failure_policy", "metadata_repair_attempts"):
        assert normalized[name] == fields[name]["default"]
    assert normalized["title"] == "人工标题"
    assert normalized["description"] == "人工发布简介"
    assert any("仍会真实调用大模型" in warning for warning in response.json()["warnings"])
    assert "安全干运行不会调用真实生成或发布工具。" not in response.json()["warnings"]
    assert jobs.json() == {"jobs": []}


@pytest.mark.parametrize("content_type", ["finance", "ai_briefing"])
def test_metadata_preflight_expands_automatic_defaults(tmp_path: Path, content_type: str) -> None:
    with _client(tmp_path) as client:
        response = client.post(
            "/validate-task",
            headers=_bearer(),
            json={
                "description": "自动元数据预检",
                "content_type": content_type,
                "publish": False,
                "params": {"script_writer": "llm", "dry_run": True},
            },
        )

    assert response.status_code == 200, response.text
    params = response.json()["task"]["params"]
    assert params["title_writer"] == "follow_script"
    assert params["description_writer"] == "follow_script"
    assert params["metadata_failure_policy"] == "fail"
    assert params["metadata_repair_attempts"] == 1
    assert params["description"] == ""
    assert any("仍会真实调用大模型" in warning for warning in response.json()["warnings"])


def test_ui_bootstrap_applies_enabled_ai_art_pipeline_defaults(tmp_path: Path) -> None:
    defaults = tmp_path / "pipeline.defaults.yaml"
    defaults.write_text(
        """
enabled: true
pipelines:
  ai_art:
    params:
      source_dir: G:/Job/Photo-Datasets/input
      archive_dir: G:/Job/Photo-Datasets/done
      failed_dir: G:/Job/Photo-Datasets/failed
      process_name: 动漫图像比例更改
      title: AI绘画作品集
      description: AI图像处理作品集
      tags: [AI绘画, 作品集]
      group_size: 4
  xhs_image_note:
    params:
      source_dir: G:/Job/Photo-Datasets/input
      archive_dir: G:/Job/Photo-Datasets/done
      failed_dir: G:/Job/Photo-Datasets/failed
      process_name: 动漫图像比例更改
""",
        encoding="utf-8",
    )

    with _client(tmp_path, pipeline_defaults_file=defaults) as client:
        response = client.get("/ui/bootstrap", headers=_bearer())

    assert response.status_code == 200
    pipelines = {pipeline["content_type"]: pipeline for pipeline in response.json()["pipelines"]}
    ai_art_fields = {field["name"]: field for field in pipelines["ai_art"]["fields"]}
    assert ai_art_fields["source_dir"]["default"] == "G:/Job/Photo-Datasets/input"
    assert ai_art_fields["archive_dir"]["default"] == "G:/Job/Photo-Datasets/done"
    assert ai_art_fields["failed_dir"]["default"] == "G:/Job/Photo-Datasets/failed"
    assert ai_art_fields["process_name"]["default"] == "动漫图像比例更改"
    assert ai_art_fields["process_name"]["type"] == "select"
    assert "动漫图像比例更改" in {option["value"] for option in ai_art_fields["process_name"]["options"]}
    assert ai_art_fields["title"]["default"] == "AI绘画作品集"
    assert ai_art_fields["description"]["default"] == "AI图像处理作品集"
    assert ai_art_fields["tags"]["default"] == ["AI绘画", "作品集"]
    assert ai_art_fields["group_size"]["default"] == 4
    assert ai_art_fields["process_name"]["required"] is True
    xhs_fields = {field["name"]: field for field in pipelines["xhs_image_note"]["fields"]}
    assert xhs_fields["source_dir"]["default"] == "G:/Job/Photo-Datasets/input"
    assert xhs_fields["archive_dir"]["default"] == "G:/Job/Photo-Datasets/done"
    assert xhs_fields["failed_dir"]["default"] == "G:/Job/Photo-Datasets/failed"
    assert xhs_fields["process_name"]["default"] == "动漫图像比例更改"
    assert xhs_fields["process_name"]["type"] == "select"
    assert "动漫图像比例更改" in {option["value"] for option in xhs_fields["process_name"]["options"]}
    assert {"title", "note", "tags", "dry_run"}.isdisjoint(xhs_fields)
    assert xhs_fields["process_name"]["required"] is True


def test_ui_bootstrap_discovers_photo_process_options_and_keeps_configured_default(tmp_path: Path, monkeypatch) -> None:
    defaults = tmp_path / "pipeline.defaults.yaml"
    defaults.write_text(
        """
enabled: true
pipelines:
  ai_art:
    params:
      process_name: 配置里的旧名称
""",
        encoding="utf-8",
    )

    def fake_run_command(command, *, cwd, timeout, env):
        assert command[-2:] == ["list-processes", "--json"]
        assert cwd == tmp_path / "photo-process"
        assert timeout == 30
        assert env == {"PYTHONIOENCODING": "utf-8"}
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "success": True,
                    "processes": [
                        {"name": "新图文流程", "description": "内部细节不暴露给 AI Pipeline"},
                        {"name": "日语视觉化"},
                    ],
                }
            ),
            stderr="",
        )

    monkeypatch.setattr("content_pipeline.api.ui_schema.run_command", fake_run_command)

    with _client(
        tmp_path,
        pipeline_defaults_file=defaults,
        photo_process_dir=tmp_path / "photo-process",
        photo_process_python=tmp_path / "python.exe",
    ) as client:
        response = client.get("/ui/bootstrap", headers=_bearer())

    assert response.status_code == 200
    pipelines = {pipeline["content_type"]: pipeline for pipeline in response.json()["pipelines"]}
    ai_art_fields = {field["name"]: field for field in pipelines["ai_art"]["fields"]}
    options = ai_art_fields["process_name"]["options"]
    assert ai_art_fields["process_name"]["type"] == "select"
    assert ai_art_fields["process_name"]["default"] == "配置里的旧名称"
    assert {option["value"] for option in options} == {"配置里的旧名称", "新图文流程", "日语视觉化"}
    assert "内部细节" not in response.text


def test_console_session_can_preflight_with_csrf(tmp_path: Path) -> None:
    task = {
        "description": "会话预检",
        "content_type": "anime",
        "publish": False,
        "params": {"script": "第一幕：会话安全测试。", "dry_run": True},
    }
    with _client(tmp_path) as client:
        login = client.post("/auth/login", json={"token": ADMIN_TOKEN})
        csrf = login.json()["csrf_token"]
        response = client.post("/validate-task", headers={"X-CSRF-Token": csrf}, json=task)

    assert response.status_code == 200
    assert response.json()["valid"] is True


def test_job_delete_api_removes_terminal_jobs_and_blocks_running_jobs(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    terminal = app.state.orchestrator.submit(TaskInput(description="可删除任务"))
    second = app.state.orchestrator.submit(TaskInput(description="批量删除任务"))
    running = app.state.orchestrator.submit(TaskInput(description="运行中任务"))
    app.state.orchestrator.store.finish(terminal, JobStatus.SUCCEEDED)
    app.state.orchestrator.store.finish(second, JobStatus.FAILED, "failed for test")
    app.state.orchestrator.store.mark_running(running, PipelineStep.ROUTE)

    with TestClient(app, base_url="http://testserver", client=("127.0.0.1", 50000)) as client:
        renamed = client.patch(f"/jobs/{second}", headers=_bearer(), json={"display_name": "新的显示名称"})
        read_back = client.get(f"/jobs/{second}", headers=_bearer())
        deleted = client.delete(f"/jobs/{terminal}", headers=_bearer())
        missing = client.get(f"/status/{terminal}", headers=_bearer())
        blocked = client.delete(f"/jobs/{running}", headers=_bearer())
        batch = client.post(
            "/jobs/batch-delete",
            headers=_bearer(),
            json={"task_ids": [" " + second + " ", second, running, terminal]},
        )
        listed = client.get("/jobs", headers=_bearer()).json()["jobs"]

    assert renamed.status_code == 200
    assert renamed.json()["display_name"] == "新的显示名称"
    assert renamed.json()["task"]["description"] == "批量删除任务"
    assert read_back.json()["display_name"] == "新的显示名称"
    assert deleted.status_code == 200
    assert deleted.json() == {"task_id": terminal, "deleted": True}
    assert missing.status_code == 404
    assert blocked.status_code == 409
    assert batch.status_code == 200
    assert batch.json()["deleted"] == [second]
    assert batch.json()["blocked"] == [running]
    assert batch.json()["not_found"] == [terminal]
    assert {job["task_id"] for job in listed} == {running}


def test_job_batch_delete_rejects_extra_fields_and_invalid_ids(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        extra = client.post(
            "/jobs/batch-delete",
            headers=_bearer(),
            json={"task_ids": ["0" * 32], "force": True},
        )
        invalid_id = client.post(
            "/jobs/batch-delete",
            headers=_bearer(),
            json={"task_ids": ["not-a-task-id"]},
        )

    assert extra.status_code == 422
    assert invalid_id.status_code == 422


def test_task_preflight_normalizes_params_and_returns_bound_fingerprint(tmp_path: Path) -> None:
    task = TaskInput(
        description="动漫控制台干运行",
        content_type="anime",
        topic="测试主题",
        params={"script": "第一幕：安全测试。", "dry_run": True},
    )
    with _client(tmp_path) as client:
        response = client.post("/validate-task", headers=_bearer(), json=task.model_dump(mode="json"))
        jobs = client.get("/jobs", headers=_bearer())

    assert response.status_code == 200
    assert jobs.json() == {"jobs": []}
    payload = response.json()
    normalized = TaskInput.model_validate(payload["task"])
    assert payload["valid"] is True
    assert normalized.params["dry_run"] is True
    assert payload["task_fingerprint"] == task_fingerprint(normalized)
    assert any("安全干运行" in warning for warning in payload["warnings"])


@pytest.mark.parametrize("script", ["短稿", "长" * 601])
def test_script_video_preflight_warns_but_remains_valid_outside_recommended_length(tmp_path: Path, script: str) -> None:
    task = {
        "description": "口播稿建议字数预检",
        "content_type": "script_video",
        "publish": False,
        "params": {"title": "今日资讯", "script": script, "dry_run": False},
    }

    with _client(tmp_path) as client:
        response = client.post("/validate-task", headers=_bearer(), json=task)

    assert response.status_code == 200
    payload = response.json()
    assert payload["valid"] is True
    assert payload["task"]["params"]["script"] == script
    assert any(
        f"当前为 {len(script)} 字" in warning and "超出建议的 300–600 字范围，仍可生成和发布" in warning
        for warning in payload["warnings"]
    )


@pytest.mark.parametrize(
    "task_path",
    sorted(path for tier in ("dry-run", "local", "publish") for path in Path("examples/tasks", tier).glob("*.json")),
    ids=lambda path: path.as_posix(),
)
def test_task_preflight_accepts_current_packaged_examples(task_path: Path, tmp_path: Path) -> None:
    task = json.loads(task_path.read_text(encoding="utf-8"))
    settings_overrides = {"web_publish_enabled": True} if task.get("publish") else {}

    with _client(tmp_path, **settings_overrides) as client:
        response = client.post("/validate-task", headers=_bearer(), json=task)

    assert response.status_code == 200, response.text
    payload = response.json()
    normalized = TaskInput.model_validate(payload["task"])
    assert payload["valid"] is True
    assert payload["task_fingerprint"] == task_fingerprint(normalized)


def test_task_preflight_rejects_unknown_anime_params(tmp_path: Path) -> None:
    task = {
        "description": "动漫参数污染",
        "content_type": "anime",
        "publish": False,
        "params": {"script": "第一幕：安全测试。", "dry_run": True, "unexpected": "value"},
    }
    with _client(tmp_path) as client:
        response = client.post("/validate-task", headers=_bearer(), json=task)

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "task_validation_failed"
    assert detail["errors"] == [
        {"field": "unexpected", "message": "Extra inputs are not permitted", "type": "extra_forbidden"}
    ]


def test_request_validation_does_not_echo_task_body(tmp_path: Path) -> None:
    task = {
        "content_type": "ai_art",
        "params": {"source_dir": "G:/do-not-echo/private-source"},
    }
    with _client(tmp_path) as client:
        response = client.post("/validate-task", headers=_bearer(), json=task)

    assert response.status_code == 422
    assert response.json()["detail"]["errors"][0]["field"] == "description"
    assert "do-not-echo" not in response.text


def test_task_preflight_returns_sanitized_field_errors(tmp_path: Path) -> None:
    task = {
        "description": "缺少 AI Art 必填字段",
        "content_type": "ai_art",
        "publish": False,
        "params": {"source_dir": "G:/private/source"},
    }
    with _client(tmp_path) as client:
        response = client.post("/validate-task", headers=_bearer(), json=task)

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "task_validation_failed"
    assert {error["field"] for error in detail["errors"]} == {"process_name", "title"}
    assert "G:/private/source" not in response.text
    assert all(set(error) == {"field", "message", "type"} for error in detail["errors"])


def test_task_preflight_blocks_web_publish_when_server_guard_is_off(tmp_path: Path) -> None:
    task = {
        "description": "不允许的发布任务",
        "content_type": "anime",
        "publish": True,
        "params": {"script": "安全测试", "dry_run": True},
    }
    with _client(tmp_path) as client:
        response = client.post("/validate-task", headers=_bearer(), json=task)

    assert response.status_code == 403
    assert response.json()["detail"] == "web publishing is disabled"


def test_task_preflight_rejects_platform_not_supported_by_pipeline(tmp_path: Path) -> None:
    task = {
        "description": "平台不匹配",
        "content_type": "japanese",
        "publish": False,
        "publish_targets": [{"platform": "douyin", "account": "test"}],
        "params": {"source_dir": "G:/images", "dry_run": True},
    }
    with _client(tmp_path) as client:
        response = client.post("/validate-task", headers=_bearer(), json=task)

    assert response.status_code == 422
    errors = response.json()["detail"]["errors"]
    assert errors == [{"field": "task", "message": "流水线不支持以下发布平台：douyin"}]


def _ready_briefing_draft(tmp_path: Path) -> str:
    """Write a valid ready ai_briefing draft directly to disk; return its draft_id."""
    from content_pipeline.content_studio.models import ContentDraft, ResearchSource
    from content_pipeline.content_studio.store import ContentDraftStore

    draft_id = "b" * 32
    draft = ContentDraft(
        draft_id=draft_id,
        template_id="ai_briefing_90s",
        title="每日 AI 简报测试",
        script="未来一周科技与金融领域迎来多项关键进展。" * 20,
        status="ready",
        revision=1,
        research=[
            ResearchSource(
                source_id="ai-briefing",
                label="AI 简报",
                skill_id="AI_BRIEFING_MARKDOWN",
                status="succeeded",
                summary="最新 AI 简报",
            )
        ],
    )
    ContentDraftStore(tmp_path / "sensitive-output-path")._save_unlocked(draft)
    return draft_id


@pytest.mark.parametrize("script", ["短稿。", "A" * 601 + "。"])
def test_content_studio_creates_video_task_outside_recommended_length(tmp_path: Path, script: str) -> None:
    from content_pipeline.content_studio.models import ContentDraft, ResearchSource
    from content_pipeline.content_studio.store import ContentDraftStore

    draft_id = "c" * 32
    ContentDraftStore(tmp_path / "sensitive-output-path")._save_unlocked(
        ContentDraft(
            draft_id=draft_id,
            template_id="ai_briefing_90s",
            title="区间外口播",
            script=script,
            status="ready",
            revision=1,
            research=[
                ResearchSource(
                    source_id="ai-briefing",
                    label="AI 简报",
                    skill_id="AI_BRIEFING_MARKDOWN",
                    status="succeeded",
                )
            ],
        )
    )
    app = create_app(_settings(tmp_path))
    app.state.orchestrator.run = lambda _task_id: None

    with TestClient(app, base_url="http://testserver", client=("127.0.0.1", 50000)) as client:
        response = client.post(
            f"/content/drafts/{draft_id}/video",
            headers=_bearer(),
            json={"revision": 1, "dry_run": False},
        )
        status = client.get(f"/status/{response.json()['task_id']}", headers=_bearer())

    assert response.status_code == 202
    assert status.status_code == 200
    assert status.json()["task"]["params"]["script"] == script


def test_content_studio_video_defaults_to_local_only(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    app.state.orchestrator.run = lambda _task_id: None
    with TestClient(app, base_url="http://testserver", client=("127.0.0.1", 50000)) as client:
        draft_id = _ready_briefing_draft(tmp_path)
        response = client.post(
            f"/content/drafts/{draft_id}/video",
            headers=_bearer(),
            json={"revision": 1, "dry_run": False, "voice_rate": 0.9},
        )
        task_id = response.json()["task_id"]
        status = client.get(f"/status/{task_id}", headers=_bearer())
        listed = client.get("/jobs", headers=_bearer())

    assert response.status_code == 202
    assert status.json()["publication_summary"]["state"] == "waiting_generation"
    listed_job = next(item for item in listed.json()["jobs"] if item["task_id"] == task_id)
    assert listed_job["publication_summary"]["label"] == "待生成"
    snapshot = json.loads(
        (tmp_path / "sensitive-output-path" / "jobs" / task_id / "status.json").read_text(encoding="utf-8")
    )
    assert snapshot["task"]["publish"] is False
    assert snapshot["task"]["publish_targets"] == []
    assert snapshot["task"]["params"]["voice_rate"] == 0.9


def test_content_studio_video_generation_rejects_publish_fields(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path, web_publish_enabled=True))
    app.state.orchestrator.run = lambda _task_id: None
    with TestClient(app, base_url="http://testserver", client=("127.0.0.1", 50000)) as client:
        draft_id = _ready_briefing_draft(tmp_path)
        response = client.post(
            f"/content/drafts/{draft_id}/video",
            headers=_bearer(),
            json={
                "revision": 1,
                "dry_run": False,
                "publish": True,
                "publish_targets": [{"platform": "douyin", "account": "金融破壁人"}],
            },
        )

    assert response.status_code == 422
    error_fields = {error["field"] for error in response.json()["detail"]["errors"]}
    assert "publish" in error_fields
    assert "publish_targets" in error_fields


def test_content_draft_requests_reject_extra_fields(tmp_path: Path) -> None:
    draft_id = _ready_briefing_draft(tmp_path)
    with _client(tmp_path) as client:
        create = client.post(
            "/content/drafts",
            headers=_bearer(),
            json={"title": "今日金融资讯", "unexpected": True},
        )
        update = client.patch(
            f"/content/drafts/{draft_id}",
            headers=_bearer(),
            json={"revision": 1, "title": "今日金融资讯", "script": "已编辑", "status": "ready"},
        )
        blank_script = client.patch(
            f"/content/drafts/{draft_id}",
            headers=_bearer(),
            json={"revision": 1, "title": "今日金融资讯", "script": "   "},
        )
        refresh = client.post(
            f"/content/drafts/{draft_id}/refresh",
            headers=_bearer(),
            json={"revision": 1, "force": True},
        )
        batch_delete = client.post(
            "/content/drafts/batch-delete",
            headers=_bearer(),
            json={"draft_ids": [draft_id], "force": True},
        )

    assert create.status_code == 422
    assert update.status_code == 422
    assert blank_script.status_code == 422
    assert refresh.status_code == 422
    assert batch_delete.status_code == 422


def test_crud_route_contract_registers_expected_methods(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    paths = app.openapi()["paths"]

    assert set(paths["/content/drafts"]) >= {"get", "post"}
    assert set(paths["/content/drafts/batch-delete"]) == {"post"}
    assert set(paths["/content/drafts/{draft_id}"]) >= {"get", "patch", "delete"}
    assert set(paths["/jobs"]) == {"get"}
    assert set(paths["/jobs/batch-delete"]) == {"post"}
    assert set(paths["/jobs/{task_id}"]) >= {"get", "patch", "delete"}

    with TestClient(app, base_url="http://testserver", client=("127.0.0.1", 50000)) as client:
        draft_alias = client.post("/content/drafts/delete", headers=_bearer(), json={"draft_ids": ["0" * 32]})
        job_alias = client.post("/jobs/delete", headers=_bearer(), json={"task_ids": ["0" * 32]})

    assert draft_alias.status_code == 200
    assert job_alias.status_code == 200
