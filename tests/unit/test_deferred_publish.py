import json
import time
from pathlib import Path

from fastapi.testclient import TestClient

import content_pipeline.api.security as security_module
from content_pipeline.api.app import create_app
from content_pipeline.deferred_publishing import publication_readiness, publication_summary
from content_pipeline.models import JobStatus, MediaValidation, PipelineStep, PublishTarget, TaskInput, VideoValidation
from content_pipeline.settings import Settings

ADMIN_TOKEN = "admin-" + "a" * 32
API_TOKEN = "api-" + "b" * 32
SESSION_SECRET = "session-" + "c" * 32


def _settings(tmp_path: Path, **overrides) -> Settings:
    profiles = tmp_path / "profiles"
    profiles.mkdir(parents=True, exist_ok=True)
    values = {
        "data_dir": tmp_path / "output",
        "profiles_dir": profiles,
        "web_allowed_hosts": "testserver",
        "web_allowed_origins": "http://testserver",
        "web_auth_required": True,
        "web_admin_token": ADMIN_TOKEN,
        "web_api_token": API_TOKEN,
        "web_session_secret": SESSION_SECRET,
        "web_publish_enabled": True,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _bearer() -> dict[str, str]:
    return {"Authorization": f"Bearer {API_TOKEN}"}


def _completed_content_job(app, *, dry_run: bool = False, validated: bool = True, legacy: bool = False):
    store = app.state.orchestrator.store
    snapshot = store.create(
        TaskInput(
            description="内容工作台 · 今日金融资讯",
            content_type="script_video",
            topic="今日金融资讯",
            params={
                "title": "今日金融资讯",
                "script": "市场信息。" * 80,
                "description": "每日市场观察",
                "tags": ["金融", "市场"],
                "dry_run": dry_run,
            },
            origin=None if legacy else "content_studio",
            source_draft_id=None if legacy else "a" * 32,
            source_draft_revision=None if legacy else 2,
        )
    )
    video = store.job_dir(snapshot.task_id) / "video" / "final-1.mp4"
    video.write_bytes(b"validated-video-bytes")
    snapshot.status = JobStatus.SUCCEEDED
    snapshot.artifacts.video = video
    if validated:
        snapshot.artifacts.validation = MediaValidation(
            video=VideoValidation(
                duration_seconds=90,
                width=1080,
                height=1920,
                aspect_ratio=0.5625,
                video_codec="h264",
                audio_codec="aac",
                decoded_video_frames=2700,
                decoded_audio_frames=4000,
            )
        )
    store.save(snapshot)
    return snapshot, video


def _body(title: str = "今日金融资讯") -> dict:
    return {
        "publish_targets": [{"platform": "douyin", "account": "金融破壁人"}],
        "title": title,
        "description": "每日市场观察",
        "tags": ["金融", "市场"],
    }


def test_completed_content_job_requires_artifact_bound_confirmation_and_keeps_generation_status(
    tmp_path: Path, monkeypatch
) -> None:
    app = create_app(_settings(tmp_path))
    snapshot, video = _completed_content_job(app)
    monkeypatch.setattr(
        "content_pipeline.deferred_publishing.call_sau_target",
        lambda **_kwargs: {
            "success": True,
            "visibility": "private",
            "private_visibility_proof": "已设置为“仅自己可见”",
            "publication_proof": "视频发布成功",
        },
    )
    monkeypatch.setattr(app.state.executor, "submit", lambda fn, **kwargs: fn(**kwargs))

    with TestClient(app, base_url="http://testserver", client=("127.0.0.1", 50000)) as client:
        readiness = client.get(f"/jobs/{snapshot.task_id}/publish-readiness", headers=_bearer())
        unconfirmed = client.post(f"/jobs/{snapshot.task_id}/publish", headers=_bearer(), json=_body())
        required = unconfirmed.json()["detail"]["required_confirmation"]
        changed = client.post(
            f"/jobs/{snapshot.task_id}/publish",
            headers={**_bearer(), "X-AI-Pipeline-Publish-Confirmation": required},
            json=_body("已被修改的标题"),
        )
        accepted = client.post(
            f"/jobs/{snapshot.task_id}/publish",
            headers={**_bearer(), "X-AI-Pipeline-Publish-Confirmation": required},
            json=_body(),
        )
        completed = client.get(f"/status/{snapshot.task_id}", headers=_bearer()).json()

    assert readiness.status_code == 200
    assert readiness.json()["eligible"] is True
    assert unconfirmed.status_code == 409
    assert required.startswith("PUBLISH:")
    assert changed.status_code == 409
    assert accepted.status_code == 202
    assert completed["status"] == "succeeded"
    assert completed["current_step"] is None
    assert completed["publication_attempts"][0]["status"] == "succeeded"
    assert completed["publication_attempts"][0]["results"]["douyin"]["success"] is True
    assert completed["publication_summary"]["state"] == "published"
    assert completed["publication_summary"]["label"] == "已发布"
    assert completed["publication_summary"]["targets"][0] == {
        "platform": "douyin",
        "account": "金融破壁人",
        "status": "published",
        "success": True,
        "visibility": "private",
        "proof": "视频发布成功",
        "error": None,
        "skipped": False,
    }
    assert video.read_bytes() == b"validated-video-bytes"
    events = app.state.orchestrator.store.events_path(snapshot.task_id).read_text(encoding="utf-8")
    assert '"event": "publish_requested"' in events
    assert '"event": "publish_started"' in events
    assert '"event": "publish_target_finished"' in events
    assert '"event": "publish_finished"' in events


def test_publish_readiness_supports_legacy_studio_job_and_rejects_unsafe_jobs(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    legacy, _ = _completed_content_job(app, legacy=True)
    dry_run, _ = _completed_content_job(app, dry_run=True)
    unvalidated, _ = _completed_content_job(app, validated=False)
    missing, missing_video = _completed_content_job(app)
    missing_video.unlink()

    with TestClient(app, base_url="http://testserver", client=("127.0.0.1", 50000)) as client:
        legacy_result = client.get(f"/jobs/{legacy.task_id}/publish-readiness", headers=_bearer()).json()
        dry_result = client.get(f"/jobs/{dry_run.task_id}/publish-readiness", headers=_bearer()).json()
        validation_result = client.get(f"/jobs/{unvalidated.task_id}/publish-readiness", headers=_bearer()).json()
        missing_result = client.get(f"/jobs/{missing.task_id}/publish-readiness", headers=_bearer()).json()

    assert legacy_result["eligible"] is True
    assert legacy_result["content_studio_job"] is True
    assert dry_result["eligible"] is False
    assert any("干运行" in reason for reason in dry_result["reasons"])
    assert validation_result["eligible"] is False
    assert any("媒体校验" in reason for reason in validation_result["reasons"])
    assert missing_result["eligible"] is False
    assert any("不存在" in reason for reason in missing_result["reasons"])


def test_publish_is_blocked_when_server_guard_is_off_or_video_leaves_job_directory(tmp_path: Path) -> None:
    disabled_app = create_app(_settings(tmp_path / "disabled", web_publish_enabled=False))
    disabled, _ = _completed_content_job(disabled_app)
    with TestClient(disabled_app, base_url="http://testserver", client=("127.0.0.1", 50000)) as client:
        blocked = client.post(f"/jobs/{disabled.task_id}/publish", headers=_bearer(), json=_body())

    guarded_app = create_app(_settings(tmp_path / "outside"))
    guarded, _ = _completed_content_job(guarded_app)
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"outside")
    current = guarded_app.state.orchestrator.store.get(guarded.task_id)
    current.artifacts.video = outside
    guarded_app.state.orchestrator.store.save(current)
    with TestClient(guarded_app, base_url="http://testserver", client=("127.0.0.1", 50000)) as client:
        escaped = client.post(f"/jobs/{guarded.task_id}/publish", headers=_bearer(), json=_body())

    symlink_app = create_app(_settings(tmp_path / "symlink-video"))
    symlinked, video = _completed_content_job(symlink_app)
    outside_symlink_target = tmp_path / "outside-symlink-target.mp4"
    outside_symlink_target.write_bytes(b"outside-symlink-target")
    video.unlink()
    video.symlink_to(outside_symlink_target)
    with TestClient(symlink_app, base_url="http://testserver", client=("127.0.0.1", 50000)) as client:
        symlinked_video = client.post(f"/jobs/{symlinked.task_id}/publish", headers=_bearer(), json=_body())

    assert blocked.status_code == 403
    assert escaped.status_code == 409
    assert "受保护的任务目录" in escaped.json()["detail"]
    assert symlinked_video.status_code == 409
    assert "受保护的任务目录" in symlinked_video.json()["detail"]


def test_publication_readiness_rejects_symlinked_job_directory(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    snapshot, _ = _completed_content_job(app)
    store = app.state.orchestrator.store
    job_dir = store.job_dir(snapshot.task_id)
    outside = tmp_path / "outside-job"
    job_dir.rename(outside)
    job_dir.symlink_to(outside, target_is_directory=True)

    readiness = publication_readiness(snapshot, store, publish_enabled=True)

    assert readiness["eligible"] is False
    assert any("任务目录不存在" in reason for reason in readiness["reasons"])


def test_video_change_invalidates_confirmation(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    snapshot, video = _completed_content_job(app)
    with TestClient(app, base_url="http://testserver", client=("127.0.0.1", 50000)) as client:
        unconfirmed = client.post(f"/jobs/{snapshot.task_id}/publish", headers=_bearer(), json=_body())
        required = unconfirmed.json()["detail"]["required_confirmation"]
        video.write_bytes(b"changed-after-review")
        rejected = client.post(
            f"/jobs/{snapshot.task_id}/publish",
            headers={**_bearer(), "X-AI-Pipeline-Publish-Confirmation": required},
            json=_body(),
        )

    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "publish_confirmation_required"
    assert rejected.json()["detail"]["required_confirmation"] != required


def test_active_publication_blocks_concurrent_attempt(tmp_path: Path, monkeypatch) -> None:
    app = create_app(_settings(tmp_path))
    snapshot, _ = _completed_content_job(app)
    queued = []
    monkeypatch.setattr(app.state.executor, "submit", lambda fn, **kwargs: queued.append((fn, kwargs)))
    with TestClient(app, base_url="http://testserver", client=("127.0.0.1", 50000)) as client:
        first = client.post(f"/jobs/{snapshot.task_id}/publish", headers=_bearer(), json=_body())
        required = first.json()["detail"]["required_confirmation"]
        accepted = client.post(
            f"/jobs/{snapshot.task_id}/publish",
            headers={**_bearer(), "X-AI-Pipeline-Publish-Confirmation": required},
            json=_body(),
        )
        concurrent = client.post(f"/jobs/{snapshot.task_id}/publish", headers=_bearer(), json=_body())

    assert accepted.status_code == 202
    assert len(queued) == 1
    assert concurrent.status_code == 409
    assert "正在排队或运行" in concurrent.json()["detail"]
    queued_summary = app.state.orchestrator.store.get(snapshot.task_id)
    assert publication_summary(queued_summary)["state"] == "publishing"
    assert publication_summary(queued_summary)["active"] is True


def test_platform_results_are_independent_and_failed_attempt_can_retry(tmp_path: Path, monkeypatch) -> None:
    app = create_app(_settings(tmp_path))
    snapshot, _ = _completed_content_job(app)
    monkeypatch.setattr(app.state.executor, "submit", lambda fn, **kwargs: fn(**kwargs))

    def partial_upload(*, target, **_kwargs):
        if target.platform == "douyin":
            return {"success": True, "visibility": "private"}
        raise RuntimeError("kuaishou unavailable")

    monkeypatch.setattr("content_pipeline.deferred_publishing.call_sau_target", partial_upload)
    body = {
        **_body(),
        "publish_targets": [
            {"platform": "douyin", "account": "金融破壁人"},
            {"platform": "kuaishou", "account": "破壁人"},
        ],
    }
    with TestClient(app, base_url="http://testserver", client=("127.0.0.1", 50000)) as client:
        first = client.post(f"/jobs/{snapshot.task_id}/publish", headers=_bearer(), json=body)
        required = first.json()["detail"]["required_confirmation"]
        client.post(
            f"/jobs/{snapshot.task_id}/publish",
            headers={**_bearer(), "X-AI-Pipeline-Publish-Confirmation": required},
            json=body,
        )
        after_partial = client.get(f"/status/{snapshot.task_id}", headers=_bearer()).json()

        monkeypatch.setattr(
            "content_pipeline.deferred_publishing.call_sau_target",
            lambda **_kwargs: {"success": True, "visibility": "private"},
        )
        retry_check = client.post(f"/jobs/{snapshot.task_id}/publish", headers=_bearer(), json=body)
        retry_required = retry_check.json()["detail"]["required_confirmation"]
        retry = client.post(
            f"/jobs/{snapshot.task_id}/publish",
            headers={**_bearer(), "X-AI-Pipeline-Publish-Confirmation": retry_required},
            json=body,
        )
        after_retry = client.get(f"/status/{snapshot.task_id}", headers=_bearer()).json()

    assert after_partial["publication_attempts"][0]["status"] == "partial"
    assert after_partial["publication_attempts"][0]["results"]["kuaishou"]["success"] is False
    assert after_partial["publication_summary"]["state"] == "partial"
    assert after_partial["publication_summary"]["needs_attention"] is True
    assert retry.status_code == 202
    assert after_retry["status"] == "succeeded"
    assert after_retry["publication_attempts"][1]["status"] == "succeeded"
    assert after_retry["publication_summary"]["state"] == "published"


def test_publication_summary_distinguishes_waiting_unpublished_and_failed(tmp_path: Path, monkeypatch) -> None:
    app = create_app(_settings(tmp_path))
    store = app.state.orchestrator.store
    waiting = store.create(
        TaskInput(
            description="内容工作台 · 等待视频",
            content_type="script_video",
            topic="等待视频",
            params={"title": "等待视频", "script": "市场信息。" * 80, "dry_run": False},
            origin="content_studio",
            source_draft_id="b" * 32,
            source_draft_revision=1,
        )
    )
    completed, _ = _completed_content_job(app)

    assert publication_summary(waiting)["state"] == "waiting_generation"
    unpublished = publication_summary(store.get(completed.task_id))
    assert unpublished["state"] == "not_published"
    assert unpublished["has_published"] is False
    assert "尚未发送到任何自媒体账号" in unpublished["message"]

    monkeypatch.setattr(
        "content_pipeline.deferred_publishing.call_sau_target",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("account session expired")),
    )
    monkeypatch.setattr(app.state.executor, "submit", lambda fn, **kwargs: fn(**kwargs))
    with TestClient(app, base_url="http://testserver", client=("127.0.0.1", 50000)) as client:
        check = client.post(f"/jobs/{completed.task_id}/publish", headers=_bearer(), json=_body())
        required = check.json()["detail"]["required_confirmation"]
        accepted = client.post(
            f"/jobs/{completed.task_id}/publish",
            headers={**_bearer(), "X-AI-Pipeline-Publish-Confirmation": required},
            json=_body(),
        )
        failed_status = client.get(f"/status/{completed.task_id}", headers=_bearer()).json()
        listed = client.get("/jobs", headers=_bearer()).json()["jobs"]

    assert accepted.status_code == 202
    assert failed_status["status"] == "succeeded"
    assert failed_status["publication_summary"]["state"] == "failed"
    assert failed_status["publication_summary"]["needs_attention"] is True
    assert failed_status["publication_summary"]["targets"][0]["account"] == "金融破壁人"
    assert failed_status["publication_summary"]["targets"][0]["error"] == "account session expired"
    listed_job = next(item for item in listed if item["task_id"] == completed.task_id)
    assert listed_job["publication_summary"]["state"] == "failed"


def test_deferred_publish_session_requires_recent_login(tmp_path: Path, monkeypatch) -> None:
    app = create_app(_settings(tmp_path, web_publish_reauth_seconds=60))
    snapshot, _ = _completed_content_job(app)
    queued = []
    monkeypatch.setattr(app.state.executor, "submit", lambda fn, **kwargs: queued.append((fn, kwargs)))

    with TestClient(app, base_url="http://testserver", client=("127.0.0.1", 50000)) as client:
        login = client.post("/auth/login", json={"token": ADMIN_TOKEN})
        csrf = login.json()["csrf_token"]
        check = client.post(
            f"/jobs/{snapshot.task_id}/publish",
            headers={"X-CSRF-Token": csrf},
            json=_body(),
        )
        required = check.json()["detail"]["required_confirmation"]
        now = time.time()
        monkeypatch.setattr(security_module.time, "time", lambda: now + 61)
        rejected = client.post(
            f"/jobs/{snapshot.task_id}/publish",
            headers={
                "X-CSRF-Token": csrf,
                "X-AI-Pipeline-Publish-Confirmation": required,
            },
            json=_body(),
        )

    assert rejected.status_code == 401
    assert rejected.json()["detail"] == {
        "code": "recent_authentication_required",
        "message": "recent authentication required for publishing",
    }
    assert not queued


def test_deferred_publish_rejects_tencent_target(tmp_path: Path, monkeypatch) -> None:
    app = create_app(_settings(tmp_path))
    snapshot, _ = _completed_content_job(app)

    monkeypatch.setattr(app.state.executor, "submit", lambda fn, **kwargs: fn(**kwargs))
    body = {
        **_body(),
        "publish_targets": [{"platform": "tencent", "account": "每日金融摘要"}],
    }
    with TestClient(app, base_url="http://testserver", client=("127.0.0.1", 50000)) as client:
        readiness = client.get(f"/jobs/{snapshot.task_id}/publish-readiness", headers=_bearer()).json()
        rejected = client.post(f"/jobs/{snapshot.task_id}/publish", headers=_bearer(), json=body)

    assert readiness["supported_platforms"] == ["douyin", "kuaishou"]
    assert "tencent" not in readiness["defaults"]["accounts"]
    assert rejected.status_code == 409
    assert "口播视频不支持以下发布平台" in rejected.json()["detail"]
    assert "tencent" in rejected.json()["detail"]


class _FakeBridgeResponse:
    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def __enter__(self) -> "_FakeBridgeResponse":
        return self

    def __exit__(self, *args) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


def test_publish_account_options_come_from_bridge_and_are_cached(tmp_path: Path, monkeypatch) -> None:
    import content_pipeline.deferred_publishing as deferred_module

    calls = []

    def fake_urlopen(request, timeout=0):
        calls.append({"url": request.full_url, "auth": request.headers.get("Authorization")})
        return _FakeBridgeResponse(
            {
                "accounts": [
                    {"platform": "douyin", "account": "金融破壁人", "status": "valid", "status_label": "有效"},
                    {"platform": "douyin", "account": "金融破壁人", "status": "valid", "status_label": "有效"},
                    {
                        "platform": "kuaishou",
                        "account": "搞AI的罗辑同学",
                        "status": "login_required",
                        "status_label": "需重新登录",
                    },
                    {"platform": "bilibili", "account": "酸菜鱼", "status": "valid", "status_label": "有效"},
                    {"platform": "douyin", "account": "  ", "status": "valid", "status_label": "有效"},
                ]
            }
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr(deferred_module, "_account_options_cache", None)
    app = create_app(_settings(tmp_path, sau_bridge_url="http://bridge.test:5800/", sau_bridge_token="secret-token"))
    snapshot, _ = _completed_content_job(app)

    with TestClient(app, base_url="http://testserver", client=("127.0.0.1", 50000)) as client:
        first = client.get(f"/jobs/{snapshot.task_id}/publish-readiness", headers=_bearer()).json()
        second = client.get(f"/jobs/{snapshot.task_id}/publish-readiness", headers=_bearer()).json()

    assert calls == [{"url": "http://bridge.test:5800/api/accounts", "auth": "Bearer secret-token"}]
    options = first["account_options"]
    assert set(options) == {"douyin", "kuaishou"}
    assert [item["account"] for item in options["douyin"]] == ["金融破壁人"]
    assert options["kuaishou"] == [
        {"account": "搞AI的罗辑同学", "status": "login_required", "status_label": "需重新登录"}
    ]
    assert second["account_options"] == options


def test_publish_account_options_fall_back_to_empty_when_bridge_fails(tmp_path: Path, monkeypatch) -> None:
    import content_pipeline.deferred_publishing as deferred_module

    def failing_urlopen(request, timeout=0):
        raise OSError("bridge offline")

    monkeypatch.setattr("urllib.request.urlopen", failing_urlopen)
    monkeypatch.setattr(deferred_module, "_account_options_cache", None)
    app = create_app(_settings(tmp_path, sau_bridge_url="http://bridge.test:5800"))
    snapshot, _ = _completed_content_job(app)

    with TestClient(app, base_url="http://testserver", client=("127.0.0.1", 50000)) as client:
        readiness = client.get(f"/jobs/{snapshot.task_id}/publish-readiness", headers=_bearer()).json()

    assert readiness["eligible"] is True
    assert readiness["account_options"] == {}


def test_public_visibility_changes_confirmation_and_reaches_upload_target(tmp_path: Path, monkeypatch) -> None:
    app = create_app(_settings(tmp_path))
    snapshot, _ = _completed_content_job(app)
    used_visibilities = []

    def record_upload(*, target, **_kwargs):
        used_visibilities.append(target.visibility)
        return {"success": True, "visibility": target.visibility, "publication_proof": "视频发布成功"}

    monkeypatch.setattr("content_pipeline.deferred_publishing.call_sau_target", record_upload)
    monkeypatch.setattr(app.state.executor, "submit", lambda fn, **kwargs: fn(**kwargs))
    public_body = {
        **_body(),
        "publish_targets": [{"platform": "douyin", "account": "金融破壁人", "visibility": "public"}],
    }
    with TestClient(app, base_url="http://testserver", client=("127.0.0.1", 50000)) as client:
        private_check = client.post(f"/jobs/{snapshot.task_id}/publish", headers=_bearer(), json=_body())
        public_check = client.post(f"/jobs/{snapshot.task_id}/publish", headers=_bearer(), json=public_body)
        required = public_check.json()["detail"]["required_confirmation"]
        accepted = client.post(
            f"/jobs/{snapshot.task_id}/publish",
            headers={**_bearer(), "X-AI-Pipeline-Publish-Confirmation": required},
            json=public_body,
        )
        completed = client.get(f"/status/{snapshot.task_id}", headers=_bearer()).json()

    assert private_check.json()["detail"]["required_confirmation"] != required
    assert accepted.status_code == 202
    assert used_visibilities == ["public"]
    assert completed["publication_summary"]["targets"][0]["visibility"] == "public"
    assert completed["publication_summary"]["targets"][0]["status"] == "published"


def _inline_job(
    app,
    *,
    publish: bool,
    status: JobStatus = JobStatus.SUCCEEDED,
    publish_results: dict | None = None,
    upload_result: dict | None = None,
    current_step: PipelineStep | None = None,
):
    store = app.state.orchestrator.store
    snapshot = store.create(
        TaskInput(
            description="AI简报",
            content_type="ai_briefing",
            publish=publish,
            publish_targets=[
                PublishTarget(platform="douyin", account="金融破壁人"),
                PublishTarget(platform="kuaishou", account="破壁人"),
            ]
            if publish
            else [],
            params={"date": "20260903"},
        )
    )
    snapshot.status = status
    snapshot.current_step = current_step
    if publish_results is not None:
        snapshot.artifacts.publish_results = publish_results
    if upload_result is not None:
        snapshot.artifacts.upload_result = upload_result
    store.save(snapshot)
    return snapshot


def test_inline_publication_summary_not_requested(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    snapshot = _inline_job(app, publish=False, upload_result={"skipped": True, "reason": "publish_not_requested"})

    summary = publication_summary(snapshot)

    assert summary["state"] == "not_published"
    assert summary["label"] == "未发布"
    assert summary["content_studio_job"] is False
    assert summary["active"] is False


def test_inline_publication_summary_published(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    snapshot = _inline_job(
        app,
        publish=True,
        publish_results={
            "douyin": {"success": True, "visibility": "private", "publication_proof": "视频发布成功"},
            "kuaishou": {"success": True, "visibility": "private", "publication_proof": "视频发布成功"},
        },
        upload_result={"targets": {}, "visibility": "private"},
    )

    summary = publication_summary(snapshot)

    assert summary["state"] == "published"
    assert summary["label"] == "已发布"
    assert summary["has_published"] is True
    assert summary["needs_attention"] is False
    assert [target["account"] for target in summary["targets"]] == ["金融破壁人", "破壁人"]
    assert len(summary["published_targets"]) == 2


def test_inline_publication_summary_partial_and_failed(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    partial = _inline_job(
        app,
        publish=True,
        status=JobStatus.PARTIAL,
        publish_results={
            "douyin": {"success": True, "publication_proof": "视频发布成功"},
            "kuaishou": {"success": False, "error": "account session expired"},
        },
    )
    failed = _inline_job(
        app,
        publish=True,
        status=JobStatus.PARTIAL,
        publish_results={
            "douyin": {"success": False, "error": "account session expired"},
            "kuaishou": {"success": False, "error": "account session expired"},
        },
    )

    partial_summary = publication_summary(partial)
    failed_summary = publication_summary(failed)

    assert partial_summary["state"] == "partial"
    assert partial_summary["label"] == "部分发布"
    assert partial_summary["needs_attention"] is True
    assert failed_summary["state"] == "failed"
    assert failed_summary["label"] == "发布失败"
    assert failed_summary["needs_attention"] is True
    assert failed_summary["has_published"] is False


def test_inline_publication_summary_dry_run_is_not_published(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    snapshot = _inline_job(
        app,
        publish=True,
        publish_results={"douyin": {"success": True, "dry_run": True, "command": ["sau"]}},
    )

    summary = publication_summary(snapshot)

    assert summary["state"] == "not_published"
    assert summary["has_published"] is False
    assert "干运行" in summary["message"]


def test_inline_publication_summary_running_states(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    generating = _inline_job(app, publish=True, status=JobStatus.RUNNING, current_step=PipelineStep.VIDEO)
    uploading = _inline_job(app, publish=True, status=JobStatus.RUNNING, current_step=PipelineStep.UPLOAD)

    generating_summary = publication_summary(generating)
    uploading_summary = publication_summary(uploading)

    assert generating_summary["state"] == "not_published"
    assert generating_summary["active"] is False
    assert uploading_summary["state"] == "publishing"
    assert uploading_summary["active"] is True


def test_inline_publication_summary_failed_before_upload(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    snapshot = _inline_job(app, publish=True, status=JobStatus.FAILED)

    summary = publication_summary(snapshot)

    assert summary["state"] == "not_published"
    assert "发布未执行" in summary["message"]


def test_inline_publication_summary_grouped_results(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    snapshot = _inline_job(
        app,
        publish=True,
        upload_result={
            "groups": {
                "0": {"bilibili": {"success": True, "publication_proof": "视频发布成功"}},
                "1": {"douyin": {"success": False, "error": "upload rejected"}},
            }
        },
    )

    summary = publication_summary(snapshot)

    assert summary["state"] == "partial"
    assert summary["has_published"] is True
    assert {target["platform"] for target in summary["targets"]} == {"bilibili", "douyin"}


def test_inline_publication_summary_cleans_raw_uploader_output(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    blob = (
        'stdout:\nSAU_UPLOAD_RESULT:{"platform":"kuaishou","success":false,'
        '"error":"Kuaishou cookie is missing or expired. Run `sau kuaishou login` first."}\n'
        "stderr:\n\x1b[38;2;112;172;222m2026-09-04 09:48:10\x1b[0m | INFO: noisy uploader log\n"
    )
    snapshot = _inline_job(
        app,
        publish=True,
        status=JobStatus.PARTIAL,
        publish_results={
            "douyin": {"success": True, "publication_proof": "视频发布成功"},
            "kuaishou": {"success": False, "error": blob},
        },
    )

    summary = publication_summary(snapshot)
    kuaishou = next(target for target in summary["targets"] if target["platform"] == "kuaishou")

    assert summary["state"] == "partial"
    assert kuaishou["error"] == "Kuaishou cookie is missing or expired. Run `sau kuaishou login` first."


def test_inline_publication_summary_truncates_long_errors(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    snapshot = _inline_job(
        app,
        publish=True,
        status=JobStatus.PARTIAL,
        publish_results={"douyin": {"success": False, "error": "\x1b[97m" + "x" * 500 + "\x1b[0m"}},
    )

    summary = publication_summary(snapshot)
    error = summary["targets"][0]["error"]

    assert error is not None
    assert len(error) <= 301
    assert "\x1b" not in error
