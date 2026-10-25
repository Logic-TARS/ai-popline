import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from content_pipeline.job_store import JobDeleteConflictError, JobStore
from content_pipeline.models import (
    ArtifactSet,
    DeferredPublishInput,
    ImageFileValidation,
    JobSnapshot,
    JobStatus,
    PipelineStep,
    PublicationAttempt,
    PublishTarget,
    RouteResult,
    TaskInput,
    VideoValidation,
)


def test_job_models_reject_unsafe_persisted_payloads() -> None:
    with pytest.raises(ValueError):
        ArtifactSet(unexpected=True)
    with pytest.raises(ValueError):
        RouteResult(content_type="japanese", topic="topic", unexpected=True)
    with pytest.raises(ValueError):
        JobSnapshot(task_id="not-a-task-id", status=JobStatus.QUEUED, task=TaskInput(description="ok"))


def test_media_validation_models_reject_invalid_dimensions() -> None:
    with pytest.raises(ValueError):
        ImageFileValidation(path=Path("a.png"), format="", width=0, height=160)
    with pytest.raises(ValueError):
        VideoValidation(
            duration_seconds=-1,
            width=90,
            height=160,
            aspect_ratio=0.5625,
            video_codec="mpeg4",
            decoded_video_frames=1,
            decoded_audio_frames=0,
        )


def test_job_store_rejects_unsafe_task_ids_before_path_access(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "output")
    unsafe_ids = ["../outside", "a" * 31, "A" * 32, " " + "0" * 32 + " "]

    for unsafe_id in unsafe_ids:
        with pytest.raises(FileNotFoundError):
            store.job_dir(unsafe_id)
        with pytest.raises(FileNotFoundError):
            store.status_path(unsafe_id)
        with pytest.raises(FileNotFoundError):
            store.events_path(unsafe_id)
        with pytest.raises(FileNotFoundError):
            store.get(unsafe_id)
        with pytest.raises(FileNotFoundError):
            store.rename_job(unsafe_id, "新名称")
        with pytest.raises(FileNotFoundError):
            store.delete_job(unsafe_id)

    assert not (tmp_path / "outside").exists()


def test_progress_is_persisted_as_an_event_without_local_paths(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "output")
    task_id = store.create(TaskInput(description="进度测试")).task_id

    snapshot = store.set_progress(
        task_id,
        percent=45,
        phase="获取 G:\\private\\source",
        message="正在读取 G:\\private\\source\\clip.mp4",
        is_estimate=True,
    )

    assert snapshot.progress is not None
    assert snapshot.progress.percent == 45
    assert snapshot.progress.is_estimate is True
    assert "G:" not in snapshot.progress.phase
    assert "G:" not in snapshot.progress.message
    events = store.events_path(task_id).read_text(encoding="utf-8")
    assert '"event": "progress_updated"' in events
    assert "G:" not in events


