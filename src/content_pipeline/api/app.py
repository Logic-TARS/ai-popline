from __future__ import annotations

import json
import logging
import secrets
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Literal
from urllib.parse import quote
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from content_pipeline.account_status import collect_account_status
from content_pipeline.api.artifacts import registered_artifacts
from content_pipeline.api.content import build_content_router
from content_pipeline.api.security import WebSecurity
from content_pipeline.api.ui_schema import (
    build_ui_pipelines,
    sanitized_error_items,
    sanitized_validation_errors,
    validate_task_for_ui,
)
from content_pipeline.content_studio import ContentStudioService
from content_pipeline.deferred_publishing import (
    PublicationEligibilityError,
    list_publish_account_options,
    publication_readiness,
    publication_summary,
    run_deferred_publication,
    validate_deferred_publish_request,
)
from content_pipeline.errors import ConfigError
from content_pipeline.job_store import JobDeleteConflictError, utc_now
from content_pipeline.maintenance import cleanup_cache
from content_pipeline.models import (
    DeferredPublishInput,
    JobStatus,
    PipelineStep,
    PublicationAttempt,
    TaskInput,
    deferred_publish_fingerprint,
    task_fingerprint,
)
from content_pipeline.orchestrator import Orchestrator
from content_pipeline.pipeline_config import load_effective_pipeline_defaults, save_pipeline_defaults_override
from content_pipeline.pipelines.registry import list_pipelines
from content_pipeline.publish_policy import (
    SUPPORTED_POLICY_PLATFORMS,
    load_publish_policy,
    save_publish_policy,
)
from content_pipeline.scheduler import ScheduledJob, ScheduleRunner, ScheduleStore, compute_next_run
from content_pipeline.settings import Settings, load_settings
from content_pipeline.web import WEB_STATIC_DIR

__all__ = ["create_app"]

_logger = logging.getLogger(__name__)


class LoginInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=1, max_length=256)

    @field_validator("token")
    @classmethod
    def normalize_token(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("token cannot be blank")
        return normalized


class DeleteJobsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_ids: list[str] = Field(min_length=1, max_length=100)

    @field_validator("task_ids")
    @classmethod
    def validate_task_ids(cls, values: list[str]) -> list[str]:
        result: list[str] = []
        for value in values:
            task_id = value.strip()
            if len(task_id) != 32 or any(character not in "0123456789abcdef" for character in task_id):
                raise ValueError("task_ids must contain 32-character lowercase hexadecimal ids")
            if task_id not in result:
                result.append(task_id)
        return result


class PublishPolicyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy: dict[str, Any]


class PipelineDefaultsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    pipelines: dict[str, Any] = Field(default_factory=dict)


class CacheCleanupInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    apply: bool = False


class RenameJobInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=120)

    @field_validator("display_name")
    @classmethod
    def normalize_display_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("display_name cannot be blank")
        return normalized


class ScheduleCreateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    task: TaskInput
    frequency: Literal["daily", "weekly"]
    time_of_day: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    weekdays: list[int] = Field(default_factory=list, max_length=7)
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("name cannot be blank")
        return normalized


class ScheduleUpdateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    enabled: bool | None = None
    frequency: Literal["daily", "weekly"] | None = None
    time_of_day: str | None = Field(default=None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    weekdays: list[int] | None = Field(default=None, max_length=7)
    task: TaskInput | None = None


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create the FastAPI application with isolated settings and executor state."""
    resolved_settings = settings or load_settings()
    orchestrator = Orchestrator(settings=resolved_settings)
    executor = ThreadPoolExecutor(max_workers=2)
    security = WebSecurity(resolved_settings)
    content_studio = ContentStudioService(resolved_settings)
    schedule_store = ScheduleStore(resolved_settings.data_dir)

    def submit_scheduled_run(task: TaskInput) -> str:
        task_id = orchestrator.submit(task)
        executor.submit(orchestrator.run, task_id)
        return task_id

    schedule_runner = ScheduleRunner(schedule_store, submit_scheduled_run)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = resolved_settings
        app.state.orchestrator = orchestrator
        app.state.executor = executor
        app.state.security = security
        app.state.content_studio = content_studio
        app.state.schedule_store = schedule_store
        app.state.schedule_runner = schedule_runner
        schedule_runner.start()
        try:
            reaped = orchestrator.store.reap_orphaned_jobs()
        except Exception:
            _logger.warning("orphaned job reconciliation failed", exc_info=True)
        else:
            if reaped["reaped"]:
                _logger.info("reaped %d orphaned jobs: %s", len(reaped["reaped"]), ", ".join(reaped["reaped"]))
        try:
            yield
        finally:
            schedule_runner.stop()
            content_studio.shutdown()
            executor.shutdown(wait=False, cancel_futures=True)

    app = FastAPI(title="AI Pipeline", lifespan=lifespan)
    app.state.settings = resolved_settings
    app.state.orchestrator = orchestrator
    app.state.executor = executor
    app.state.security = security
    app.state.content_studio = content_studio
    app.state.schedule_store = schedule_store
    app.state.schedule_runner = schedule_runner
    app.include_router(build_content_router(content_studio, orchestrator, executor, resolved_settings))
    app.mount("/static", StaticFiles(directory=WEB_STATIC_DIR), name="static")

    @app.exception_handler(RequestValidationError)
    async def sanitized_request_validation(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "detail": {
                    "code": "request_validation_failed",
                    "errors": sanitized_error_items(exc.errors()),
                }
            },
        )

    @app.middleware("http")
    async def enforce_web_boundary(request: Request, call_next):
        rejection = security.enforce(request)
        if rejection is not None:
            return security.add_security_headers(request, rejection)
        response = await call_next(request)
        return security.add_security_headers(request, response)

    @app.get("/", include_in_schema=False)
    def web_console() -> FileResponse:
        return FileResponse(WEB_STATIC_DIR / "index.html", media_type="text/html")

    @app.get("/health", tags=["system"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready", tags=["system"])
    def ready() -> JSONResponse:
        checks = {
            "data_dir_available": resolved_settings.data_dir.is_dir(),
            "profiles_available": resolved_settings.profiles_dir.is_dir(),
            "pipeline_defaults_available": resolved_settings.pipeline_defaults_file.is_file(),
        }
        is_ready = all(checks.values())
        return JSONResponse(
            status_code=200 if is_ready else 503,
            content={"status": "ready" if is_ready else "not_ready", **checks},
        )

    @app.get("/monitor/uptime-kuma", tags=["system"])
    def uptime_kuma_monitor() -> dict[str, Any]:
        """Minimal unauthenticated liveness payload for Uptime Kuma HTTP(s) monitors."""
        return {
            "ok": True,
            "service": "ai-pipeline",
            "status": "up",
            "app": "AI Pipeline",
        }

    @app.post("/auth/login", tags=["authentication"])
    def login(credentials: LoginInput, request: Request) -> JSONResponse:
        if not resolved_settings.web_auth_required:
            raise HTTPException(status_code=404, detail="authentication is not enabled")
        if len(resolved_settings.web_session_secret.get_secret_value()) < 32:
            raise HTTPException(status_code=503, detail="session authentication is not configured")
        peer = request.client.host if request.client else "unknown"
        if not security.verify_login(peer, credentials.token):
            raise HTTPException(status_code=401, detail="invalid credentials")
        cookie, session = security.new_session()
        response = JSONResponse({"authenticated": True, "csrf_token": session.csrf_token})
        security.set_session_cookies(response, cookie, session, secure=request.url.scheme == "https")
        return response

    @app.post("/auth/logout", tags=["authentication"])
    def logout() -> JSONResponse:
        response = JSONResponse({"authenticated": False})
        security.clear_session_cookies(response)
        return response

    @app.get("/auth/me", tags=["authentication"])
    def current_user(request: Request) -> dict[str, Any]:
        return {"authenticated": True, "method": request.state.auth_kind}

    @app.get("/capabilities", tags=["system"])
    def capabilities() -> dict[str, Any]:
        return {"pipelines": [pipeline.__dict__ for pipeline in list_pipelines()]}

    @app.get("/ui/bootstrap", tags=["web"])
    def ui_bootstrap() -> dict[str, Any]:
        return {
            "app_name": "AI Pipeline",
            "api_contract_version": 2,
            "features": {
                "content_draft_crud": True,
                "content_draft_bulk_delete": True,
                "job_rename": True,
                "job_delete": True,
                "job_bulk_delete": True,
                "task_schedules": True,
            },
            "auth_required": resolved_settings.web_auth_required,
            "publish_enabled": resolved_settings.web_publish_enabled,
            "active_refresh_ms": 2500,
            "idle_refresh_ms": 15000,
            "statuses": [status.value for status in JobStatus],
            "steps": [step.value for step in PipelineStep],
            "pipelines": build_ui_pipelines(resolved_settings),
        }

    @app.post("/validate-task", tags=["jobs"])
    def validate_task(task: TaskInput) -> dict[str, Any]:
        if task.publish and not resolved_settings.web_publish_enabled:
            raise HTTPException(status_code=403, detail="web publishing is disabled")
        try:
            normalized, warnings = validate_task_for_ui(task)
        except ValidationError as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": "task_validation_failed", "errors": sanitized_validation_errors(exc)},
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": "task_validation_failed", "errors": [{"field": "task", "message": str(exc)}]},
            ) from exc
        return {
            "valid": True,
            "task": normalized.model_dump(mode="json"),
            "task_fingerprint": task_fingerprint(normalized),
            "warnings": warnings,
        }

    @app.post("/run", tags=["jobs"])
    def run_task(
        task: TaskInput,
        request: Request,
        publish_confirmation: str | None = Header(default=None, alias="X-AI-Pipeline-Publish-Confirmation"),
    ) -> dict[str, str]:
        if task.publish and not resolved_settings.web_publish_enabled:
            raise HTTPException(status_code=403, detail="web publishing is disabled")
        try:
            normalized, _warnings = validate_task_for_ui(task)
        except ValidationError as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": "task_validation_failed", "errors": sanitized_validation_errors(exc)},
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": "task_validation_failed", "errors": [{"field": "task", "message": str(exc)}]},
            ) from exc

        if normalized.publish:
            fingerprint = task_fingerprint(normalized)
            expected = f"PUBLISH:{fingerprint}"
            if not publish_confirmation or not secrets.compare_digest(publish_confirmation, expected):
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "publish_confirmation_required",
                        "task_fingerprint": fingerprint,
                        "required_confirmation": expected,
                    },
                )
            if not security.session_is_recent(request):
                raise HTTPException(
                    status_code=401,
                    detail={
                        "code": "recent_authentication_required",
                        "message": "recent authentication required for publishing",
                    },
                )
        task_id = orchestrator.submit(normalized)
        executor.submit(orchestrator.run, task_id)
        return {"task_id": task_id, "status": "queued"}

    @app.get("/status/{task_id}", tags=["jobs"])
    def get_status(task_id: str = Path(pattern=r"^[0-9a-f]{32}$")) -> dict[str, Any]:
        try:
            snapshot = orchestrator.store.get(task_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="task not found") from exc
        return snapshot.model_dump(mode="json") | {"publication_summary": publication_summary(snapshot)}

    @app.get("/jobs", tags=["jobs"])
    def list_jobs(
        status: JobStatus | None = None,
        content_type: str | None = None,
        limit: int = Query(default=50, ge=1, le=200),
    ) -> dict[str, Any]:
        jobs = orchestrator.store.list_jobs(status=status, content_type=content_type, limit=limit)
        return {
            "jobs": [job.model_dump(mode="json") | {"publication_summary": publication_summary(job)} for job in jobs]
        }

    def delete_jobs_result(request: DeleteJobsInput) -> dict[str, list[str]]:
        return orchestrator.store.delete_jobs(request.task_ids)

    app.add_api_route("/jobs/batch-delete", delete_jobs_result, methods=["POST"], tags=["jobs"])
    app.add_api_route("/jobs/delete", delete_jobs_result, methods=["POST"], tags=["jobs"], include_in_schema=False)

    @app.get("/jobs/{task_id}", tags=["jobs"])
    def get_job(task_id: str = Path(pattern=r"^[0-9a-f]{32}$")) -> dict[str, Any]:
        try:
            snapshot = orchestrator.store.get(task_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="task not found") from exc
        return snapshot.model_dump(mode="json") | {"publication_summary": publication_summary(snapshot)}

    @app.patch("/jobs/{task_id}", tags=["jobs"])
    def rename_job(request: RenameJobInput, task_id: str = Path(pattern=r"^[0-9a-f]{32}$")) -> dict[str, Any]:
        try:
            snapshot = orchestrator.store.rename_job(task_id, request.display_name)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="task not found") from exc
        return snapshot.model_dump(mode="json") | {"publication_summary": publication_summary(snapshot)}

    @app.delete("/jobs/{task_id}", tags=["jobs"])
    def delete_job(task_id: str = Path(pattern=r"^[0-9a-f]{32}$")) -> dict[str, Any]:
        try:
            orchestrator.store.delete_job(task_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="task not found") from exc
        except JobDeleteConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"task_id": task_id, "deleted": True}

    @app.get("/schedules", tags=["schedules"])
    def list_schedules() -> dict[str, Any]:
        return {"schedules": [schedule_runner.describe(item) for item in schedule_store.list()]}

    @app.post("/schedules", tags=["schedules"], status_code=201)
    def create_schedule(request: ScheduleCreateInput) -> dict[str, Any]:
        if request.task.publish and not resolved_settings.web_publish_enabled:
            raise HTTPException(status_code=403, detail="web publishing is disabled")
        try:
            normalized, _warnings = validate_task_for_ui(request.task)
            schedule = schedule_store.create(
                name=request.name,
                task=normalized,
                frequency=request.frequency,
                time_of_day=request.time_of_day,
                weekdays=request.weekdays,
                enabled=request.enabled,
            )
        except ValidationError as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": "task_validation_failed", "errors": sanitized_validation_errors(exc)},
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": "task_validation_failed", "errors": [{"field": "schedule", "message": str(exc)}]},
            ) from exc
        return schedule_runner.describe(schedule)

    @app.patch("/schedules/{schedule_id}", tags=["schedules"])
    def update_schedule(
        request: ScheduleUpdateInput,
        schedule_id: str = Path(pattern=r"^[0-9a-f]{32}$"),
    ) -> dict[str, Any]:
        try:
            current = schedule_store.get(schedule_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="schedule not found") from exc
        updates = request.model_dump(exclude_unset=True)
        if not updates:
            return schedule_runner.describe(current)
        if request.task is not None:
            if request.task.publish and not resolved_settings.web_publish_enabled:
                raise HTTPException(status_code=403, detail="web publishing is disabled")
            try:
                normalized_task, _warnings = validate_task_for_ui(request.task)
            except ValidationError as exc:
                raise HTTPException(
                    status_code=422,
                    detail={"code": "task_validation_failed", "errors": sanitized_validation_errors(exc)},
                ) from exc
            except ValueError as exc:
                raise HTTPException(
                    status_code=422,
                    detail={"code": "task_validation_failed", "errors": [{"field": "task", "message": str(exc)}]},
                ) from exc
            updates["task"] = normalized_task.model_dump(mode="json")
        data = current.model_dump(mode="json")
        data.update(updates)
        try:
            updated = ScheduledJob.model_validate(data)
        except ValidationError as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": "task_validation_failed", "errors": sanitized_validation_errors(exc)},
            ) from exc
        timing_changed = any(key in updates for key in ("frequency", "time_of_day", "weekdays"))
        if not updated.enabled:
            updated.next_run_at = None
        elif timing_changed or not current.enabled or updated.next_run_at is None:
            updated.next_run_at = compute_next_run(updated, datetime.now().astimezone()).isoformat()
        schedule_store.save(updated)
        return schedule_runner.describe(updated)

    @app.delete("/schedules/{schedule_id}", tags=["schedules"])
    def delete_schedule(schedule_id: str = Path(pattern=r"^[0-9a-f]{32}$")) -> dict[str, Any]:
        try:
            schedule_store.delete(schedule_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="schedule not found") from exc
        return {"schedule_id": schedule_id, "deleted": True}

    @app.post("/schedules/{schedule_id}/run", tags=["schedules"], status_code=202)
    def run_schedule_now(schedule_id: str = Path(pattern=r"^[0-9a-f]{32}$")) -> dict[str, str]:
        try:
            schedule = schedule_store.get(schedule_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="schedule not found") from exc
        task_id = submit_scheduled_run(schedule.task)
        schedule.last_task_id = task_id
        schedule.last_run_at = utc_now()
        schedule.last_error = None
        schedule_store.save(schedule)
        return {"schedule_id": schedule_id, "task_id": task_id, "status": "queued"}

    def _serialize_publish_policy(policy: dict[str, Any]) -> dict[str, Any]:
        return {
            "policy": {
                content_type: {
                    "enabled": bool(entry.get("enabled")),
                    "targets": [target.model_dump(mode="json") for target in entry.get("targets", [])],
                }
                for content_type, entry in policy.items()
            },
            "supported_platforms": list(SUPPORTED_POLICY_PLATFORMS),
            "publish_enabled": resolved_settings.web_publish_enabled,
        }

    @app.get("/publish-policy", tags=["publishing"])
    def get_publish_policy() -> dict[str, Any]:
        return _serialize_publish_policy(load_publish_policy(resolved_settings))

    @app.put("/publish-policy", tags=["publishing"])
    def put_publish_policy(request: PublishPolicyInput) -> dict[str, Any]:
        try:
            policy = save_publish_policy(resolved_settings, request.policy)
        except ConfigError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _serialize_publish_policy(policy)

    @app.get("/pipeline-defaults", tags=["settings"])
    def get_pipeline_defaults() -> dict[str, Any]:
        effective = load_effective_pipeline_defaults(resolved_settings)
        return {
            "enabled": bool(effective.get("enabled")),
            "pipelines": effective.get("pipelines") or {},
        }

    @app.put("/pipeline-defaults", tags=["settings"])
    def put_pipeline_defaults(request: PipelineDefaultsInput) -> dict[str, Any]:
        known = {pipeline.content_type for pipeline in list_pipelines()}
        unknown = sorted(set(request.pipelines or {}) - known)
        if unknown:
            raise HTTPException(status_code=422, detail=f"unknown content types: {', '.join(unknown)}")
        try:
            effective = save_pipeline_defaults_override(
                resolved_settings,
                {"enabled": request.enabled, "pipelines": request.pipelines},
            )
        except ConfigError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "enabled": bool(effective.get("enabled")),
            "pipelines": effective.get("pipelines") or {},
        }

    @app.get("/account-status", tags=["settings"])
    def get_account_status() -> dict[str, Any]:
        return collect_account_status(resolved_settings)

    @app.post("/maintenance/cache-cleanup", tags=["maintenance"])
    def run_cache_cleanup(request: CacheCleanupInput) -> dict[str, Any]:
        """Scan or delete regenerable caches under CACHE_CLEANUP_ROOT.

        Paths in the response stay relative to the scanned root so the console never
        receives absolute filesystem locations.
        """
        try:
            report = cleanup_cache(resolved_settings.cache_cleanup_root, apply=request.apply)
        except ValueError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "cache_cleanup_unavailable", "message": str(exc)},
            ) from exc
        return report.to_payload()

    @app.get("/jobs/{task_id}/publish-readiness", tags=["publishing"])
    def get_publish_readiness(task_id: str = Path(pattern=r"^[0-9a-f]{32}$")) -> dict[str, Any]:
        try:
            snapshot = orchestrator.store.get(task_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="task not found") from exc
        return {
            "task_id": task_id,
            **publication_readiness(
                snapshot,
                orchestrator.store,
                publish_enabled=resolved_settings.web_publish_enabled,
                account_options=list_publish_account_options(resolved_settings),
            ),
            "publication_summary": publication_summary(snapshot),
        }

    @app.post("/jobs/{task_id}/publish", tags=["publishing"], status_code=202)
    def publish_completed_job(
        publication: DeferredPublishInput,
        request: Request,
        task_id: str = Path(pattern=r"^[0-9a-f]{32}$"),
        publish_confirmation: str | None = Header(default=None, alias="X-AI-Pipeline-Publish-Confirmation"),
    ) -> dict[str, str]:
        if not resolved_settings.web_publish_enabled:
            raise HTTPException(status_code=403, detail="web publishing is disabled")
        try:
            snapshot = orchestrator.store.get(task_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="task not found") from exc
        try:
            _video, video_sha256 = validate_deferred_publish_request(
                snapshot,
                orchestrator.store,
                publication,
                publish_enabled=True,
            )
        except PublicationEligibilityError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        fingerprint = deferred_publish_fingerprint(task_id, video_sha256, publication)
        expected = f"PUBLISH:{fingerprint}"
        if not publish_confirmation or not secrets.compare_digest(publish_confirmation, expected):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "publish_confirmation_required",
                    "publication_fingerprint": fingerprint,
                    "required_confirmation": expected,
                },
            )
        if not security.session_is_recent(request):
            raise HTTPException(
                status_code=401,
                detail={
                    "code": "recent_authentication_required",
                    "message": "recent authentication required for publishing",
                },
            )

        attempt = PublicationAttempt(
            attempt_id=uuid4().hex,
            request=publication,
            video_sha256=video_sha256,
            requested_at=utc_now(),
        )
        try:
            orchestrator.store.add_publication_attempt(task_id, attempt)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        executor.submit(
            run_deferred_publication,
            task_id=task_id,
            attempt_id=attempt.attempt_id,
            store=orchestrator.store,
            settings=resolved_settings,
        )
        return {"task_id": task_id, "attempt_id": attempt.attempt_id, "status": "queued"}

    @app.get("/jobs/{task_id}/events", tags=["jobs"])
    def get_job_events(
        task_id: str = Path(pattern=r"^[0-9a-f]{32}$"),
        limit: int = Query(default=50, ge=1, le=500),
    ) -> dict[str, Any]:
        try:
            events = orchestrator.store.read_events(task_id, limit=limit)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="task not found") from exc
        except OSError as exc:
            raise HTTPException(
                status_code=500,
                detail={"code": "job_events_unavailable", "message": "job events cannot be read"},
            ) from exc
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=500,
                detail={"code": "job_events_invalid", "message": "job events are invalid"},
            ) from exc
        return {"task_id": task_id, "events": events}

    @app.get("/jobs/{task_id}/artifacts", tags=["artifacts"])
    def list_job_artifacts(task_id: str = Path(pattern=r"^[0-9a-f]{32}$")) -> dict[str, Any]:
        job_dir = orchestrator.store.job_dir(task_id)
        if job_dir.is_symlink():
            raise HTTPException(status_code=404, detail="task not found")
        try:
            snapshot = orchestrator.store.get(task_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="task not found") from exc
        artifacts = registered_artifacts(snapshot, job_dir)
        return {
            "task_id": task_id,
            "artifacts": [
                {
                    "artifact_id": artifact.artifact_id,
                    "name": artifact.path.name,
                    "relative_path": artifact.relative_path,
                    "media_type": artifact.media_type,
                    "size": artifact.size,
                    "url": f"/jobs/{task_id}/artifacts/{artifact.artifact_id}",
                }
                for artifact in artifacts.values()
            ],
        }

    @app.get("/jobs/{task_id}/artifacts/{artifact_id}", tags=["artifacts"])
    def get_job_artifact(
        task_id: str = Path(pattern=r"^[0-9a-f]{32}$"),
        artifact_id: str = Path(pattern=r"^[0-9a-f]{24}$"),
    ) -> FileResponse:
        job_dir = orchestrator.store.job_dir(task_id)
        if job_dir.is_symlink():
            raise HTTPException(status_code=404, detail="task not found")
        try:
            snapshot = orchestrator.store.get(task_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="task not found") from exc
        artifact = registered_artifacts(snapshot, job_dir).get(artifact_id)
        if artifact is None:
            raise HTTPException(status_code=404, detail="artifact not found")
        return FileResponse(
            artifact.path,
            media_type=artifact.media_type,
            headers={"Content-Disposition": f"inline; filename*=UTF-8''{quote(artifact.path.name)}"},
        )

    return app


app = create_app()
