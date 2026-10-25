from __future__ import annotations

import re
from pathlib import Path

import yaml

from content_pipeline.settings import Settings


def _read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def _env_example_keys() -> set[str]:
    keys: set[str] = set()
    for line in _read(".env.example").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        keys.add(stripped.split("=", maxsplit=1)[0])
    return keys


def _settings_env_names() -> set[str]:
    names: set[str] = set()
    for field_name, field_info in Settings.model_fields.items():
        alias = field_info.validation_alias
        choices = getattr(alias, "choices", None)
        if choices:
            names.update(str(choice) for choice in choices)
        elif isinstance(alias, str):
            names.add(alias)
        else:
            names.add(field_name.upper())
    return names


def test_production_dockerfile_starts_through_guarded_cli() -> None:
    dockerfile = _read("Dockerfile")

    assert dockerfile.startswith("FROM python:3.11-slim\n")
    assert 'CMD ["ai-pipeline", "serve"' in dockerfile
    assert '"--allow-container-wildcard"' in dockerfile
    assert "uvicorn" not in dockerfile.split("CMD", maxsplit=1)[-1]
    assert "--no-proxy-headers" not in dockerfile
    assert "--no-server-header" not in dockerfile
    assert re.search(r"^USER\s+appuser$", dockerfile, re.MULTILINE)
    assert "COPY pyproject.toml README.md ./" in dockerfile
    assert dockerfile.index("COPY pyproject.toml README.md ./") < dockerfile.index("RUN pip install --no-cache-dir .")
    assert "COPY config ./config" in dockerfile
    assert dockerfile.index("COPY config ./config") < dockerfile.index("RUN pip install --no-cache-dir .")
    assert "RUN mkdir -p /data && chown appuser:appuser /data" in dockerfile
    assert dockerfile.index("RUN mkdir -p /data") < dockerfile.index("USER appuser")
    assert "ENV PIPELINE_DATA_DIR=/data" in dockerfile
    assert dockerfile.index("USER appuser") < dockerfile.index("ENV PIPELINE_DATA_DIR=/data")
    assert "HEALTHCHECK" in dockerfile


def test_dev_dockerfile_includes_packaged_defaults_before_install() -> None:
    dockerfile = _read("Dockerfile.dev")

    assert dockerfile.startswith("FROM python:3.11-slim\n")
    assert "COPY pyproject.toml README.md ./" in dockerfile
    assert dockerfile.index("COPY pyproject.toml README.md ./") < dockerfile.index(
        'RUN pip install --no-cache-dir ".[test,dev]"'
    )
    assert "COPY config ./config" in dockerfile
    assert dockerfile.index("COPY config ./config") < dockerfile.index('RUN pip install --no-cache-dir ".[test,dev]"')
    assert "ENV PIPELINE_DATA_DIR=/data" in dockerfile