def test_read_events_returns_recent_events_and_rejects_invalid_logs(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "output")
    task_id = store.create(TaskInput(description="事件读取测试")).task_id
    store.event(task_id, "first", {"index": 1})
    store.event(task_id, "second", {"index": 2})

    events = store.read_events(task_id, limit=1)

    assert events == [{"timestamp": events[0]["timestamp"], "event": "second", "payload": {"index": 2}}]
    with pytest.raises(ValueError, match="limit must be a positive integer"):
        store.read_events(task_id, limit=0)
    store.events_path(task_id).write_text("{not-json\n", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        store.read_events(task_id)


def test_delete_job_removes_terminal_job_and_blocks_running_job(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "output")
    terminal = store.create(TaskInput(description="可删除任务")).task_id
    running = store.create(TaskInput(description="运行中任务")).task_id
    store.finish(terminal, JobStatus.SUCCEEDED)
    store.mark_running(running, PipelineStep.ROUTE)

    store.delete_job(terminal)

    assert not store.job_dir(terminal).exists()
    with pytest.raises(FileNotFoundError):
        store.get(terminal)
    with pytest.raises(JobDeleteConflictError):
        store.delete_job(running)
    assert store.job_dir(running).exists()


def test_finish_truncates_long_errors_so_status_stays_readable(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "output")
    task_id = store.create(TaskInput(description="长错误")).task_id
    store.finish(task_id, JobStatus.FAILED, "x" * 4104)
    snapshot = store.get(task_id)
    assert snapshot.status is JobStatus.FAILED
    assert snapshot.error is not None
    assert len(snapshot.error) <= 2000
    assert "[错误信息已截断]" in snapshot.error
    assert snapshot.error.endswith("x" * 10)


def test_finish_keeps_the_tail_of_a_long_error(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "output")
    task_id = store.create(TaskInput(description="长错误尾部")).task_id
    store.finish(task_id, JobStatus.FAILED, "head" + "x" * 4000 + "final error line")
    snapshot = store.get(task_id)
    assert snapshot.error is not None
    assert len(snapshot.error) <= 2000
    assert snapshot.error.endswith("final error line")


def test_job_store_rejects_symlinked_job_directory(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "output")
    task_id = "0" * 32
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "status.json").write_text(
        JobSnapshot(
            task_id=task_id,
            status=JobStatus.SUCCEEDED,
            task=TaskInput(description="外部任务"),
        ).model_dump_json(),
        encoding="utf-8",
    )
    (outside / "events.jsonl").write_text('{"event":"outside"}\n', encoding="utf-8")
    (store.jobs_dir / task_id).symlink_to(outside, target_is_directory=True)

    with pytest.raises(FileNotFoundError):
        store.get(task_id)
    with pytest.raises(FileNotFoundError):
        store.read_events(task_id)
    with pytest.raises(FileNotFoundError):
        store.save(JobSnapshot(task_id=task_id, status=JobStatus.SUCCEEDED, task=TaskInput(description="更新外部任务")))
    with pytest.raises(FileNotFoundError):
        store.event(task_id, "outside_write", {"ok": True})
    with pytest.raises(FileNotFoundError):
        store.delete_job(task_id)

    assert outside.is_dir()
    assert (outside / "status.json").is_file()
    assert "更新外部任务" not in (outside / "status.json").read_text(encoding="utf-8")
    assert "outside_write" not in (outside / "events.jsonl").read_text(encoding="utf-8")
    assert store.delete_jobs([task_id])["not_found"] == [task_id]


def test_list_jobs_skips_symlinked_job_directory(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "output")
    real = store.create(TaskInput(description="真实任务")).task_id
    store.finish(real, JobStatus.SUCCEEDED)
    task_id = "0" * 32
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "status.json").write_text(
        JobSnapshot(
            task_id=task_id,
            status=JobStatus.SUCCEEDED,
            task=TaskInput(description="外部任务"),
        ).model_dump_json(),
        encoding="utf-8",
    )
    (store.jobs_dir / task_id).symlink_to(outside, target_is_directory=True)

    listed_ids = {snapshot.task_id for snapshot in store.list_jobs()}

    assert real in listed_ids
    assert task_id not in listed_ids


def test_delete_jobs_reports_partial_results(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "output")
    first = store.create(TaskInput(description="第一项")).task_id
    blocked = store.create(TaskInput(description="运行中")).task_id
    store.finish(first, JobStatus.FAILED, "failed for test")
    store.mark_running(blocked, PipelineStep.ROUTE)
    missing = "0" * 32

    result = store.delete_jobs([first, blocked, missing])

    assert result["deleted"] == [first]
    assert result["blocked"] == [blocked]
    assert result["not_found"] == [missing]
    assert result["failed"] == []


def test_rename_job_preserves_original_task_and_records_event(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "output")
    task_id = store.create(TaskInput(description="原始任务名称")).task_id

    renamed = store.rename_job(task_id, "  新显示名称  ")

    assert renamed.display_name == "新显示名称"
    assert renamed.task.description == "原始任务名称"
    persisted = store.get(task_id)
    assert persisted.display_name == "新显示名称"
    assert persisted.task.description == "原始任务名称"
    events = store.events_path(task_id).read_text(encoding="utf-8")
    assert '"event": "job_metadata_updated"' in events
    assert "新显示名称" in events


