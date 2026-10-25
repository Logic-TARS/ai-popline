from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from content_pipeline.errors import ExternalToolError
from content_pipeline.settings import Settings
from content_pipeline.tools.common import mpt_task_id
from content_pipeline.tools.narrated_mpt_client import (
    _adaptive_voice_rate,
    _guard_generated_subtitle,
    call_narrated_mpt,
)


def test_mismatched_input_hash_never_reuses_existing_task(tmp_path: Path, monkeypatch) -> None:
    mpt_dir = tmp_path / "mpt"
    mpt_name = mpt_task_id("ai-briefing-20260720")
    task_dir = mpt_dir / "storage" / "tasks" / mpt_name
    task_dir.mkdir(parents=True)
    (task_dir / "final-1.mp4").write_bytes(b"old-video")
    (task_dir / "subtitle.srt").write_text("old subtitle", encoding="utf-8")
    (task_dir / "script.json").write_text(json.dumps({"script": "new narration"}), encoding="utf-8")
    (task_dir / "ai-popline-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "task_id": "ai-briefing-20260720",
                "title": "Daily AI",
                "script_sha256": "wrong",
                "input_sha256": {"article.md": "old"},
            }
        ),
        encoding="utf-8",
    )
    captured: list[str] = []
    captured_kwargs: dict[str, object] = {}
    temporary_directory: Path | None = None

    def fake_run(command, **kwargs):
        nonlocal temporary_directory
        captured.extend(command)
        captured_kwargs.update(kwargs)
        environment = kwargs["env"]
        temporary_directory = Path(environment["TEMP"])
        assert temporary_directory.is_dir()
        assert environment["TEMP"] == environment["TMP"] == environment["TMPDIR"]
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "final-1.mp4").write_bytes(b"new-video")
        (task_dir / "subtitle.srt").write_text(
            "1\n00:00:00,000 --> 00:00:05,000\n这是一段足够长并且有效的人工智能简报字幕。\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("content_pipeline.tools.narrated_mpt_client.run_command", fake_run)
    result = call_narrated_mpt(
        task_name="ai-briefing-20260720",
        title="Daily AI",
        script="new narration",
        output_dir=tmp_path / "output",
        settings=Settings(mpt_dir=mpt_dir, mpt_python=Path("python")),
        input_hashes={"article.md": "new"},
    )
    assert captured[captured.index("--task-id") + 1] == mpt_name
    environment = captured_kwargs["env"]
    assert isinstance(environment, dict)
    assert environment["PYTHONIOENCODING"] == "utf-8"
    assert environment["PYTHONUTF8"] == "1"
    assert temporary_directory is not None
    assert not temporary_directory.exists()
    assert captured_kwargs["timeout"] == 3600
    assert result.video.read_bytes() == b"new-video"
    assert list((mpt_dir / "storage" / "tasks").glob(f"{mpt_name}.stale-*"))


def test_invalid_exact_task_is_archived_instead_of_reused(tmp_path: Path, monkeypatch) -> None:
    mpt_dir = tmp_path / "mpt"
    mpt_name = mpt_task_id("finance-20260721")
    task_dir = mpt_dir / "storage" / "tasks" / mpt_name
    task_dir.mkdir(parents=True)
    (task_dir / "final-1.mp4").write_bytes(b"incomplete-video")
    (task_dir / "subtitle.srt").write_text(
        "1\n00:00:00,000 --> 00:00:05,000\n这是一段足够长并且有效的金融字幕。\n",
        encoding="utf-8",
    )
    (task_dir / "script.json").write_text(json.dumps({"script": "same narration"}), encoding="utf-8")
    captured: list[str] = []

    def fake_run(command, **kwargs):
        captured.extend(command)
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "final-1.mp4").write_bytes(b"regenerated-video")
        (task_dir / "subtitle.srt").write_text(
            "1\n00:00:00,000 --> 00:00:05,000\n这是一段足够长并且有效的金融字幕。\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("content_pipeline.tools.narrated_mpt_client.run_command", fake_run)
    result = call_narrated_mpt(
        task_name="finance-20260721",
        title="每日基金日报",
        script="same narration",
        output_dir=tmp_path / "output",
        settings=Settings(mpt_dir=mpt_dir, mpt_python=Path("python")),
    )
    assert captured
    assert result.video.read_bytes() == b"regenerated-video"
    assert list((mpt_dir / "storage" / "tasks").glob(f"{mpt_name}.stale-*"))


def test_narrated_mpt_reports_safe_milestones_from_streamed_output(tmp_path: Path, monkeypatch) -> None:
    mpt_dir = tmp_path / "mpt"
    task_name = "content-progress"
    task_dir = mpt_dir / "storage" / "tasks" / mpt_task_id(task_name)
    updates: list[tuple[int, str, str, bool]] = []

    def fake_run(command, **kwargs):
        callback = kwargs["on_output"]
        assert callback is not None
        callback("stdout", "starting tts generation\n")
        callback("stderr", "downloading pexels material\n")
        callback("stdout", "render video\n")
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "final-1.mp4").write_bytes(b"new-video")
        (task_dir / "subtitle.srt").write_text(
            "1\n00:00:00,000 --> 00:00:05,000\n这是一段足够长并且有效的口播字幕内容。\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("content_pipeline.tools.narrated_mpt_client.run_command", fake_run)
    call_narrated_mpt(
        task_name=task_name,
        title="今日资讯",
        script="市场信息。" * 80,
        output_dir=tmp_path / "output",
        settings=Settings(mpt_dir=mpt_dir, mpt_python=Path("python")),
        on_progress=lambda percent, phase, message, estimate: updates.append((percent, phase, message, estimate)),
    )

    assert (15, "初始化 MoneyPrinterTurbo", "MoneyPrinterTurbo 已启动，正在准备视频任务", False) in updates
    assert (30, "生成配音与字幕", "MoneyPrinterTurbo 正在生成配音或字幕", True) in updates
    assert (45, "获取视频素材", "MoneyPrinterTurbo 正在获取视频素材", True) in updates
    assert (65, "渲染视频", "MoneyPrinterTurbo 正在合成视频", True) in updates
    assert (85, "复制视频产物", "视频、字幕和生成清单已写入任务目录", False) in updates


@pytest.mark.parametrize(
    ("characters", "expected"),
    [(1, 0.55), (350, 0.6), (482, 0.82), (500, 0.85), (1000, 1.2)],
)
def test_adaptive_voice_rate_targets_90_seconds_with_bounded_adaptation(characters: int, expected: float) -> None:
    assert _adaptive_voice_rate("x" * characters) == expected


def test_voice_rate_is_part_of_reuse_identity(tmp_path: Path, monkeypatch) -> None:
    mpt_dir = tmp_path / "mpt"
    output_dir = tmp_path / "output"
    task_name = "content-voice-rate"
    script = "市场信息。" * 80

    call_narrated_mpt(
        task_name=task_name,
        title="今日资讯",
        script=script,
        output_dir=output_dir,
        settings=Settings(mpt_dir=mpt_dir, mpt_python=Path("python")),
        dry_run=True,
        voice_rate=0.8,
    )
    first_manifest = json.loads((output_dir / "generation_manifest.json").read_text(encoding="utf-8"))
    assert first_manifest["voice_rate"] == 0.8

    task_dir = mpt_dir / "storage" / "tasks" / mpt_task_id(task_name)
    captured: list[str] = []

    def fake_run(command, **kwargs):
        captured.extend(command)
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "final-1.mp4").write_bytes(b"new-video")
        (task_dir / "subtitle.srt").write_text(
            "1\n00:00:00,000 --> 00:00:05,000\n这是一段足够长并且有效的口播字幕内容。\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("content_pipeline.tools.narrated_mpt_client.run_command", fake_run)
    call_narrated_mpt(
        task_name=task_name,
        title="今日资讯",
        script=script,
        output_dir=output_dir,
        settings=Settings(mpt_dir=mpt_dir, mpt_python=Path("python")),
        voice_rate=1.1,
    )

    assert captured[captured.index("--voice-rate") + 1] == "1.1"
    updated_manifest = json.loads((output_dir / "generation_manifest.json").read_text(encoding="utf-8"))
    assert updated_manifest["voice_rate"] == 1.1


def test_narrated_mpt_cleans_temporary_directory_when_run_fails(tmp_path: Path, monkeypatch) -> None:
    mpt_dir = tmp_path / "mpt"
    temporary_directory: Path | None = None

    def fake_run(_command, **kwargs):
        nonlocal temporary_directory
        environment = kwargs["env"]
        temporary_directory = Path(environment["TEMP"])
        assert temporary_directory.is_dir()
        assert environment["PYTHONIOENCODING"] == "utf-8"
        assert environment["PYTHONUTF8"] == "1"
        raise RuntimeError("run failed")

    monkeypatch.setattr("content_pipeline.tools.narrated_mpt_client.run_command", fake_run)

    with pytest.raises(RuntimeError, match="run failed"):
        call_narrated_mpt(
            task_name="failed-narration",
            title="今日资讯",
            script="这是一段足够长的口播稿。" * 10,
            output_dir=tmp_path / "output",
            settings=Settings(mpt_dir=mpt_dir, mpt_python=Path("python")),
        )

    assert temporary_directory is not None
    assert not temporary_directory.exists()


def test_generated_subtitle_path_must_stay_inside_task_dir(tmp_path: Path) -> None:
    task_dir = tmp_path / "mpt" / "storage" / "tasks" / "content-subtitle"
    task_dir.mkdir(parents=True)
    outside = tmp_path / "outside.srt"
    outside.write_text("subtitle", encoding="utf-8")

    with pytest.raises(ExternalToolError, match="invalid subtitle path"):
        _guard_generated_subtitle(outside, task_dir)


def test_generated_subtitle_path_rejects_symlink(tmp_path: Path) -> None:
    task_dir = tmp_path / "mpt" / "storage" / "tasks" / "content-subtitle"
    task_dir.mkdir(parents=True)
    target = task_dir / "target.srt"
    target.write_text("subtitle", encoding="utf-8")
    link = task_dir / "subtitle.srt"
    link.symlink_to(target)

    with pytest.raises(ExternalToolError, match="invalid subtitle path"):
        _guard_generated_subtitle(link, task_dir)
