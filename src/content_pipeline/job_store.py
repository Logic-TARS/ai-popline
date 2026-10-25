from __future__ import annotations

import json
import re
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from .models import (
    ArtifactSet,
    JobProgress,
    JobSnapshot,
    JobStatus,
    PipelineStep,
    PublicationAttempt,
    RouteResult,
    TaskInput,
)

STORE_VERSION = 1
"""Schema version written into every status.json. Increment on breaking changes."""

__all__ = ["STORE_VERSION", "JobDeleteConflictError", "JobStore", "utc_now"]

_TASK_ID_PATTERN = re.compile(r"[0-9a-f]{32}")


class JobDeleteConflictError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _safe_progress_text(value: str) -> str:
    """Prevent progress events from becoming a channel for local path disclosure."""
    return re.sub(r"(?:[A-Za-z]:[\\/][^\s\"']+|/(?:[^\s\"']+/)+[^\s\"']+)", "[已隐藏路径]", value)


_MAX_ERROR_CHARS = 2000
_TRUNCATION_NOTICE = "...[错误信息已截断]..."


def _safe_error_text(value: str | None) -> str | None:
    """Keep persisted errors inside the JobSnapshot limit so status.json stays readable.

    Both ends are kept: the first lines explain how the work started, and the final
    lines usually carry the actual failure reason for external-tool output.
    """
    if value is None:
        return None
    if len(value) <= _MAX_ERROR_CHARS:
        return value
    budget = _MAX_ERROR_CHARS - len(_TRUNCATION_NOTICE)
    head = budget // 2
    return value[:head] + _TRUNCATION_NOTICE + value[-(budget - head) :]


def _validate_task_id(task_id: str) -> None:
    if not _TASK_ID_PATTERN.fullmatch(task_id):
        raise FileNotFoundError(f"job not found: {task_id}")


