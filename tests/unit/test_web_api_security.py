import importlib
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from content_pipeline.api.app import create_app, task_fingerprint
from content_pipeline.api.artifacts import registered_artifacts
from content_pipeline.api.security import WebSecurity
from content_pipeline.models import (
    ArtifactSet,
    JobSnapshot,
    JobStatus,
    MetadataAuditSummary,
    MetadataStageAuditSummary,
    TaskInput,
)
from content_pipeline.settings import Settings

security_module = importlib.import_module("content_pipeline.api.security")

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
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _client(tmp_path: Path, **overrides) -> TestClient:
    app = create_app(_settings(tmp_path, **overrides))
    return TestClient(app, base_url="http://testserver", client=("127.0.0.1", 50000))


def _bearer() -> dict[str, str]:
    return {"Authorization": f"Bearer {API_TOKEN}"}


def test_health_is_minimal_and_other_endpoints_require_authentication(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        health = client.get("/health")
        ready = client.get("/ready")
        authenticated_ready = client.get("/ready", headers=_bearer())

    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    assert health.headers["x-content-type-options"] == "nosniff"
    assert ready.status_code == 401
    assert authenticated_ready.status_code == 200
    assert authenticated_ready.json() == {
        "status": "ready",
        "data_dir_available": True,
        "profiles_available": True,
        "pipeline_defaults_available": True,
    }
    assert "output" not in authenticated_ready.text


def test_uptime_kuma_monitor_is_public_and_minimal(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        monitor = client.get("/monitor/uptime-kuma")

    # 远程网段内的 Uptime Kuma 无需凭据即可监测，但仍在边界和 Host 校验之下
    remote_settings = _settings(
        tmp_path / "remote",
        web_allowed_networks="10.147.17.0/24",
        web_allowed_hosts="10.147.17.5",
        web_allowed_origins="https://10.147.17.5:8080",
    )
    with TestClient(
        create_app(remote_settings),
        base_url="https://10.147.17.5:8080",
        client=("10.147.17.42", 50000),
    ) as remote_client:
        remote_monitor = remote_client.get("/monitor/uptime-kuma")

    assert monitor.status_code == 200
    assert monitor.json() == {
        "ok": True,
        "service": "ai-pipeline",
        "status": "up",
        "app": "AI Pipeline",
    }
    assert monitor.headers["x-content-type-options"] == "nosniff"
    assert remote_monitor.status_code == 200
    assert remote_monitor.json()["status"] == "up"


def test_ready_returns_503_without_leaking_paths_when_profiles_are_missing(tmp_path: Path) -> None:
    missing_profiles_dir = tmp_path / "missing-profiles"
    assert not missing_profiles_dir.exists()

    with _client(tmp_path, profiles_dir=missing_profiles_dir) as client:
        response = client.get("/ready", headers=_bearer())

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "data_dir_available": True,
        "profiles_available": False,
        "pipeline_defaults_available": True,
    }
    assert str(missing_profiles_dir) not in response.text


def test_ready_returns_503_without_leaking_paths_when_pipeline_defaults_are_missing(tmp_path: Path) -> None:
    missing_defaults = tmp_path / "missing-defaults.yaml"
    assert not missing_defaults.exists()

    with _client(tmp_path, pipeline_defaults_file=missing_defaults) as client:
        response = client.get("/ready", headers=_bearer())

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "data_dir_available": True,
        "profiles_available": True,
        "pipeline_defaults_available": False,
    }
    assert str(missing_defaults) not in response.text


@pytest.mark.parametrize(
    "overrides, authorization",
    [
        (
            {
                "web_admin_token": "short",
                "web_api_token": "short",
                "web_session_secret": "short",
            },
            "Bearer short",
        ),
        (
            {"web_session_secret": ADMIN_TOKEN},
            f"Bearer {API_TOKEN}",
        ),
        (
            {"web_session_secret": API_TOKEN},
            f"Bearer {API_TOKEN}",
        ),
    ],
)
def test_misconfigured_authentication_fails_closed(
    tmp_path: Path,
    overrides: dict[str, str],
    authorization: str,
) -> None:
    app = create_app(_settings(tmp_path, **overrides))
    with TestClient(app, base_url="http://testserver", client=("127.0.0.1", 50000)) as client:
        health = client.get("/health")
        protected = client.get("/jobs", headers={"Authorization": authorization})

    assert health.status_code == 200
    assert protected.status_code == 503
    assert protected.json()["error"]["code"] == "authentication_misconfigured"


