from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from content_pipeline.account_status import collect_account_status
from content_pipeline.api.app import create_app
from content_pipeline.errors import ConfigError
from content_pipeline.models import RouteResult
from content_pipeline.pipeline_config import (
    apply_pipeline_defaults,
    load_effective_pipeline_defaults,
    save_pipeline_defaults_override,
)
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


def test_pipeline_defaults_override_enables_params_merge(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    before = load_effective_pipeline_defaults(settings)
    assert before.get("enabled") is False

    save_pipeline_defaults_override(
        settings,
        {"enabled": True, "pipelines": {"finance": {"params": {"script_writer": "rule", "date": "auto"}}}},
    )
    effective = load_effective_pipeline_defaults(settings)
    assert effective["enabled"] is True
    assert effective["pipelines"]["finance"]["params"]["script_writer"] == "rule"

    route = apply_pipeline_defaults(
        RouteResult(content_type="finance", topic="基金日报", params={"date": "20260904"}),
        settings=settings,
        task_id="a" * 32,
    )
    # explicit task params win; defaults fill the rest
    assert route.params == {"date": "20260904", "script_writer": "rule"}


def test_pipeline_defaults_override_disabled_by_default(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    route = RouteResult(content_type="finance", topic="基金日报", params={})
    assert apply_pipeline_defaults(route, settings=settings, task_id="a" * 32).params == {}


def test_pipeline_defaults_override_validation(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with pytest.raises(ConfigError):
        save_pipeline_defaults_override(
            settings,
            {"enabled": True, "pipelines": {"finance": {"params": ["not-a-dict"]}}},
        )


def test_pipeline_defaults_api_roundtrip(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    with TestClient(app, base_url="http://testserver", client=("127.0.0.1", 50000)) as client:
        initial = client.get("/pipeline-defaults", headers=_bearer())
        unknown = client.put(
            "/pipeline-defaults",
            headers=_bearer(),
            json={"enabled": True, "pipelines": {"not_a_pipeline": {"params": {}}}},
        )
        updated = client.put(
            "/pipeline-defaults",
            headers=_bearer(),
            json={"enabled": True, "pipelines": {"finance": {"params": {"script_writer": "rule"}}}},
        )
        reread = client.get("/pipeline-defaults", headers=_bearer())

    assert initial.status_code == 200
    assert initial.json()["enabled"] is False
    assert unknown.status_code == 422
    assert updated.status_code == 200
    assert reread.json()["enabled"] is True
    assert reread.json()["pipelines"]["finance"]["params"] == {"script_writer": "rule"}


def test_account_status_cookie_fallback(tmp_path: Path) -> None:
    sau_dir = tmp_path / "sau"
    cookies = sau_dir / "cookies"
    cookies.mkdir(parents=True)
    (cookies / "douyin_金融破壁人.json").write_text("{}", encoding="utf-8")
    (cookies / "bilibili_硅基思维TARS.json").write_text("{}", encoding="utf-8")
    (cookies / "notes.txt").write_text("ignore", encoding="utf-8")
    settings = _settings(tmp_path, sau_dir=sau_dir, sau_bridge_url="")

    status = collect_account_status(settings)

    assert status["source"] == "cookies"
    by_key = {(item["platform"], item["account"]): item for item in status["accounts"]}
    assert by_key[("douyin", "金融破壁人")]["has_cookie"] is True
    assert by_key[("douyin", "金融破壁人")]["cookie_modified"]
    assert by_key[("bilibili", "硅基思维TARS")]["has_cookie"] is True
    assert len(by_key) == 2


def test_account_status_api(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path, sau_bridge_url="", sau_dir=tmp_path / "empty-sau"))
    with TestClient(app, base_url="http://testserver", client=("127.0.0.1", 50000)) as client:
        response = client.get("/account-status", headers=_bearer())

    assert response.status_code == 200
    assert response.json()["source"] == "cookies"
    assert response.json()["accounts"] == []


CACHED_PATHS = (
    "src/content_pipeline/__pycache__",
    "scripts/__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".coverage",
)
KEPT_PATHS = (
    "src/content_pipeline/orchestrator.py",
    "build/lib/module.py",
    "dist/ai_pipeline-0.1.0.tar.gz",
    "output/jobs/job-1/__pycache__/worker.pyc",
    "data/logs/pipeline.log",
    ".venv/lib/site-packages/demo/__pycache__/demo.pyc",
)


def _cache_tree(root: Path) -> None:
    for relative in (
        "src/content_pipeline/__pycache__/pipeline.cpython-311.pyc",
        "scripts/__pycache__/clean_cache.cpython-311.pyc",
        ".pytest_cache/v/cache/lastfailed",
        ".ruff_cache/0.8.0/cache",
        ".coverage",
        "output/jobs/job-1/__pycache__/worker.pyc",
        "data/logs/pipeline.log",
        ".venv/lib/site-packages/demo/__pycache__/demo.pyc",
        *KEPT_PATHS,
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("payload", encoding="utf-8")


def test_cache_cleanup_api_scan_is_read_only(tmp_path: Path) -> None:
    _cache_tree(tmp_path)
    app = create_app(_settings(tmp_path, cache_cleanup_root=tmp_path))
    with TestClient(app, base_url="http://testserver", client=("127.0.0.1", 50000)) as client:
        response = client.post("/maintenance/cache-cleanup", headers=_bearer(), json={"apply": False})

    assert response.status_code == 200
    payload = response.json()
    assert payload["applied"] is False
    assert payload["total"] == len(CACHED_PATHS)
    assert {item["path"] for item in payload["items"]} == set(CACHED_PATHS)
    assert payload["failures"] == []
    assert (tmp_path / "src" / "content_pipeline" / "__pycache__").is_dir()
    # the console never receives absolute filesystem locations
    assert str(tmp_path) not in response.text


def test_cache_cleanup_api_apply_removes_only_caches(tmp_path: Path) -> None:
    _cache_tree(tmp_path)
    app = create_app(_settings(tmp_path, cache_cleanup_root=tmp_path))
    with TestClient(app, base_url="http://testserver", client=("127.0.0.1", 50000)) as client:
        response = client.post("/maintenance/cache-cleanup", headers=_bearer(), json={"apply": True})

    assert response.status_code == 200
    assert response.json()["applied"] is True
    assert response.json()["failures"] == []
    for relative in CACHED_PATHS:
        assert not (tmp_path / relative).exists()
    for relative in KEPT_PATHS:
        assert (tmp_path / relative).is_file()


def test_cache_cleanup_api_requires_csrf_for_session_writes(tmp_path: Path) -> None:
    _cache_tree(tmp_path)
    app = create_app(_settings(tmp_path, cache_cleanup_root=tmp_path))
    with TestClient(app, base_url="http://testserver", client=("127.0.0.1", 50000)) as client:
        login = client.post("/auth/login", json={"token": ADMIN_TOKEN})
        missing_csrf = client.post("/maintenance/cache-cleanup", json={"apply": True})
        allowed = client.post(
            "/maintenance/cache-cleanup",
            headers={"X-CSRF-Token": login.json()["csrf_token"]},
            json={"apply": True},
        )

    assert login.status_code == 200
    assert missing_csrf.status_code == 403
    assert missing_csrf.json()["error"]["code"] == "csrf_failed"
    assert allowed.status_code == 200
    assert allowed.json()["applied"] is True
    assert not (tmp_path / "src" / "content_pipeline" / "__pycache__").exists()


def test_cache_cleanup_api_rejects_extra_fields_and_unsafe_roots(tmp_path: Path) -> None:
    _cache_tree(tmp_path)
    protected_root = create_app(_settings(tmp_path, cache_cleanup_root=tmp_path / "output"))
    missing_root = create_app(_settings(tmp_path, cache_cleanup_root=tmp_path / "missing"))
    with TestClient(protected_root, base_url="http://testserver", client=("127.0.0.1", 50000)) as client:
        extra_field = client.post(
            "/maintenance/cache-cleanup", headers=_bearer(), json={"apply": False, "include_build": True}
        )
        protected = client.post("/maintenance/cache-cleanup", headers=_bearer(), json={"apply": False})
    with TestClient(missing_root, base_url="http://testserver", client=("127.0.0.1", 50000)) as client:
        missing = client.post("/maintenance/cache-cleanup", headers=_bearer(), json={"apply": False})

    assert extra_field.status_code == 422
    assert protected.status_code == 409
    assert protected.json()["detail"]["code"] == "cache_cleanup_unavailable"
    assert missing.status_code == 409
    assert str(tmp_path) not in protected.text + missing.text
    assert (tmp_path / "output" / "jobs" / "job-1" / "__pycache__" / "worker.pyc").is_file()