def _parse_timestamp(value: str | None) -> datetime | None:
    """Parse a persisted ISO timestamp, treating naive values as UTC."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


class JobStore:
    def __init__(self, data_dir: Path):
        self.jobs_dir = data_dir / "jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create(self, task: TaskInput) -> JobSnapshot:
        task_id = uuid4().hex
        job_dir = self.job_dir(task_id)
        (job_dir / "images").mkdir(parents=True)
        (job_dir / "video").mkdir()
        now = utc_now()
        snapshot = JobSnapshot(
            task_id=task_id,
            status=JobStatus.QUEUED,
            task=task,
            store_version=STORE_VERSION,
            created_at=now,
            updated_at=now,
        )
        self.save(snapshot)
        self.event(task_id, "job_created", {"task": task.model_dump(mode="json")})
        return snapshot

    def job_dir(self, task_id: str) -> Path:
        _validate_task_id(task_id)
        return self.jobs_dir / task_id

    def status_path(self, task_id: str) -> Path:
        return self.job_dir(task_id) / "status.json"

    def events_path(self, task_id: str) -> Path:
        return self.job_dir(task_id) / "events.jsonl"

    def _existing_job_dir(self, task_id: str) -> Path:
        path = self.job_dir(task_id)
        if path.is_symlink() or not path.is_dir():
            raise FileNotFoundError(f"job not found: {task_id}")
        return path

    def get(self, task_id: str) -> JobSnapshot:
        with self._lock:
            raw = (self._existing_job_dir(task_id) / "status.json").read_text(encoding="utf-8")
            return JobSnapshot.model_validate_json(raw)

    def save(self, snapshot: JobSnapshot) -> None:
        with self._lock:
            job_dir = self.job_dir(snapshot.task_id)
            if job_dir.is_symlink():
                raise FileNotFoundError(f"job not found: {snapshot.task_id}")
            path = job_dir / "status.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            snapshot.store_version = STORE_VERSION
            snapshot.updated_at = utc_now()
            data = snapshot.model_dump(mode="json")
            tmp_path = path.with_name(f"{path.name}.tmp")
            tmp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp_path.replace(path)

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def event(self, task_id: str, event: str, payload: dict[str, Any] | None = None) -> None:
        record = {
            "timestamp": utc_now(),
            "event": event,
            "payload": payload or {},
        }
        with self._lock:
            events_path = self._existing_job_dir(task_id) / "events.jsonl"
            with events_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def read_events(self, task_id: str, *, limit: int | None = None) -> list[dict[str, Any]]:
        if limit is not None and limit < 1:
            raise ValueError("limit must be a positive integer")
        events_path = self._existing_job_dir(task_id) / "events.jsonl"
        if not events_path.is_file():
            raise FileNotFoundError(f"job not found: {task_id}")
        events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        return events[-limit:] if limit is not None else events

    # ------------------------------------------------------------------
    # Lifecycle helpers
    # ------------------------------------------------------------------

    def mark_running(self, task_id: str, step: PipelineStep) -> JobSnapshot:
        snapshot = self.get(task_id)
        snapshot.status = JobStatus.RUNNING
        if snapshot.started_at is None:
            snapshot.started_at = utc_now()
        snapshot.current_step = step
        snapshot.error = None
        self.save(snapshot)
        self.event(task_id, "step_started", {"step": step.value})
        return snapshot

    def set_route(self, task_id: str, route: RouteResult) -> None:
        snapshot = self.get(task_id)
        snapshot.route = route
        self.save(snapshot)
        self.event(task_id, "route_resolved", route.model_dump(mode="json"))

    def set_artifacts(self, task_id: str, artifacts: ArtifactSet) -> None:
        snapshot = self.get(task_id)
        snapshot.artifacts = artifacts
        self.save(snapshot)

    def set_progress(
        self,
        task_id: str,
        *,
        percent: int,
        phase: str,
        message: str,
        is_estimate: bool = False,
    ) -> JobSnapshot:
        """Persist a user-facing progress update without exposing tool output or paths."""
        snapshot = self.get(task_id)
        previous = snapshot.progress.percent if snapshot.progress else 0
        progress = JobProgress(
            percent=max(previous, percent),
            phase=_safe_progress_text(phase),
            message=_safe_progress_text(message),
            updated_at=utc_now(),
            is_estimate=is_estimate,
        )
        snapshot.progress = progress
        self.save(snapshot)
        self.event(task_id, "progress_updated", progress.model_dump(mode="json"))
        return snapshot

    def finish(self, task_id: str, status: JobStatus, error: str | None = None) -> None:
        snapshot = self.get(task_id)
        snapshot.status = status
        snapshot.current_step = (
            PipelineStep.COMPLETE if status in {JobStatus.SUCCEEDED, JobStatus.PARTIAL} else snapshot.current_step
        )
        snapshot.error = _safe_error_text(error)
        snapshot.finished_at = utc_now()
        self.save(snapshot)
        self.event(task_id, "job_finished", {"status": status.value, "error": _safe_error_text(error)})

    # ------------------------------------------------------------------
    # Deferred publication helpers
    # ------------------------------------------------------------------

    def add_publication_attempt(self, task_id: str, attempt: PublicationAttempt) -> JobSnapshot:
        with self._lock:
            snapshot = self.get(task_id)
            if any(item.status in {"queued", "running"} for item in snapshot.publication_attempts):
                raise ValueError("a publication attempt is already active")
            snapshot.publication_attempts.append(attempt)
            self.save(snapshot)
            self.event(
                task_id,
                "publish_requested",
                {"attempt_id": attempt.attempt_id, "platforms": [t.platform for t in attempt.request.publish_targets]},
            )
            return snapshot

    def mark_publication_started(self, task_id: str, attempt_id: str) -> PublicationAttempt:
        with self._lock:
            snapshot, attempt = self._publication_attempt(task_id, attempt_id)
            attempt.status = "running"
            attempt.started_at = utc_now()
            attempt.error = None
            self.save(snapshot)
            self.event(task_id, "publish_started", {"attempt_id": attempt_id})
            return attempt

    def set_publication_target_result(
        self,
        task_id: str,
        attempt_id: str,
        platform: str,
        result: dict[str, Any],
    ) -> PublicationAttempt:
        with self._lock:
            snapshot, attempt = self._publication_attempt(task_id, attempt_id)
            attempt.results[platform] = result
            self.save(snapshot)
            self.event(
                task_id,
                "publish_target_finished",
                {
                    "attempt_id": attempt_id,
                    "platform": platform,
                    "success": bool(result.get("success")),
                },
            )
            return attempt

    def finish_publication_attempt(
        self,
        task_id: str,
        attempt_id: str,
        status: str,
        error: str | None = None,
    ) -> PublicationAttempt:
        if status not in {"succeeded", "partial", "failed"}:
            raise ValueError(f"invalid terminal publication status: {status}")
        with self._lock:
            snapshot, attempt = self._publication_attempt(task_id, attempt_id)
            attempt.status = status  # type: ignore[assignment]
            attempt.error = _safe_error_text(error)
            attempt.finished_at = utc_now()
            self.save(snapshot)
            self.event(
                task_id,
                "publish_finished",
                {"attempt_id": attempt_id, "status": status, "error": _safe_progress_text(error or "") or None},
            )
            return attempt

    def _publication_attempt(self, task_id: str, attempt_id: str) -> tuple[JobSnapshot, PublicationAttempt]:
        snapshot = self.get(task_id)
        attempt = next((item for item in snapshot.publication_attempts if item.attempt_id == attempt_id), None)
        if attempt is None:
            raise FileNotFoundError(f"publication attempt not found: {attempt_id}")
        return snapshot, attempt

    # ------------------------------------------------------------------
    # Listing & cleanup
    # ------------------------------------------------------------------

    def list_jobs(
        self,
        status: JobStatus | None = None,
        content_type: str | None = None,
        limit: int = 50,
    ) -> list[JobSnapshot]:
        """Return recent job snapshots, optionally filtered. Newest first."""
        jobs: list[JobSnapshot] = []
        job_dirs = [path for path in self.jobs_dir.iterdir() if not path.is_symlink() and path.is_dir()]
        job_dirs.sort(
            key=lambda path: (path / "status.json").stat().st_mtime if (path / "status.json").exists() else 0,
            reverse=True,
        )
        for job_dir in job_dirs:
            if job_dir.is_symlink() or not job_dir.is_dir():
                continue
            status_path = job_dir / "status.json"
            if not status_path.exists():
                continue
            try:
                snapshot = JobSnapshot.model_validate_json(status_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if status is not None and snapshot.status != status:
                continue
            if content_type is not None and snapshot.route and snapshot.route.content_type != content_type:
                continue
            jobs.append(snapshot)
            if len(jobs) >= limit:
                break
        return jobs

    def rename_job(self, task_id: str, display_name: str) -> JobSnapshot:
        normalized = display_name.strip()
        if not normalized or len(normalized) > 120:
            raise ValueError("display_name must contain 1-120 characters")
        with self._lock:
            try:
                snapshot = self.get(task_id)
            except FileNotFoundError as exc:
                raise FileNotFoundError(f"job not found: {task_id}") from exc
            snapshot.display_name = normalized
            self.save(snapshot)
            self.event(task_id, "job_metadata_updated", {"display_name": normalized})
            return snapshot

    def delete_job(self, task_id: str) -> None:
        with self._lock:
            try:
                job_dir = self._existing_job_dir(task_id)
                snapshot = self.get(task_id)
            except FileNotFoundError as exc:
                raise FileNotFoundError(f"job not found: {task_id}") from exc
            if snapshot.status in {JobStatus.QUEUED, JobStatus.RUNNING}:
                raise JobDeleteConflictError("queued or running jobs cannot be deleted")
            if any(item.status in {"queued", "running"} for item in snapshot.publication_attempts):
                raise JobDeleteConflictError("jobs with an active publication cannot be deleted")
            _rmtree(job_dir)

    def delete_jobs(self, task_ids: list[str]) -> dict[str, list[str]]:
        result = {"deleted": [], "not_found": [], "blocked": [], "failed": []}
        for task_id in dict.fromkeys(task_ids):
            try:
                self.delete_job(task_id)
                result["deleted"].append(task_id)
            except JobDeleteConflictError:
                result["blocked"].append(task_id)
            except FileNotFoundError:
                result["not_found"].append(task_id)
            except OSError:
                result["failed"].append(task_id)
        return result

    def cleanup_old_jobs(self, max_age_days: int = 30) -> int:
        """Delete terminal jobs older than *max_age_days*. Returns count of removed jobs."""
        if max_age_days < 1:
            raise ValueError("max_age_days must be a positive integer")
        cutoff = time.time() - (max_age_days * 86400)
        removed = 0
        with self._lock:
            for job_dir in self.jobs_dir.iterdir():
                if job_dir.is_symlink() or not job_dir.is_dir():
                    continue
                if not _TASK_ID_PATTERN.fullmatch(job_dir.name):
                    continue
                status_file = job_dir / "status.json"
                if not status_file.exists():
                    continue
                try:
                    mtime = status_file.stat().st_mtime
                    snapshot = JobSnapshot.model_validate_json(status_file.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if snapshot.task_id != job_dir.name:
                    continue
                if mtime >= cutoff:
                    continue
                if snapshot.status in {JobStatus.QUEUED, JobStatus.RUNNING}:
                    continue
                if any(item.status in {"queued", "running"} for item in snapshot.publication_attempts):
                    continue
                _rmtree(job_dir)
                removed += 1
        return removed

    def reap_orphaned_jobs(self, max_age_seconds: int = 21600) -> dict[str, list[str]]:
        """Fail queued or running jobs whose worker is no longer alive.

        A job whose worker thread died keeps its queued/running status forever, which makes it
        undeletable through every supported path. Anything left untouched for longer than
        *max_age_seconds* is assumed orphaned and rewritten as a failed terminal job.
        """
        if max_age_seconds < 1:
            raise ValueError("max_age_seconds must be a positive integer")
        cutoff = datetime.now(UTC) - timedelta(seconds=max_age_seconds)
        error = "orphaned: no active worker"
        result: dict[str, list[str]] = {"reaped": [], "skipped": []}
        with self._lock:
            for job_dir in self.jobs_dir.iterdir():
                if job_dir.is_symlink() or not job_dir.is_dir():
                    continue
                if not _TASK_ID_PATTERN.fullmatch(job_dir.name):
                    continue
                status_file = job_dir / "status.json"
                if not status_file.exists():
                    continue
                try:
                    snapshot = JobSnapshot.model_validate_json(status_file.read_text(encoding="utf-8"))
                    updated_at = _parse_timestamp(snapshot.updated_at) or datetime.fromtimestamp(
                        status_file.stat().st_mtime, tz=UTC
                    )
                except (OSError, ValueError):
                    continue
                if snapshot.task_id != job_dir.name:
                    continue
                if snapshot.status not in {JobStatus.QUEUED, JobStatus.RUNNING}:
                    continue
                if updated_at > cutoff:
                    result["skipped"].append(snapshot.task_id)
                    continue
                snapshot.status = JobStatus.FAILED
                snapshot.error = _safe_error_text(error)
                snapshot.finished_at = utc_now()
                self.save(snapshot)
                self.event(
                    snapshot.task_id,
                    "job_reaped",
                    {"status": JobStatus.FAILED.value, "error": error, "max_age_seconds": max_age_seconds},
                )
                result["reaped"].append(snapshot.task_id)
        return result


def _rmtree(path: Path) -> None:
    """Remove a directory tree. Missing path is a no-op."""
    if not path.exists():
        return
    for child in sorted(path.iterdir(), key=lambda p: p.is_dir(), reverse=True):
        if child.is_dir() and not child.is_symlink():
            _rmtree(child)
        else:
            child.unlink(missing_ok=True)
    path.rmdir()
