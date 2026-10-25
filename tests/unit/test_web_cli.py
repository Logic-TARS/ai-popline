import importlib
import json
import sys
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from content_pipeline.job_store import JobStore
from content_pipeline.models import JobStatus, PipelineStep, TaskInput
from content_pipeline.settings import Settings

cli = importlib.import_module("content_pipeline.cli.main")
doctor = importlib.import_module("content_pipeline.cli.doctor")


def test_serve_rejects_wildcard_before_starting_uvicorn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["ai-pipeline", "serve", "--host", "0.0.0.0"])
    monkeypatch.setattr(cli, "load_settings", lambda: Settings(_env_file=None))

    with pytest.raises(SystemExit, match="wildcard listeners are not supported"):
        cli.main()


def test_serve_uses_guarded_uvicorn_options(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cert = tmp_path / "server.crt"
    key = tmp_path / "server.key"
    cert.write_text("certificate", encoding="utf-8")
    key.write_text("key", encoding="utf-8")
    settings = Settings(
        _env_file=None,
        web_bind_host="127.0.0.1",
        web_port=8765,
        web_tls_certfile=cert,
        web_tls_keyfile=key,
    )
    captured = {}

    def fake_run(app: str, **kwargs) -> None:
        captured["app"] = app
        captured.update(kwargs)

    monkeypatch.setattr(sys, "argv", ["ai-pipeline", "serve"])
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", fake_run)

    assert cli.main() == 0
    assert captured == {
        "app": "content_pipeline.api.app:app",
        "host": "127.0.0.1",
        "port": 8765,
        "ssl_certfile": str(cert),
        "ssl_keyfile": str(key),
        "proxy_headers": False,
        "server_header": False,
    }


def test_serve_container_wildcard_uses_guarded_uvicorn_options(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(
        _env_file=None,
        web_auth_required=True,
        web_admin_token="a" * 32,
        web_api_token="b" * 32,
        web_session_secret="c" * 32,
        web_allow_zerotier_http=True,
    )
    captured = {}

    def fake_run(app: str, **kwargs) -> None:
        captured["app"] = app
        captured.update(kwargs)

    monkeypatch.setattr(
        sys,
        "argv",
        ["ai-pipeline", "serve", "--host", "0.0.0.0", "--port", "8080", "--allow-container-wildcard"],
    )
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", fake_run)

    assert cli.main() == 0
    assert captured["app"] == "content_pipeline.api.app:app"
    assert captured["host"] == "0.0.0.0"
    assert captured["port"] == 8080
    assert captured["proxy_headers"] is False
    assert captured["server_header"] is False


def test_diagnostic_bundle_redacts_web_secrets_and_excludes_runtime_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "output"
    job_dir = data_dir / "jobs" / ("a" * 32)
    job_dir.mkdir(parents=True)
    (job_dir / "status.json").write_text('{"raw":"task state"}', encoding="utf-8")
    (job_dir / "events.jsonl").write_text('{"raw":"task event"}\n', encoding="utf-8")
    (job_dir / "poster.png").write_bytes(b"not-really-an-image")
    settings = Settings(
        _env_file=None,
        data_dir=data_dir,
        web_admin_token="admin-value-that-must-not-appear-123456",
        web_api_token="api-value-that-must-not-appear-12345678",
        web_session_secret="session-value-that-must-not-appear-1234",
    )
    bundle = tmp_path / "diagnostics.zip"
    monkeypatch.setattr(doctor, "load_settings", lambda: settings)

    assert doctor.run_doctor_bundle(bundle) == 0
    with zipfile.ZipFile(bundle) as archive:
        assert archive.namelist() == ["diagnostics.txt"]
        diagnostics = archive.read("diagnostics.txt").decode("utf-8")

    assert "admin-value-that-must-not-appear" not in diagnostics
    assert "api-value-that-must-not-appear" not in diagnostics
    assert "session-value-that-must-not-appear" not in diagnostics
    assert "status.json" not in diagnostics
    assert "events.jsonl" not in diagnostics
    assert "poster.png" not in diagnostics
    assert "task state" not in diagnostics
    assert "task event" not in diagnostics
    assert "web_admin_token: **********" in diagnostics
    assert "web_api_token: **********" in diagnostics
    assert "web_session_secret: **********" in diagnostics
    assert doctor._redact_setting("browser_cookie", "cookie-value") == "**********"
    assert doctor._redact_setting("service_password", "password-value") == "**********"
    assert doctor._redact_setting("api_credential_file", "credential-value") == "**********"
    assert "--- External Tools ---" in diagnostics
    assert "gemini.ok:" in diagnostics
    assert "photo_process.ok:" in diagnostics
    assert "mpt.ok:" in diagnostics
    assert "sau.ok:" in diagnostics
    assert "--- Web Security ---" in diagnostics
    assert "bind_policy_ok:" in diagnostics
    assert "--- Warnings ---" in diagnostics


def test_doctor_bundle_cli_routes_to_bundle_writer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bundle = tmp_path / "handoff" / "diagnostics.zip"
    captured = {}

    def fake_run_doctor_bundle(output_path: Path) -> int:
        captured["output_path"] = output_path
        return 0

    monkeypatch.setattr(sys, "argv", ["ai-pipeline", "doctor", "--bundle", str(bundle)])
    monkeypatch.setattr(doctor, "run_doctor_bundle", fake_run_doctor_bundle)

    assert cli.main() == 0
    assert captured == {"output_path": bundle}


def test_serve_rejects_missing_tls_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    missing_cert = tmp_path / "missing.crt"
    key = tmp_path / "server.key"
    key.write_text("key", encoding="utf-8")
    settings = Settings(_env_file=None, web_tls_certfile=missing_cert, web_tls_keyfile=key)
    monkeypatch.setattr(sys, "argv", ["ai-pipeline", "serve"])
    monkeypatch.setattr(cli, "load_settings", lambda: settings)

    with pytest.raises(SystemExit, match="TLS certificate does not exist"):
        cli.main()


@pytest.mark.parametrize(
    "argv",
    [
        ["ai-pipeline", "jobs", "list", "--limit", "0"],
        ["ai-pipeline", "jobs", "events", "a" * 32, "--limit", "0"],
        ["ai-pipeline", "jobs", "cleanup", "--older-than-days", "0"],
        ["ai-pipeline", "jobs", "cleanup", "--older-than-days", "-1"],
        ["ai-pipeline", "jobs", "reap", "--older-than-hours", "0"],
        ["ai-pipeline", "jobs", "reap", "--older-than-hours", "-1"],
    ],
)
def test_jobs_commands_reject_non_positive_counts(argv: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", argv)

    with pytest.raises(SystemExit):
        cli.main()


def test_jobs_delete_cli_deletes_terminal_jobs_and_exits_nonzero_when_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = Settings(_env_file=None, data_dir=tmp_path / "output")
    store = JobStore(settings.data_dir)
    terminal = store.create(TaskInput(description="可删除任务")).task_id
    store.finish(terminal, JobStatus.SUCCEEDED)
    running = store.create(TaskInput(description="运行中任务")).task_id
    store.mark_running(running, PipelineStep.ROUTE)
    monkeypatch.setattr(cli, "load_settings", lambda: settings)

    monkeypatch.setattr(sys, "argv", ["ai-pipeline", "jobs", "delete", terminal, running])
    assert cli.main() == 1
    result = json.loads(capsys.readouterr().out)
    assert result == {"deleted": [terminal], "not_found": [], "blocked": [running], "failed": []}
    assert not store.job_dir(terminal).exists()

    monkeypatch.setattr(sys, "argv", ["ai-pipeline", "jobs", "delete", running])
    assert cli.main() == 1
    assert json.loads(capsys.readouterr().out)["blocked"] == [running]

    monkeypatch.setattr(sys, "argv", ["ai-pipeline", "jobs", "delete", terminal])
    assert cli.main() == 0
    assert json.loads(capsys.readouterr().out)["not_found"] == [terminal]


def test_jobs_reap_cli_fails_orphaned_jobs_so_they_become_deletable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = Settings(_env_file=None, data_dir=tmp_path / "output")
    store = JobStore(settings.data_dir)
    orphaned = store.create(TaskInput(description="僵死排队任务")).task_id
    active = store.create(TaskInput(description="活跃排队任务")).task_id
    status_path = store.status_path(orphaned)
    data = json.loads(status_path.read_text(encoding="utf-8"))
    data["updated_at"] = (datetime.now(UTC) - timedelta(hours=7)).isoformat()
    status_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(sys, "argv", ["ai-pipeline", "jobs", "reap"])

    assert cli.main() == 0

    assert json.loads(capsys.readouterr().out) == {"reaped": [orphaned], "skipped": [active]}
    assert store.get(orphaned).status is JobStatus.FAILED
    assert store.get(active).status is JobStatus.QUEUED

    monkeypatch.setattr(sys, "argv", ["ai-pipeline", "jobs", "delete", orphaned])
    assert cli.main() == 0
    assert json.loads(capsys.readouterr().out)["deleted"] == [orphaned]


@pytest.mark.parametrize(
    "argv",
    [
        ["ai-pipeline", "jobs", "show", "../outside"],
        ["ai-pipeline", "jobs", "show", "A" * 32],
        ["ai-pipeline", "jobs", "events", "../outside"],
        ["ai-pipeline", "jobs", "events", "A" * 32],
    ],
)
def test_jobs_commands_report_invalid_task_ids_as_not_found(
    argv: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(_env_file=None, data_dir=tmp_path / "output")
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(cli, "load_settings", lambda: settings)

    with pytest.raises(SystemExit, match="task not found"):
        cli.main()

    assert not (settings.data_dir / "outside").exists()


def test_jobs_events_reports_corrupt_event_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(_env_file=None, data_dir=tmp_path / "output")
    task_id = "a" * 32
    job_dir = settings.data_dir / "jobs" / task_id
    job_dir.mkdir(parents=True)
    (job_dir / "events.jsonl").write_text("{not-json\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["ai-pipeline", "jobs", "events", task_id])
    monkeypatch.setattr(cli, "load_settings", lambda: settings)

    with pytest.raises(SystemExit, match="invalid job events"):
        cli.main()


@pytest.mark.parametrize("command", ["run", "validate-task"])
def test_task_file_commands_report_missing_file(command: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    missing = tmp_path / "missing-task.json"
    monkeypatch.setattr(sys, "argv", ["ai-pipeline", command, "--task", str(missing)])

    with pytest.raises(SystemExit, match="task file does not exist"):
        cli.main()


@pytest.mark.parametrize("command", ["run", "validate-task"])
def test_task_file_commands_report_invalid_json(command: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    task = tmp_path / "task.json"
    task.write_text("{not-json", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["ai-pipeline", command, "--task", str(task)])

    with pytest.raises(SystemExit, match="invalid task file"):
        cli.main()


@pytest.mark.parametrize("command", ["run", "validate-task"])
def test_task_file_commands_reject_unknown_pipeline_params(
    command: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = tmp_path / "task.json"
    task.write_text(
        '{"description":"bad","content_type":"anime","params":{"script":"测试脚本","dry_run":true,"unexpected":"value"}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "argv", ["ai-pipeline", command, "--task", str(task)])

    with pytest.raises(SystemExit, match="invalid task file"):
        cli.main()


@pytest.mark.parametrize(
    "task_path",
    sorted(path for tier in ("dry-run", "local", "publish") for path in Path("examples/tasks", tier).glob("*.json")),
    ids=lambda path: path.as_posix(),
)
def test_validate_task_cli_accepts_current_packaged_examples(
    task_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["ai-pipeline", "validate-task", "--task", str(task_path)])

    assert cli.main() == 0

    output = capsys.readouterr().out
    assert '"description"' in output
    assert '"params"' in output