def test_delete_job_blocks_active_publication(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "output")
    task_id = store.create(TaskInput(description="正在发布")).task_id
    store.finish(task_id, JobStatus.SUCCEEDED)
    snapshot = store.get(task_id)
    snapshot.publication_attempts.append(
        PublicationAttempt(
            attempt_id="a" * 32,
            request=DeferredPublishInput(
                publish_targets=[PublishTarget(platform="douyin", account="test")],
                title="测试标题",
            ),
            video_sha256="b" * 64,
            requested_at="2026-08-11T00:00:00+00:00",
        )
    )
    store.save(snapshot)

    with pytest.raises(JobDeleteConflictError, match="active publication"):
        store.delete_job(task_id)

    assert store.job_dir(task_id).exists()


def test_cleanup_old_jobs_keeps_active_generation_and_publication(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "output")
    terminal = store.create(TaskInput(description="旧终态任务")).task_id
    running = store.create(TaskInput(description="旧运行中任务")).task_id
    publishing = store.create(TaskInput(description="旧发布中任务")).task_id
    recent = store.create(TaskInput(description="新终态任务")).task_id

    store.finish(terminal, JobStatus.SUCCEEDED)
    store.mark_running(running, PipelineStep.ROUTE)
    store.finish(publishing, JobStatus.SUCCEEDED)
    snapshot = store.get(publishing)
    snapshot.publication_attempts.append(
        PublicationAttempt(
            attempt_id="c" * 32,
            request=DeferredPublishInput(
                publish_targets=[PublishTarget(platform="douyin", account="test")],
                title="测试标题",
            ),
            video_sha256="d" * 64,
            requested_at="2026-08-11T00:00:00+00:00",
            status="running",
        )
    )
    store.save(snapshot)
    store.finish(recent, JobStatus.FAILED, "recent failure")

    old_timestamp = time.time() - (31 * 86400)
    for task_id in (terminal, running, publishing):
        status_path = store.status_path(task_id)
        os.utime(status_path, (old_timestamp, old_timestamp))

    assert store.cleanup_old_jobs(max_age_days=30) == 1

    assert not store.job_dir(terminal).exists()
    assert store.job_dir(running).exists()
    assert store.job_dir(publishing).exists()
    assert store.job_dir(recent).exists()


@pytest.mark.parametrize("max_age_days", [0, -1])
def test_cleanup_old_jobs_rejects_non_positive_age(tmp_path: Path, max_age_days: int) -> None:
    store = JobStore(tmp_path / "output")
    task_id = store.create(TaskInput(description="旧终态任务")).task_id
    store.finish(task_id, JobStatus.SUCCEEDED)

    with pytest.raises(ValueError, match="max_age_days must be a positive integer"):
        store.cleanup_old_jobs(max_age_days=max_age_days)

    assert store.job_dir(task_id).exists()


def test_cleanup_old_jobs_skips_non_job_dirs_and_mismatched_status_files(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "output")
    real_task_id = store.create(TaskInput(description="真实旧任务")).task_id
    store.finish(real_task_id, JobStatus.FAILED, "old failure")

    non_job_dir = store.jobs_dir / "operator-notes"
    non_job_dir.mkdir()
    (non_job_dir / "status.json").write_text(
        store.status_path(real_task_id).read_text(encoding="utf-8"), encoding="utf-8"
    )

    mismatched_dir = store.jobs_dir / ("f" * 32)
    mismatched_dir.mkdir()
    (mismatched_dir / "status.json").write_text(
        store.status_path(real_task_id).read_text(encoding="utf-8"), encoding="utf-8"
    )

    outside = tmp_path / "outside-job"
    outside.mkdir()
    symlinked_dir = store.jobs_dir / ("e" * 32)
    symlinked_dir.symlink_to(outside, target_is_directory=True)

    old_timestamp = time.time() - (31 * 86400)
    for status_path in (
        store.status_path(real_task_id),
        non_job_dir / "status.json",
        mismatched_dir / "status.json",
    ):
        os.utime(status_path, (old_timestamp, old_timestamp))

    assert store.cleanup_old_jobs(max_age_days=30) == 1

    assert not store.job_dir(real_task_id).exists()
    assert non_job_dir.exists()
    assert mismatched_dir.exists()
    assert symlinked_dir.exists()
    assert outside.exists()


