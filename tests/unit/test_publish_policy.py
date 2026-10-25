from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from content_pipeline.api.app import create_app
from content_pipeline.errors import ConfigError
from content_pipeline.job_store import JobStore
from content_pipeline.models import JobStatus, PublishTarget, TaskInput
from content_pipeline.publish_policy import load_publish_policy, resolve_publish_plan, save_publish_policy
from content_pipeline.publishing import publish_generated_video
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


def test_load_publish_policy_packaged_defaults(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    policy = load_publish_policy(settings)

    assert policy["finance"]["enabled"] is False
    assert [target.platform for target in policy["finance"]["targets"]] == ["douyin", "kuaishou"]


def test_save_publish_policy_overrides_defaults(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    (settings.data_dir).mkdir(parents=True, exist_ok=True)

    effective = save_publish_policy(
        settings,
        {"finance": {"enabled": True, "targets": [{"platform": "douyin", "account": "金融破壁人"}]}},
    )

    assert effective["finance"]["enabled"] is True
    assert [target.account for target in effective["finance"]["targets"]] == ["金融破壁人"]
    assert effective["ai_briefing"]["enabled"] is False  # untouched entries keep packaged defaults
    reloaded = load_publish_policy(
        Settings(_env_file=None, data_dir=settings.data_dir, profiles_dir=settings.profiles_dir)
    )
    assert reloaded["finance"]["enabled"] is True


def test_save_publish_policy_rejects_unknown_content_type(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with pytest.raises(ConfigError):
        save_publish_policy(settings, {"unknown_pipeline": {"enabled": True, "targets": []}})


def test_resolve_publish_plan_precedence(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    save_publish_policy(
        settings,
        {"finance": {"enabled": True, "targets": [{"platform": "douyin", "account": "金融破壁人"}]}},
    )

    inherited = resolve_publish_plan("finance", TaskInput(description="t"), settings=settings)
    explicit_off = resolve_publish_plan("finance", TaskInput(description="t", publish=False), settings=settings)
    explicit_targets = resolve_publish_plan(
        "finance",
        TaskInput(description="t", publish_targets=[PublishTarget(platform="kuaishou", account="破壁人")]),
        settings=settings,
    )

    assert inherited["requested"] is True
    assert [target.platform for target in inherited["targets"]] == ["douyin"]
    assert explicit_off["requested"] is False
    assert [target.account for target in explicit_targets["targets"]] == ["破壁人"]


def test_publish_generated_video_skip_merges_existing_upload_result(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = JobStore(settings.data_dir)
    snapshot = store.create(TaskInput(description="t", content_type="finance", publish=False))
    snapshot.artifacts.upload_result = {"script_writer": "rule"}
    store.save(snapshot)

    final, error = publish_generated_video(
        snapshot=snapshot,
        store=store,
        artifacts=snapshot.artifacts,
        settings=settings,
        video=tmp_path / "video.mp4",
        title="t",
        description="",
        tags=[],
        requested=False,
        targets=[],
        dry_run=False,
    )

    saved = store.get(snapshot.task_id)
    assert final == JobStatus.SUCCEEDED
    assert error is None
    assert saved.artifacts.upload_result == {
        "script_writer": "rule",
        "skipped": True,
        "reason": "publish_not_requested",
    }


def test_publish_generated_video_loop_and_failure_status(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    store = JobStore(settings.data_dir)
    snapshot = store.create(TaskInput(description="t", content_type="finance", publish=True))
    calls: list[str] = []

    def fake_sau(*, target, **_kwargs):
        calls.append(target.platform)
        if target.platform == "kuaishou":
            return {"success": False, "error": "account session expired"}
        return {"success": True, "visibility": "private", "publication_proof": "视频发布成功"}

    monkeypatch.setattr("content_pipeline.publishing.call_sau_target", fake_sau)
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")

    final, error = publish_generated_video(
        snapshot=snapshot,
        store=store,
        artifacts=snapshot.artifacts,
        settings=settings,
        video=video,
        title="t",
        description="",
        tags=[],
        requested=True,
        targets=[
            PublishTarget(platform="douyin", account="a"),
            PublishTarget(platform="kuaishou", account="b"),
            PublishTarget(platform="xiaohongshu", account="c"),
        ],
        dry_run=False,
    )

    saved = store.get(snapshot.task_id)
    assert calls == ["douyin", "kuaishou"]
    assert final == JobStatus.PARTIAL
    assert error == "publish_failed: kuaishou, xiaohongshu"
    assert saved.artifacts.publish_results["douyin"]["success"] is True
    assert saved.artifacts.publish_results["xiaohongshu"]["success"] is False
    assert saved.artifacts.upload_result["visibility"] == "mixed"


def test_publish_policy_api_roundtrip(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    with TestClient(app, base_url="http://testserver", client=("127.0.0.1", 50000)) as client:
        initial = client.get("/publish-policy", headers=_bearer())
        updated = client.put(
            "/publish-policy",
            headers=_bearer(),
            json={
                "policy": {
                    "ai_briefing": {"enabled": True, "targets": [{"platform": "douyin", "account": "金融破壁人"}]}
                }
            },
        )
        invalid = client.put(
            "/publish-policy",
            headers=_bearer(),
            json={"policy": {"finance": {"enabled": True, "targets": [{"platform": "onlyfans", "account": "x"}]}}},
        )
        reread = client.get("/publish-policy", headers=_bearer())

    assert initial.status_code == 200
    assert initial.json()["policy"]["ai_briefing"]["enabled"] is False
    assert updated.status_code == 200
    assert updated.json()["policy"]["ai_briefing"]["enabled"] is True
    assert invalid.status_code == 422
    assert reread.json()["policy"]["ai_briefing"]["targets"] == [
        {"platform": "douyin", "account": "金融破壁人", "tid": None, "visibility": "private"}
    ]