def test_login_session_requires_csrf_for_writes(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        bad_login = client.post("/auth/login", json={"token": "wrong"})
        login = client.post("/auth/login", json={"token": ADMIN_TOKEN})
        csrf_token = login.json()["csrf_token"]
        current = client.get("/auth/me")
        missing_csrf = client.post("/auth/logout")
        logout = client.post("/auth/logout", headers={"X-CSRF-Token": csrf_token})

    assert bad_login.status_code == 401
    assert login.status_code == 200
    assert "HttpOnly" in login.headers["set-cookie"]
    assert "SameSite=strict" in login.headers["set-cookie"]
    assert current.json() == {"authenticated": True, "method": "session"}
    assert missing_csrf.status_code == 403
    assert missing_csrf.json()["error"]["code"] == "csrf_failed"
    assert logout.status_code == 200


def test_login_rejects_blank_token_and_extra_fields(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        blank = client.post("/auth/login", json={"token": "   "})
        extra = client.post("/auth/login", json={"token": ADMIN_TOKEN, "role": "admin"})
        padded = client.post("/auth/login", json={"token": f"  {ADMIN_TOKEN}  "})

    assert blank.status_code == 422
    assert extra.status_code == 422
    assert padded.status_code == 200


def test_session_and_publish_reauth_default_to_seven_days_and_reject_longer_values(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    assert settings.web_session_ttl_seconds == 604800
    assert settings.web_publish_reauth_seconds == 604800
    with pytest.raises(ValidationError):
        _settings(tmp_path, web_session_ttl_seconds=604801)
    with pytest.raises(ValidationError):
        _settings(tmp_path, web_publish_reauth_seconds=604801)


def test_session_expiry_and_cookie_max_age_use_configured_ttl(tmp_path: Path, monkeypatch) -> None:
    issued_at = 1_800_000_000
    monkeypatch.setattr(security_module.time, "time", lambda: issued_at)
    security = WebSecurity(_settings(tmp_path))
    cookie, session = security.new_session()

    assert session.expires_at - session.issued_at == 604800
    assert security.decode_session(cookie) == session

    app = create_app(_settings(tmp_path))
    with TestClient(app, base_url="http://testserver", client=("127.0.0.1", 50000)) as client:
        login = client.post("/auth/login", json={"token": ADMIN_TOKEN})

    set_cookies = login.headers.get_list("set-cookie")
    assert len(set_cookies) == 2
    assert all("Max-Age=604800" in value for value in set_cookies)

    monkeypatch.setattr(security_module.time, "time", lambda: session.expires_at - 1)
    assert security.decode_session(cookie) == session
    monkeypatch.setattr(security_module.time, "time", lambda: session.expires_at)
    assert security.decode_session(cookie) is None


def test_bearer_authentication_and_origin_validation(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        bearer = client.get("/auth/me", headers=_bearer())
        rejected_host = client.get("/health", headers={"Host": "evil.example"})
        rejected_origin = client.post(
            "/auth/login",
            headers={"Origin": "https://evil.example"},
            json={"token": ADMIN_TOKEN},
        )

    assert bearer.json() == {"authenticated": True, "method": "bearer"}
    assert rejected_host.status_code == 400
    assert rejected_host.json()["error"]["code"] == "host_not_allowed"
    assert rejected_origin.status_code == 403
    assert rejected_origin.json()["error"]["code"] == "origin_not_allowed"


def test_remote_peer_requires_configured_network_auth_and_https(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        web_allowed_networks="10.147.17.0/24",
        web_allowed_hosts="10.147.17.5",
        web_allowed_origins="https://10.147.17.5:8080",
    )
    app = create_app(settings)
    with TestClient(
        app,
        base_url="http://10.147.17.5:8080",
        client=("10.147.17.42", 50000),
    ) as insecure_client:
        insecure = insecure_client.get("/health")
    with TestClient(
        app,
        base_url="https://10.147.17.5:8080",
        client=("10.147.17.42", 50000),
    ) as secure_client:
        secure = secure_client.get("/health")
        protected = secure_client.get("/jobs")
        authenticated = secure_client.get("/jobs", headers=_bearer())

    assert insecure.status_code == 426
    assert secure.status_code == 200
    assert "strict-transport-security" in secure.headers
    assert protected.status_code == 401
    assert authenticated.status_code == 200


def test_explicit_zerotier_http_exception_is_required(tmp_path: Path) -> None:
    app = create_app(
        _settings(
            tmp_path,
            web_allowed_networks="10.147.17.0/24",
            web_allowed_hosts="10.147.17.5",
            web_allowed_origins="http://10.147.17.5:8080",
            web_allow_zerotier_http=True,
        )
    )
    with TestClient(
        app,
        base_url="http://10.147.17.5:8080",
        client=("10.147.17.42", 50000),
    ) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert "strict-transport-security" not in response.headers


def test_physical_lan_peer_is_rejected(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path, web_allowed_networks="10.147.17.0/24"))
    with TestClient(app, base_url="http://testserver", client=("192.168.1.25", 50000)) as client:
        response = client.get("/health", headers={"X-Forwarded-For": "10.147.17.42"})

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "peer_not_allowed"


def test_web_publish_requires_enablement_and_task_bound_confirmation(tmp_path: Path) -> None:
    task = {
        "description": "safe dry run",
        "content_type": "anime",
        "publish": True,
        "params": {"script": "第一幕：安全发布确认。", "dry_run": True},
    }
    disabled_app = create_app(_settings(tmp_path / "disabled"))
    disabled_app.state.orchestrator.run = lambda _task_id: None
    with TestClient(disabled_app, base_url="http://testserver", client=("127.0.0.1", 50000)) as client:
        disabled = client.post("/run", headers=_bearer(), json=task)

    enabled_app = create_app(_settings(tmp_path / "enabled", web_publish_enabled=True))
    enabled_app.state.orchestrator.run = lambda _task_id: None
    with TestClient(enabled_app, base_url="http://testserver", client=("127.0.0.1", 50000)) as client:
        confirmation_required = client.post("/run", headers=_bearer(), json=task)
        required = confirmation_required.json()["detail"]["required_confirmation"]
        changed_task = {**task, "description": "changed after confirmation"}
        changed_rejected = client.post(
            "/run",
            headers={**_bearer(), "X-AI-Pipeline-Publish-Confirmation": required},
            json=changed_task,
        )
        accepted = client.post(
            "/run",
            headers={**_bearer(), "X-AI-Pipeline-Publish-Confirmation": required},
            json=task,
        )

    assert disabled.status_code == 403
    assert confirmation_required.status_code == 409
    assert required.startswith("PUBLISH:")
    assert changed_rejected.status_code == 409
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "queued"


def test_run_rejects_task_params_that_fail_preflight(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    app.state.orchestrator.run = lambda _task_id: None
    task = {
        "description": "unsafe direct run",
        "content_type": "anime",
        "publish": False,
        "params": {"script": "第一幕：安全测试。", "dry_run": True, "unexpected": "value"},
    }

    with TestClient(app, base_url="http://testserver", client=("127.0.0.1", 50000)) as client:
        response = client.post("/run", headers=_bearer(), json=task)
        jobs = client.get("/jobs", headers=_bearer())

    assert response.status_code == 422
    assert response.json()["detail"] == {
        "code": "task_validation_failed",
        "errors": [{"field": "unexpected", "message": "Extra inputs are not permitted", "type": "extra_forbidden"}],
    }
    assert jobs.json() == {"jobs": []}


def test_session_publish_requires_recent_login(tmp_path: Path, monkeypatch) -> None:
    app = create_app(_settings(tmp_path, web_publish_enabled=True, web_publish_reauth_seconds=60))
    app.state.orchestrator.run = lambda _task_id: None
    task = TaskInput(
        description="session publish",
        content_type="anime",
        publish=True,
        params={"script": "第一幕：会话发布确认。", "dry_run": True},
    )
    confirmation = f"PUBLISH:{task_fingerprint(task)}"

    with TestClient(app, base_url="http://testserver", client=("127.0.0.1", 50000)) as client:
        login = client.post("/auth/login", json={"token": ADMIN_TOKEN})
        csrf = login.json()["csrf_token"]
        now = time.time()
        monkeypatch.setattr(security_module.time, "time", lambda: now + 61)
        response = client.post(
            "/run",
            headers={
                "X-CSRF-Token": csrf,
                "X-AI-Pipeline-Publish-Confirmation": confirmation,
            },
            json=task.model_dump(mode="json"),
        )

    assert response.status_code == 401
    assert response.json()["detail"] == {
        "code": "recent_authentication_required",
        "message": "recent authentication required for publishing",
    }


def test_artifact_endpoint_serves_only_registered_files_inside_job(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    store = app.state.orchestrator.store
    snapshot = store.create(TaskInput(description="artifact test", content_type="anime"))
    job_video = store.job_dir(snapshot.task_id) / "video" / "preview.mp4"
    job_video.write_bytes(b"video-bytes")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    snapshot.artifacts.video = job_video
    snapshot.artifacts.source_document = outside
    snapshot.artifacts.manifest_path = store.status_path(snapshot.task_id)
    snapshot.artifacts.external_status_path = store.events_path(snapshot.task_id)
    store.save(snapshot)

    with TestClient(app, base_url="http://testserver", client=("127.0.0.1", 50000)) as client:
        listing = client.get(f"/jobs/{snapshot.task_id}/artifacts", headers=_bearer())
        artifacts = listing.json()["artifacts"]
        downloaded = client.get(artifacts[0]["url"], headers=_bearer())
        invalid_id = client.get(f"/jobs/{snapshot.task_id}/artifacts/../../outside.txt", headers=_bearer())

    assert listing.status_code == 200
    assert len(artifacts) == 1
    assert {artifact["relative_path"] for artifact in artifacts} == {"video/preview.mp4"}
    assert downloaded.content == b"video-bytes"
    assert invalid_id.status_code in {404, 422}


def test_metadata_audit_is_downloadable_but_not_embedded_in_job_api_responses(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    store = app.state.orchestrator.store
    snapshot = store.create(TaskInput(description="metadata audit", content_type="anime"))
    audit_path = store.job_dir(snapshot.task_id) / "metadata_audit.json"
    audit_body = '{"facts":["private fact"],"attempts":[{"text":"candidate","prompt_sha256":"fingerprint"}]}'
    audit_path.write_text(audit_body, encoding="utf-8")
    snapshot.artifacts.metadata_audit_path = audit_path
    snapshot.artifacts.metadata_audit = MetadataAuditSummary(
        status="failed",
        title=MetadataStageAuditSummary(
            status="failed", generation_mode="llm", quality_passed=False, error_codes=["invalid_json_contract"]
        ),
        description=MetadataStageAuditSummary(status="not_started"),
    )
    store.save(snapshot)

    with TestClient(app, base_url="http://testserver", client=("127.0.0.1", 50000)) as client:
        responses = [
            client.get(f"/status/{snapshot.task_id}", headers=_bearer()),
            client.get(f"/jobs/{snapshot.task_id}", headers=_bearer()),
            client.get("/jobs", headers=_bearer()),
        ]
        listing = client.get(f"/jobs/{snapshot.task_id}/artifacts", headers=_bearer())
        artifact = next(item for item in listing.json()["artifacts"] if item["relative_path"] == "metadata_audit.json")
        downloaded = client.get(artifact["url"], headers=_bearer())

    for response in responses:
        assert response.status_code == 200
        assert "private fact" not in response.text
        assert "candidate" not in response.text
        assert "fingerprint" not in response.text
    assert downloaded.text == audit_body


def test_artifact_endpoint_rejects_symlinked_job_directory(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    store = app.state.orchestrator.store
    task_id = "0" * 32
    outside = tmp_path / "outside-job"
    outside.mkdir()
    video = outside / "preview.mp4"
    video.write_bytes(b"outside-video")
    snapshot = JobSnapshot(
        task_id=task_id,
        status=JobStatus.SUCCEEDED,
        task=TaskInput(description="外部工件"),
        artifacts=ArtifactSet(video=video),
    )
    (outside / "status.json").write_text(snapshot.model_dump_json(), encoding="utf-8")
    (outside / "events.jsonl").write_text('{"event":"outside"}\n', encoding="utf-8")
    job_dir = store.jobs_dir / task_id
    job_dir.symlink_to(outside, target_is_directory=True)

    with TestClient(app, base_url="http://testserver", client=("127.0.0.1", 50000)) as client:
        status = client.get(f"/status/{task_id}", headers=_bearer())
        details = client.get(f"/jobs/{task_id}", headers=_bearer())
        readiness = client.get(f"/jobs/{task_id}/publish-readiness", headers=_bearer())
        events = client.get(f"/jobs/{task_id}/events", headers=_bearer())
        listing = client.get(f"/jobs/{task_id}/artifacts", headers=_bearer())
        downloaded = client.get(f"/jobs/{task_id}/artifacts/{'1' * 24}", headers=_bearer())

    for response in (status, details, readiness, events, listing, downloaded):
        assert response.status_code == 404
        assert response.json()["detail"] == "task not found"
    assert registered_artifacts(snapshot, job_dir) == {}


def test_session_crud_writes_require_and_accept_csrf(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    snapshot = app.state.orchestrator.store.create(TaskInput(description="CSRF CRUD 测试"))
    app.state.orchestrator.store.finish(snapshot.task_id, status=JobStatus.FAILED, error="test terminal status")

    with TestClient(app, base_url="http://testserver", client=("127.0.0.1", 50000)) as client:
        login = client.post("/auth/login", json={"token": ADMIN_TOKEN})
        csrf = login.json()["csrf_token"]
        missing = client.patch(f"/jobs/{snapshot.task_id}", json={"display_name": "新名称"})
        renamed = client.patch(
            f"/jobs/{snapshot.task_id}",
            headers={"X-CSRF-Token": csrf},
            json={"display_name": "新名称"},
        )
        deleted = client.delete(f"/jobs/{snapshot.task_id}", headers={"X-CSRF-Token": csrf})

    assert missing.status_code == 403
    assert missing.json()["error"]["code"] == "csrf_failed"
    assert renamed.status_code == 200
    assert renamed.json()["display_name"] == "新名称"
    assert deleted.status_code == 200


def test_job_path_ids_reject_invalid_values_before_store_access(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    valid_task_id = "0" * 32

    with TestClient(app, base_url="http://testserver", client=("127.0.0.1", 50000)) as client:
        status_uppercase = client.get(f"/status/{'A' * 32}", headers=_bearer())
        job_short = client.get("/jobs/abc", headers=_bearer())
        rename_traversal = client.patch(
            "/jobs/../outside",
            headers=_bearer(),
            json={"display_name": "unsafe"},
        )
        delete_with_spaces = client.delete(f"/jobs/%20{valid_task_id}%20", headers=_bearer())
        readiness_uppercase = client.get(f"/jobs/{'A' * 32}/publish-readiness", headers=_bearer())
        publish_short = client.post(f"/jobs/{'1' * 31}/publish", headers=_bearer(), json={"targets": []})
        events_traversal = client.get("/jobs/../../outside/events", headers=_bearer())
        artifacts_bad_task = client.get(f"/jobs/{valid_task_id[:-1]}g/artifacts", headers=_bearer())
        artifact_bad_id = client.get(f"/jobs/{valid_task_id}/artifacts/{'1' * 23}g", headers=_bearer())

    assert status_uppercase.status_code == 422
    assert job_short.status_code == 422
    assert rename_traversal.status_code in {404, 422}
    assert delete_with_spaces.status_code == 422
    assert readiness_uppercase.status_code == 422
    assert publish_short.status_code == 422
    assert events_traversal.status_code in {404, 422}
    assert artifacts_bad_task.status_code == 422
    assert artifact_bad_id.status_code == 422


def test_web_query_limits_reject_non_positive_values(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    snapshot = app.state.orchestrator.store.create(TaskInput(description="limit boundary"))
    app.state.orchestrator.store.event(snapshot.task_id, "custom_event", {"ok": True})

    with TestClient(app, base_url="http://testserver", client=("127.0.0.1", 50000)) as client:
        jobs_zero = client.get("/jobs?limit=0", headers=_bearer())
        jobs_negative = client.get("/jobs?limit=-1", headers=_bearer())
        events_zero = client.get(f"/jobs/{snapshot.task_id}/events?limit=0", headers=_bearer())
        events_negative = client.get(f"/jobs/{snapshot.task_id}/events?limit=-1", headers=_bearer())
        drafts_zero = client.get("/content/drafts?limit=0", headers=_bearer())
        drafts_negative = client.get("/content/drafts?limit=-1", headers=_bearer())

    assert jobs_zero.status_code == 422
    assert jobs_negative.status_code == 422
    assert events_zero.status_code == 422
    assert events_negative.status_code == 422
    assert drafts_zero.status_code == 422
    assert drafts_negative.status_code == 422


def test_job_events_reports_corrupt_event_log_without_path_leak(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    snapshot = app.state.orchestrator.store.create(TaskInput(description="corrupt events"))
    app.state.orchestrator.store.events_path(snapshot.task_id).write_text("{not-json\n", encoding="utf-8")

    with TestClient(app, base_url="http://testserver", client=("127.0.0.1", 50000)) as client:
        response = client.get(f"/jobs/{snapshot.task_id}/events", headers=_bearer())

    assert response.status_code == 500
    assert response.json()["detail"] == {"code": "job_events_invalid", "message": "job events are invalid"}
    assert str(app.state.orchestrator.store.jobs_dir) not in response.text