def _backdate_updated_at(store: JobStore, task_id: str, seconds: int = 7 * 3600) -> None:
    """Rewrite a persisted updated_at so staleness rules can be exercised."""
    status_path = store.status_path(task_id)
    data = json.loads(status_path.read_text(encoding="utf-8"))
    data["updated_at"] = (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()
    status_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_reap_orphaned_jobs_fails_jobs_whose_worker_is_gone(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "output")
    queued = store.create(TaskInput(description="僵死排队任务")).task_id
    running = store.create(TaskInput(description="僵死运行任务")).task_id
    store.mark_running(running, PipelineStep.VIDEO)
    finished = store.create(TaskInput(description="已完成任务")).task_id
    store.finish(finished, JobStatus.SUCCEEDED)
    non_job_dir = store.jobs_dir / "operator-notes"
    non_job_dir.mkdir()
    (non_job_dir / "status.json").write_text(store.status_path(queued).read_text(encoding="utf-8"), encoding="utf-8")
    _backdate_updated_at(store, queued)
    _backdate_updated_at(store, running)

    result = store.reap_orphaned_jobs(max_age_seconds=21600)

    assert sorted(result["reaped"]) == sorted([queued, running])
    assert result["skipped"] == []
    for task_id in (queued, running):
        snapshot = store.get(task_id)
        assert snapshot.status is JobStatus.FAILED
        assert snapshot.error == "orphaned: no active worker"
        assert snapshot.finished_at is not None
        assert '"event": "job_reaped"' in store.events_path(task_id).read_text(encoding="utf-8")
    assert store.get(queued).task.description == "僵死排队任务"
    assert store.get(running).current_step is PipelineStep.VIDEO
    assert store.get(finished).status is JobStatus.SUCCEEDED
    assert non_job_dir.exists()


def test_reap_orphaned_jobs_keeps_active_jobs_inside_the_age_window(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "output")
    queued = store.create(TaskInput(description="活跃排队任务")).task_id
    running = store.create(TaskInput(description="活跃运行任务")).task_id
    store.mark_running(running, PipelineStep.ROUTE)

    result = store.reap_orphaned_jobs(max_age_seconds=21600)

    assert result["reaped"] == []
    assert sorted(result["skipped"]) == sorted([queued, running])
    assert store.get(queued).status is JobStatus.QUEUED
    assert store.get(running).status is JobStatus.RUNNING


def test_reaped_orphan_can_then_be_deleted(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "output")
    task_id = store.create(TaskInput(description="僵死任务")).task_id
    store.mark_running(task_id, PipelineStep.VIDEO)
    _backdate_updated_at(store, task_id)

    with pytest.raises(JobDeleteConflictError):
        store.delete_job(task_id)

    assert store.reap_orphaned_jobs(max_age_seconds=21600)["reaped"] == [task_id]

    assert store.delete_jobs([task_id])["deleted"] == [task_id]
    assert not store.job_dir(task_id).exists()


@pytest.mark.parametrize("max_age_seconds", [0, -1])
def test_reap_orphaned_jobs_rejects_non_positive_age(tmp_path: Path, max_age_seconds: int) -> None:
    store = JobStore(tmp_path / "output")
    task_id = store.create(TaskInput(description="活跃排队任务")).task_id

    with pytest.raises(ValueError, match="max_age_seconds must be a positive integer"):
        store.reap_orphaned_jobs(max_age_seconds=max_age_seconds)

    assert store.get(task_id).status is JobStatus.QUEUED