def test_dockerignore_excludes_sensitive_and_generated_context() -> None:
    ignored = {
        line.strip()
        for line in _read(".dockerignore").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    required_patterns = {
        ".env",
        ".env.*",
        "!.env.example",
        ".git",
        ".venv",
        "output",
        "data",
        "*.log",
        "diagnostics*.zip",
        "*.pem",
        "*.key",
        "*.crt",
        "*.egg-info",
        "__pycache__",
        "*.py[cod]",
        ".pytest_cache",
        ".ruff_cache",
    }
    assert required_patterns <= ignored


def test_gitignore_excludes_local_secrets_and_diagnostics() -> None:
    ignored = {
        line.strip() for line in _read(".gitignore").splitlines() if line.strip() and not line.lstrip().startswith("#")
    }

    required_patterns = {
        ".env",
        ".env.*",
        "!.env.example",
        "*.pem",
        "*.key",
        "*.crt",
        "diagnostics*.zip",
        "*.log",
        "output/",
        "data/",
        "dist/",
        "build/",
        ".pytest_cache/",
        ".ruff_cache/",
        ".mypy_cache/",
        "__pycache__/",
        "*.py[cod]",
    }
    assert required_patterns <= ignored


def test_env_example_keys_match_settings_environment_contract() -> None:
    env_keys = _env_example_keys()
    settings_names = _settings_env_names()

    assert env_keys <= settings_names
    assert {
        "ENV",
        "PIPELINE_DATA_DIR",
        "PIPELINE_DEFAULTS_FILE",
        "WEB_BIND_HOST",
        "WEB_AUTH_REQUIRED",
        "WEB_ADMIN_TOKEN",
        "WEB_API_TOKEN",
        "WEB_SESSION_SECRET",
        "WEB_PUBLISH_ENABLED",
        "WEB_ALLOW_ZEROTIER_HTTP",
        "SAU_BILIBILI_PRIVATE_ARGS",
    } <= env_keys


def test_env_example_loads_and_keeps_fail_closed_defaults() -> None:
    settings = Settings(_env_file=".env.example")

    assert settings.env == "development"
    assert settings.data_dir == Path("output")
    assert settings.pipeline_defaults_file == Path("config/pipeline.defaults.yaml")
    assert settings.web_bind_host == "127.0.0.1"
    assert settings.web_port == 8080
    assert settings.allowed_hosts == ("127.0.0.1", "localhost", "::1")
    assert settings.allowed_origins == ("http://127.0.0.1:8080", "http://localhost:8080")
    assert settings.web_auth_required is False
    assert settings.web_admin_token.get_secret_value() == ""
    assert settings.web_api_token.get_secret_value() == ""
    assert settings.web_session_secret.get_secret_value() == ""
    assert settings.web_publish_enabled is False
    assert settings.web_publish_reauth_seconds == 604800
    assert settings.web_allow_zerotier_http is False
    assert settings.sau_bilibili_private_args == "--is-only-self 1"


def test_compose_publishes_only_loopback_and_requires_authentication() -> None:
    compose = yaml.safe_load(_read("docker-compose.yml"))
    service = compose["services"]["ai-pipeline"]

    assert service["restart"] == "unless-stopped"
    assert service["init"] is True
    assert service["security_opt"] == ["no-new-privileges:true"]
    assert service["ports"] == ["127.0.0.1:${WEB_PORT:-8080}:8080"]
    environment = service["environment"]
    assert environment["PIPELINE_DATA_DIR"] == "/data"
    assert environment["WEB_AUTH_REQUIRED"] == "true"
    assert environment["WEB_ALLOW_ZEROTIER_HTTP"] == "true"
    assert environment["WEB_ALLOWED_NETWORKS"] == "172.16.0.0/12"
    assert "WEB_PUBLISH_ENABLED" not in environment

    volumes = service["volumes"]
    assert "./profiles:/app/profiles:ro" in volumes
    assert all(not volume.startswith("./output:/data:ro") for volume in volumes)

    healthcheck = service["healthcheck"]
    assert healthcheck["test"] == [
        "CMD",
        "python",
        "-c",
        "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health')",
    ]
    assert healthcheck["interval"] == "30s"
    assert healthcheck["timeout"] == "3s"
    assert healthcheck["retries"] == 3
    assert healthcheck["start_period"] == "10s"


def test_container_storage_contract_is_documented() -> None:
    docs = _read("docs/configuration.md")

    assert "## Container runtime storage" in docs
    assert "PIPELINE_DATA_DIR=/data" in docs
    assert "host `.env` cannot redirect container job state" in docs
    assert "production `Dockerfile` runs as the non-root `appuser`" in docs
    assert "copies `config/pipeline.defaults.yaml` into the image" in docs
    assert "restart: unless-stopped" in docs
    assert "init: true" in docs
    assert "no-new-privileges:true" in docs
    assert "WEB_PORT" in docs
    assert '"127.0.0.1:${WEB_PORT:-8080}:8080"' in docs
    assert "./output:/data" in docs
    assert "docker run --rm" in docs
    assert "-e WEB_AUTH_REQUIRED=true" in docs
    assert '-e WEB_ADMIN_TOKEN="$WEB_ADMIN_TOKEN"' in docs
    assert '-e WEB_API_TOKEN="$WEB_API_TOKEN"' in docs
    assert '-e WEB_SESSION_SECRET="$WEB_SESSION_SECRET"' in docs
    assert "must be independent random values with at least 32 characters" in docs
    assert "Do not mount `/data` read-only" in docs


def test_readme_documents_production_delivery_contract() -> None:
    readme = _read("README.md")

    assert "uv sync --extra test --extra dev" in readme
    assert "The quality gate checks formatting, Ruff lint, non-external/non-publish tests" in readme
    assert "sdist and wheel contents" in readme
    assert "dependency compatibility with `uv pip check`" in readme
    assert "installed-wheel CLI smoke test" in readme
    assert "isolated wheel smoke" in readme
    assert "loads packaged `config/pipeline.defaults.yaml` without the repository checkout" in readme
    assert "ai-pipeline-dist" in readme
    assert "retained for 14 days" in readme
    assert "pushes to `main`, pull requests, and manual `workflow_dispatch` runs" in readme
    assert "before the production Docker image build" in readme
    assert "Dockerfile` starts through `ai-pipeline serve --allow-container-wildcard" in readme
    assert "runs as non-root `appuser`" in readme
    assert "copies `config/pipeline.defaults.yaml` into the image" in readme
    assert "PIPELINE_DATA_DIR=/data" in readme
    assert "mounts job output at `./output:/data`" in readme
    assert "checks `/health` with a service healthcheck" in readme
    assert "Direct `docker run` deployments must pass `WEB_AUTH_REQUIRED=true`" in readme
    assert "container wildcard listener fails closed without application authentication" in readme
    assert "values with at least 32 characters each" in readme
    assert "Do not mount `/data` read-only" in readme
    assert "ai-pipeline jobs cleanup --older-than-days N" in readme
    assert "Cleanup skips `queued` and `running` jobs" in readme
    assert "skips jobs with an active deferred publication attempt" in readme


def test_publishing_runbook_documents_release_gate_and_evidence() -> None:
    runbook = _read("docs/publishing-runbook.md")

    assert "## Release-gate checklist" in runbook
    assert "bash scripts/check.sh" in runbook
    assert "non-external/non-publish tests" in runbook
    assert "sdist/wheel content checks" in runbook
    assert "`uv pip check` after normal and isolated wheel installation" in runbook
    assert "installed-wheel CLI smoke test" in runbook
    assert "isolated wheel smoke" in runbook
    assert "loads packaged `config/pipeline.defaults.yaml` without the repository checkout" in runbook
    assert "dist/check/" in runbook
    assert "ai-pipeline-dist" in runbook
    assert "retained for 14 days" in runbook
    assert "workflow run that completed the Bash quality gate" in runbook
    assert "before the production Docker image build" in runbook
    assert "Confirm the intended license with the project owner before release" in runbook
    assert "do not infer or ship unapproved license metadata" in runbook
    assert "ai-pipeline capabilities" in runbook
    assert "ai-pipeline serve" in runbook
    assert "raw non-loopback Uvicorn command" in runbook
    assert "WEB_ADMIN_TOKEN" in runbook
    assert "WEB_API_TOKEN" in runbook
    assert "WEB_SESSION_SECRET" in runbook
    assert "## Dry-run review" in runbook
    assert "ai-pipeline validate-task --task path/to/task.json" in runbook
    assert "ai-pipeline jobs events <task_id>" in runbook
    assert "`artifacts.validation` proves media decodability" in runbook
    assert "## Publish execution" in runbook
    assert "WEB_PUBLISH_ENABLED=true" in runbook
    assert "Archive `status.json`, `events.jsonl`, generated artifacts, and platform proof" in runbook
    assert "`artifacts.publish_results` records each requested platform target" in runbook
    assert "never treat missing proof as success" in runbook


def test_web_access_boundary_documents_production_acceptance_and_recovery() -> None:
    docs = _read("docs/web-access-boundary.md")

    assert "## Production acceptance checklist" in docs
    assert "rejects wildcard host binds outside the container-only `--allow-container-wildcard` path" in docs
    assert "WEB_AUTH_REQUIRED=true" in docs
    assert "independent `WEB_ADMIN_TOKEN`, `WEB_API_TOKEN`, and `WEB_SESSION_SECRET`" in docs
    assert "WEB_ALLOWED_NETWORKS" in docs
    assert "WEB_ALLOWED_HOSTS" in docs
    assert "WEB_ALLOWED_ORIGINS" in docs
    assert "WEB_TLS_CERTFILE" in docs
    assert "WEB_TLS_KEYFILE" in docs
    assert "WEB_ALLOW_ZEROTIER_HTTP=true" in docs
    assert "Windows Firewall allows the chosen TCP port only on the ZeroTier local address" in docs
    assert "`/health` returns only minimal liveness" in docs
    assert "Browser write requests enforce CSRF and Host/Origin checks" in docs
    assert "Authorization: Bearer <WEB_API_TOKEN>" in docs
    assert "WEB_PUBLISH_ENABLED=false" in docs
    assert "MCP, Photo-Process, desktop browser helper, MPT, SAU, Gemini" in docs
    assert "## Runtime failure and recovery" in docs
    assert "Prefer fail-closed recovery: stop `ai-pipeline serve`" in docs
    assert "do not expose raw lower-tool ports to debug remotely" in docs
    assert "pipeline_defaults_available" in docs
    assert "rotate all three Web secrets" in docs
    assert "verify `status.json`, `events.jsonl`, `artifacts.validation`, and `artifacts.publish_results`" in docs


def test_configuration_docs_cover_production_baseline_and_diagnostics() -> None:
    docs = _read("docs/configuration.md")

    assert "ai-pipeline doctor --bundle output\\diagnostics.zip" in docs
    assert "redacts fields whose names contain `token`, `secret`, `key`, `password`, `credential`, or `cookie`" in docs
    assert "It contains `diagnostics.txt` only" in docs
    assert "must not include `.env` contents, task JSON, `status.json`, `events.jsonl`" in docs
    assert "media artifacts, browser cookies, or raw lower-tool logs" in docs
    assert "## Production configuration baseline" in docs
    assert "Use `.env.example` as a template, not as a deployable configuration" in docs
    assert "never commit `.env`, `.env.*`, TLS private keys, or diagnostic bundles" in docs
    assert "Set `PIPELINE_DATA_DIR` to a durable writable directory" in docs
    assert "GEMINI_SKILL_DIR" in docs
    assert "PHOTO_PROCESS_DIR" in docs
    assert "MPT_DIR" in docs
    assert "SAU_DIR" in docs
    assert "FINANCE_MD_DIR" in docs
    assert "AI_BRIEFING_DIR" in docs
    assert "Keep `WEB_PUBLISH_ENABLED=false` until generated artifacts have been reviewed" in docs
    assert "production mode fails closed even on loopback if authentication is disabled" in docs
    assert "three independent random secrets" in docs
    assert "Prefer TLS with `WEB_TLS_CERTFILE` and `WEB_TLS_KEYFILE`" in docs
    assert "WEB_ALLOW_ZEROTIER_HTTP=true" in docs
    assert "Keep `SAU_BILIBILI_PRIVATE_ARGS=--is-only-self 1`" in docs


def test_smoke_testing_docs_cover_external_tool_troubleshooting() -> None:
    docs = _read("docs/smoke-testing.md")

    assert "## Production quality gate" in docs
    assert "bash scripts/check.sh" in docs
    assert "./scripts/check.ps1" in docs
    assert "non-external/non-publish tests" in docs
    assert "packaged Web static JavaScript syntax with `node --check`" in docs
    assert "sdist and wheel contents" in docs
    assert "runs `uv pip check`" in docs
    assert "installed-wheel CLI smoke tests" in docs
    assert "temporary isolated project" in docs
    assert "runs `uv pip check` there" in docs
    assert "verifies `config/pipeline.defaults.yaml` loads from the packaged fallback" in docs
    assert "removes the temporary isolated project even when a smoke step fails" in docs
    assert "pushes to `main`, pull requests, and manual `workflow_dispatch` runs" in docs
    assert "Python 3.11 and Node.js 24" in docs
    assert "before building the production Docker image" in docs
    assert "uploaded as `ai-pipeline-dist`" in docs
    assert "do not substitute an unchecked local build" in docs
    assert "ai-pipeline capabilities" in docs
    assert "ai-pipeline validate-task --task examples/tasks/dry-run/task.example.json" in docs
    assert "## External tool troubleshooting" in docs
    assert "ai-pipeline doctor --bundle output/diagnostics.zip" in docs
    assert "must not include `.env`, task JSON, browser cookies, media" in docs
    assert "ai-pipeline jobs show <task_id>" in docs
    assert "ai-pipeline jobs events <task_id>" in docs
    assert "Do not enable\n`RUN_PUBLISH_TESTS=1` while diagnosing generation-only failures" in docs
    assert "artifacts.validation" in docs
    assert "Gemini MCP / `gemini-skill`" in docs
    assert "MCP exits early" in docs
    assert "Photo-Process / browser worker" in docs
    assert "AUTH_REQUIRED" in docs
    assert "BROWSER_BUSY" in docs
    assert "GEM_ACCESS_FAILED" in docs
    assert "UI_CHANGED" in docs
    assert "never treat a visible browser as success" in docs
    assert "MoneyPrinterTurbo (MPT)" in docs
    assert "subtitle looks like a file path" in docs
    assert "require video decode, audio, and subtitle validation" in docs
    assert "SAU uploaders" in docs
    assert "PRIVATE_VISIBILITY_UNSUPPORTED" in docs
    assert "ALREADY_PUBLISHED" in docs
    assert "SAU_BILIBILI_PRIVATE_ARGS=--is-only-self 1" in docs
    assert "require Douyin/Kuaishou visibility proof" in docs
    assert "never treat missing proof as success" in docs


def test_documentation_index_points_to_production_handoff_guides() -> None:
    docs = _read("docs/README.md")

    assert "[Smoke Testing](smoke-testing.md)" in docs
    assert "production quality gate" in docs
    assert "installed-wheel smoke tests" in docs
    assert "external tool troubleshooting" in docs
    assert "[Publishing Runbook](publishing-runbook.md)" in docs
    assert "release-gate checklist" in docs
    assert "dry-run review" in docs


def test_core_docs_document_production_handoff_boundary() -> None:
    architecture = _read("docs/architecture.md")
    agent_interface = _read("docs/agent-interface.md")
    web_console = _read("docs/web-console.md")

    assert "## Production Handoff Boundary" in architecture
    assert "Production delivery is gated by `bash scripts/check.sh`" in architecture
    assert "dependency compatibility through `uv pip check`" in architecture
    assert "installed-wheel CLI smoke tests" in architecture
    assert "isolated wheel smoke" in architecture
    assert "guarded `ai-pipeline serve` entry point" in architecture
    assert "local-only adapter boundary" in architecture

    assert "Before handing an agent integration to operations" in agent_interface
    assert "the production quality gate (`bash scripts/check.sh`)" in agent_interface
    assert "installed-wheel CLI smoke tests" in agent_interface
    assert "isolated wheel smoke tests" in agent_interface
    assert "pipeline_defaults_available" in agent_interface

    assert "Production handoff should use the packaged `ai-pipeline serve` entry point" in web_console
    assert "package, dependency, installed-wheel, and isolated-wheel smoke gates" in web_console
