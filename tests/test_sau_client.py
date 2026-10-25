import sqlite3
import subprocess
from pathlib import Path

import pytest

from content_pipeline.account_ledger import LedgerAccount
from content_pipeline.errors import ConfigError, ExternalToolError, PrivateVisibilityUnsupportedError
from content_pipeline.models import PublishTarget
from content_pipeline.profiles import UploadProfile
from content_pipeline.settings import Settings
from content_pipeline.tools.sau_client import (
    _resolve_account_identity,
    call_sau_target,
    call_sau_upload,
    call_sau_xiaohongshu_note,
)


@pytest.fixture(autouse=True)
def _isolated_sau_dir(tmp_path, monkeypatch):
    """Ensure tests don't pick up the real SAU ledger at the default sau_dir."""
    monkeypatch.setenv("SAU_DIR", str(tmp_path / "nonexistent_sau"))


def test_douyin_target_dry_run_is_private(tmp_path: Path) -> None:
    video = tmp_path / "demo.mp4"
    video.write_bytes(b"x")
    result = call_sau_target(
        target=PublishTarget(platform="douyin", account="山下富士"),
        video=video,
        title="title",
        desc="desc",
        tags=["AI绘画"],
        settings=Settings(),
        dry_run=True,
    )
    assert result["visibility"] == "private"
    assert "--headless" in result["command"]


def test_kuaishou_target_dry_run_is_private_and_headed(tmp_path: Path) -> None:
    video = tmp_path / "demo.mp4"
    video.write_bytes(b"x")
    result = call_sau_target(
        target=PublishTarget(platform="kuaishou", account="破壁人"),
        video=video,
        title="title",
        desc="desc",
        tags=["基金"],
        settings=Settings(),
        dry_run=True,
    )
    assert result["visibility"] == "private"
    assert "--headed" in result["command"]
    assert "--force" not in result["command"]
    file_arg = result["command"][result["command"].index("--file") + 1]
    assert Path(file_arg).is_absolute()


def test_kuaishou_requires_private_and_success_proof(tmp_path: Path, monkeypatch) -> None:
    video = tmp_path / "demo.mp4"
    video.write_bytes(b"x")

    def fake_run_command(command, **kwargs):
        import subprocess

        return subprocess.CompletedProcess(args=command, returncode=0, stdout="视频发布成功", stderr="")

    monkeypatch.setattr("content_pipeline.tools.sau_client.run_command", fake_run_command)
    with pytest.raises(PrivateVisibilityUnsupportedError, match="Kuaishou"):
        call_sau_target(
            target=PublishTarget(platform="kuaishou", account="破壁人"),
            video=video,
            title="title",
            desc="desc",
            tags=["基金"],
            settings=Settings(),
        )


def test_publish_history_duplicate_is_unverified_failure(tmp_path: Path, monkeypatch) -> None:
    video = tmp_path / "demo.mp4"
    video.write_bytes(b"x")

    def fake_run_command(command, **kwargs):
        raise ExternalToolError("[douyin/金融破壁人] 此内容已在 24 小时内发布过，或上次点击发布后状态未知。")

    monkeypatch.setattr("content_pipeline.tools.sau_client.run_command", fake_run_command)
    result = call_sau_target(
        target=PublishTarget(platform="douyin", account="金融破壁人"),
        video=video,
        title="title",
        desc="desc",
        tags=["基金"],
        settings=Settings(),
    )
    assert result["success"] is False
    assert result["skipped"] is True
    assert result["reason"] == "already_published_unverified"
    assert result["visibility"] == "unknown"
    assert result["publication_proof"] is None
    assert "平台未返回本次发布成功凭证" in result["error"]


def test_bilibili_target_requires_tid() -> None:
    with pytest.raises(ValueError, match="tid"):
        PublishTarget(platform="bilibili", account="default")


def test_tencent_target_is_rejected(tmp_path: Path) -> None:
    video = tmp_path / "demo.mp4"
    video.write_bytes(b"x")
    with pytest.raises(ConfigError, match="unsupported upload platform"):
        call_sau_target(
            target=PublishTarget(platform="tencent", account="破壁人Wallbreaker"),
            video=video,
            title="视频号验收标题超过十六个字符",
            desc="只保存草稿",
            tags=["AI", "测试"],
            settings=Settings(),
            dry_run=True,
        )


def test_dry_run_upload_does_not_require_private_args(tmp_path: Path) -> None:
    video = tmp_path / "demo.mp4"
    video.write_bytes(b"x")
    result = call_sau_upload(
        profile=UploadProfile(account="default", tid=249, visibility="private"),
        topic="topic",
        params={"dry_run": True},
        video=video,
        settings=Settings(),
    )
    assert result["dry_run"] is True
    assert result["visibility"] == "private"


def test_real_upload_requires_private_args(tmp_path: Path, monkeypatch) -> None:
    video = tmp_path / "demo.mp4"
    video.write_bytes(b"x")
    captured_command = None

    def fake_run_command(command, **kwargs):
        nonlocal captured_command
        captured_command = command
        import subprocess

        return subprocess.CompletedProcess(args=command, returncode=0, stdout="uploaded", stderr="")

    monkeypatch.setattr("content_pipeline.tools.sau_client.run_command", fake_run_command)

    with pytest.raises(PrivateVisibilityUnsupportedError, match="--is-only-self 1"):
        call_sau_upload(
            profile=UploadProfile(account="default", tid=249, visibility="private"),
            topic="topic",
            params={},
            video=video,
            settings=Settings(sau_bilibili_private_args=""),
        )
    assert captured_command is None


def test_real_bilibili_upload_passes_verified_private_flag(tmp_path: Path, monkeypatch) -> None:
    video = tmp_path / "demo.mp4"
    video.write_bytes(b"x")
    captured_command = None

    def fake_run_command(command, **kwargs):
        nonlocal captured_command
        captured_command = command
        import subprocess

        return subprocess.CompletedProcess(args=command, returncode=0, stdout="uploaded", stderr="")

    monkeypatch.setattr("content_pipeline.tools.sau_client.run_command", fake_run_command)
    result = call_sau_target(
        target=PublishTarget(platform="bilibili", account="酸菜鱼-sakana", tid=27),
        video=video,
        title="title",
        desc="desc",
        tags=["动漫"],
        settings=Settings(sau_bilibili_private_args="--is-only-self 1"),
    )

    assert captured_command[-2:] == ["--is-only-self", "1"]
    assert result["visibility"] == "private"
    assert result["private_visibility_proof"] == "biliup --is-only-self 1"


def test_xiaohongshu_note_dry_run_builds_upload_note_command(tmp_path: Path) -> None:
    image1 = tmp_path / "1.png"
    image2 = tmp_path / "2.png"
    image1.write_bytes(b"x")
    image2.write_bytes(b"x")

    result = call_sau_xiaohongshu_note(
        images=[image1, image2],
        title="小红书标题",
        note="正文",
        tags=["AI绘画", "小红书图文"],
        account="小红书账号",
        settings=Settings(),
        dry_run=True,
    )

    command = result["command"]
    assert command[:3] == [str(Settings().sau_exe), "xiaohongshu", "upload-note"]
    assert command[command.index("--account") + 1] == "小红书账号"
    image_args = command[command.index("--images") + 1 : command.index("--title")]
    assert [Path(value).name for value in image_args] == ["1.png", "2.png"]
    assert all(Path(value).is_absolute() for value in image_args)
    assert command[command.index("--title") + 1] == "小红书标题"
    assert command[command.index("--visibility") + 1] == "private"
    assert command[command.index("--note") + 1] == "正文"
    assert command[command.index("--tags") + 1] == "AI绘画,小红书图文"
    assert "--headless" in command
    assert result["visibility"] == "private"
    assert result["publication_proof"] == "skipped_in_dry_run"


def test_xiaohongshu_note_requires_success_proof(tmp_path: Path, monkeypatch) -> None:
    image = tmp_path / "1.png"
    image.write_bytes(b"x")

    def fake_run_command(command, **kwargs):
        import subprocess

        return subprocess.CompletedProcess(args=command, returncode=0, stdout="上传完成", stderr="")

    monkeypatch.setattr("content_pipeline.tools.sau_client.run_command", fake_run_command)
    with pytest.raises(PrivateVisibilityUnsupportedError, match="Xiaohongshu"):
        call_sau_xiaohongshu_note(
            images=[image],
            title="小红书标题",
            account="小红书账号",
            settings=Settings(),
        )


def test_xiaohongshu_note_accepts_success_proof(tmp_path: Path, monkeypatch) -> None:
    image = tmp_path / "1.png"
    image.write_bytes(b"x")

    def fake_run_command(command, **kwargs):
        import subprocess

        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout="已设置为“仅自己可见”\n图文发布成功",
            stderr="",
        )

    monkeypatch.setattr("content_pipeline.tools.sau_client.run_command", fake_run_command)
    result = call_sau_xiaohongshu_note(
        images=[image],
        title="小红书标题",
        account="小红书账号",
        settings=Settings(),
    )

    assert result["success"] is True
    assert result["visibility"] == "private"
    assert result["publication_proof"] == "图文发布成功"


def test_douyin_public_target_requires_public_visibility_proof(tmp_path: Path, monkeypatch) -> None:
    video = tmp_path / "demo.mp4"
    video.write_bytes(b"x")
    target = PublishTarget(platform="douyin", account="山下富士", visibility="public")

    dry_run = call_sau_target(
        target=target,
        video=video,
        title="title",
        desc="desc",
        tags=["AI绘画"],
        settings=Settings(),
        dry_run=True,
    )
    assert dry_run["visibility"] == "public"
    assert "--visibility" not in dry_run["command"]

    captured_command = None

    def fake_run_command(command, **kwargs):
        nonlocal captured_command
        captured_command = command
        import subprocess

        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout="已设置为“公开可见”\n视频发布成功",
            stderr="",
        )

    monkeypatch.setattr("content_pipeline.tools.sau_client.run_command", fake_run_command)
    result = call_sau_target(
        target=target,
        video=video,
        title="title",
        desc="desc",
        tags=["AI绘画"],
        settings=Settings(),
    )
    assert "--visibility" not in captured_command
    assert result["visibility"] == "public"
    assert result["publication_proof"] == "视频发布成功"
    assert "private_visibility_proof" not in result

    def fake_run_command_without_proof(command, **kwargs):
        import subprocess

        return subprocess.CompletedProcess(args=command, returncode=0, stdout="视频发布成功", stderr="")

    monkeypatch.setattr("content_pipeline.tools.sau_client.run_command", fake_run_command_without_proof)
    with pytest.raises(PrivateVisibilityUnsupportedError, match="Douyin"):
        call_sau_target(
            target=target,
            video=video,
            title="title",
            desc="desc",
            tags=["AI绘画"],
            settings=Settings(),
        )


def test_kuaishou_public_target_requires_public_visibility_proof(tmp_path: Path, monkeypatch) -> None:
    video = tmp_path / "demo.mp4"
    video.write_bytes(b"x")
    target = PublishTarget(platform="kuaishou", account="破壁人", visibility="public")

    dry_run = call_sau_target(
        target=target,
        video=video,
        title="title",
        desc="desc",
        tags=["基金"],
        settings=Settings(),
        dry_run=True,
    )
    assert dry_run["visibility"] == "public"
    assert "--visibility" not in dry_run["command"]

    def fake_run_command(command, **kwargs):
        import subprocess

        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout="已设置为“公开可见”\n视频发布成功",
            stderr="",
        )

    monkeypatch.setattr("content_pipeline.tools.sau_client.run_command", fake_run_command)
    result = call_sau_target(
        target=target,
        video=video,
        title="title",
        desc="desc",
        tags=["基金"],
        settings=Settings(),
    )
    assert result["visibility"] == "public"
    assert result["publication_proof"] == "视频发布成功"
    assert "private_visibility_proof" not in result

    def fake_run_command_without_proof(command, **kwargs):
        import subprocess

        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout="已设置为“仅自己可见”\n视频发布成功",
            stderr="",
        )

    monkeypatch.setattr("content_pipeline.tools.sau_client.run_command", fake_run_command_without_proof)
    with pytest.raises(PrivateVisibilityUnsupportedError, match="Kuaishou"):
        call_sau_target(
            target=target,
            video=video,
            title="title",
            desc="desc",
            tags=["基金"],
            settings=Settings(),
        )


def test_bilibili_public_target_skips_private_args(tmp_path: Path, monkeypatch) -> None:
    video = tmp_path / "demo.mp4"
    video.write_bytes(b"x")
    captured_command = None

    def fake_run_command(command, **kwargs):
        nonlocal captured_command
        captured_command = command
        import subprocess

        return subprocess.CompletedProcess(args=command, returncode=0, stdout="uploaded", stderr="")

    monkeypatch.setattr("content_pipeline.tools.sau_client.run_command", fake_run_command)
    result = call_sau_target(
        target=PublishTarget(platform="bilibili", account="酸菜鱼-sakana", tid=27, visibility="public"),
        video=video,
        title="title",
        desc="desc",
        tags=["动漫"],
        settings=Settings(sau_bilibili_private_args="--is-only-self 1"),
    )

    assert "--is-only-self" not in captured_command
    assert result["visibility"] == "public"
    assert result["publication_proof"] == "biliup exited successfully"
    assert "private_visibility_proof" not in result


def test_xiaohongshu_note_public_requires_public_visibility_proof(tmp_path: Path, monkeypatch) -> None:
    image = tmp_path / "1.png"
    image.write_bytes(b"x")
    captured_command = None

    def fake_run_command(command, **kwargs):
        nonlocal captured_command
        captured_command = command
        import subprocess

        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout="已设置为“公开可见”\n图文发布成功",
            stderr="",
        )

    monkeypatch.setattr("content_pipeline.tools.sau_client.run_command", fake_run_command)
    result = call_sau_xiaohongshu_note(
        images=[image],
        title="小红书标题",
        account="小红书账号",
        settings=Settings(),
        visibility="public",
    )

    assert captured_command[captured_command.index("--visibility") + 1] == "public"
    assert result["success"] is True
    assert result["visibility"] == "public"
    assert result["publication_proof"] == "图文发布成功"

    def fake_run_command_without_proof(command, **kwargs):
        import subprocess

        return subprocess.CompletedProcess(args=command, returncode=0, stdout="图文发布成功", stderr="")

    monkeypatch.setattr("content_pipeline.tools.sau_client.run_command", fake_run_command_without_proof)
    with pytest.raises(PrivateVisibilityUnsupportedError, match="Xiaohongshu"):
        call_sau_xiaohongshu_note(
            images=[image],
            title="小红书标题",
            account="小红书账号",
            settings=Settings(),
            visibility="public",
        )


def test_xiaohongshu_note_rejects_unsupported_visibility(tmp_path: Path) -> None:
    image = tmp_path / "1.png"
    image.write_bytes(b"x")

    with pytest.raises(ConfigError, match="unsupported xiaohongshu visibility"):
        call_sau_xiaohongshu_note(
            images=[image],
            title="小红书标题",
            account="小红书账号",
            settings=Settings(),
            visibility="friends",
        )


def _sau_accounts(accounts: dict[str, list[str]] | None):
    """Inject a canned SAU account list via the ledger resolver.

    ``resolve_publish_account`` itself (SQLite read, matching) is covered in
    ``tests/unit/test_account_ledger.py``.
    """
    if accounts is None:
        return lambda settings, platform, ref: None

    def fake_resolve(settings, platform, ref):
        known = accounts.get(platform) or []
        if ref in known:
            return LedgerAccount(
                platform=platform,
                platform_uid="0",
                identity=ref,
                nickname=ref,
                public_id=None,
                status="valid",
                checked_at="2026-09-22T00:00:00+08:00",
                uid_source="cookie",
            )
        available = "、".join(known) if known else f"SAU 账号账本里没有 {platform} 平台的任何账号"
        raise ConfigError(
            f"SAU 账号账本里没有 {platform} 账号「{ref}」：账号可能已在 SAU 改过标识或换号。"
            f"当前可用账号：{available}。"
            "若刚在 SAU 新增/改过账号，请先运行 `sau accounts sync` 刷新账本。"
        )

    return fake_resolve


def _bridge_settings(**overrides) -> Settings:
    return Settings(sau_bridge_url="http://bridge.test:5800", **overrides)


def _publish_output(stdout: str):
    def fake_run_command(command, **kwargs):
        return subprocess.CompletedProcess(args=command, returncode=0, stdout=stdout, stderr="")

    return fake_run_command


def _publish_proof() -> str:
    return "已设置为“仅自己可见”\n视频发布成功"


def test_sau_target_refuses_account_that_sau_does_not_have(tmp_path: Path, monkeypatch) -> None:
    video = tmp_path / "demo.mp4"
    video.write_bytes(b"x")
    monkeypatch.setattr(
        "content_pipeline.tools.sau_client.resolve_publish_account",
        _sau_accounts({"kuaishou": ["搞AI的罗辑同学"]}),
    )
    published = []
    monkeypatch.setattr(
        "content_pipeline.tools.sau_client.run_command",
        lambda command, **kwargs: published.append(command),
    )

    with pytest.raises(ConfigError):
        call_sau_target(
            target=PublishTarget(platform="kuaishou", account="破壁人"),
            video=video,
            title="title",
            desc="desc",
            tags=["基金"],
            settings=_bridge_settings(),
        )

    assert published == []


def test_missing_account_error_names_the_account_and_sau_alternatives(tmp_path: Path, monkeypatch) -> None:
    video = tmp_path / "demo.mp4"
    video.write_bytes(b"x")
    monkeypatch.setattr(
        "content_pipeline.tools.sau_client.resolve_publish_account",
        _sau_accounts({"kuaishou": ["搞AI的罗辑同学", "备用号"]}),
    )
    monkeypatch.setattr("content_pipeline.tools.sau_client.run_command", _publish_output(_publish_proof()))

    with pytest.raises(ConfigError) as excinfo:
        call_sau_target(
            target=PublishTarget(platform="kuaishou", account="破壁人"),
            video=video,
            title="title",
            desc="desc",
            tags=["基金"],
            settings=_bridge_settings(),
        )

    message = str(excinfo.value)
    assert "破壁人" in message
    assert "搞AI的罗辑同学" in message
    assert "备用号" in message


def test_sau_target_publishes_when_sau_has_the_account(tmp_path: Path, monkeypatch) -> None:
    video = tmp_path / "demo.mp4"
    video.write_bytes(b"x")
    monkeypatch.setattr(
        "content_pipeline.tools.sau_client.resolve_publish_account",
        _sau_accounts({"kuaishou": ["搞AI的罗辑同学"]}),
    )
    monkeypatch.setattr("content_pipeline.tools.sau_client.run_command", _publish_output(_publish_proof()))

    result = call_sau_target(
        target=PublishTarget(platform="kuaishou", account="搞AI的罗辑同学"),
        video=video,
        title="title",
        desc="desc",
        tags=["基金"],
        settings=_bridge_settings(),
    )

    assert result["success"] is True
    assert result["publication_proof"] == "视频发布成功"


def test_sau_target_publishes_when_sau_accounts_are_unavailable(tmp_path: Path, monkeypatch) -> None:
    video = tmp_path / "demo.mp4"
    video.write_bytes(b"x")
    monkeypatch.setattr(
        "content_pipeline.tools.sau_client.resolve_publish_account",
        _sau_accounts(None),
    )
    monkeypatch.setattr("content_pipeline.tools.sau_client.run_command", _publish_output(_publish_proof()))

    result = call_sau_target(
        target=PublishTarget(platform="kuaishou", account="搞AI的罗辑同学"),
        video=video,
        title="title",
        desc="desc",
        tags=["基金"],
        settings=_bridge_settings(),
    )

    assert result["success"] is True


def test_sau_target_dry_run_resolves_account_identity(tmp_path: Path, monkeypatch) -> None:
    """dry_run 也执行账号解析，这样命令行的 --account 可以直接核对。"""
    video = tmp_path / "demo.mp4"
    video.write_bytes(b"x")

    def fake_resolve(settings, platform, ref):
        return LedgerAccount(
            platform=platform,
            platform_uid="0",
            identity="解析后的身份",
            nickname=ref,
            public_id=None,
            status="valid",
            checked_at="2026-09-22T00:00:00+08:00",
            uid_source="cookie",
        )

    monkeypatch.setattr("content_pipeline.tools.sau_client.resolve_publish_account", fake_resolve)

    result = call_sau_target(
        target=PublishTarget(platform="kuaishou", account="破壁人"),
        video=video,
        title="title",
        desc="desc",
        tags=["基金"],
        settings=Settings(),
        dry_run=True,
    )

    assert result["dry_run"] is True
    account_idx = result["command"].index("--account")
    assert result["command"][account_idx + 1] == "解析后的身份"


def test_xiaohongshu_note_refuses_account_that_sau_does_not_have(tmp_path: Path, monkeypatch) -> None:
    image = tmp_path / "1.png"
    image.write_bytes(b"x")
    monkeypatch.setattr(
        "content_pipeline.tools.sau_client.resolve_publish_account",
        _sau_accounts({"xiaohongshu": ["山下富士"]}),
    )
    published = []
    monkeypatch.setattr(
        "content_pipeline.tools.sau_client.run_command",
        lambda command, **kwargs: published.append(command),
    )

    with pytest.raises(ConfigError):
        call_sau_xiaohongshu_note(
            images=[image],
            title="小红书标题",
            account="小红书账号",
            settings=_bridge_settings(),
            visibility="public",
        )

    assert published == []


# ---------------------------------------------------------------------------
# Task 6: ledger-based account resolution (_resolve_account_identity)
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE accounts (
  platform TEXT NOT NULL, platform_uid TEXT NOT NULL, identity TEXT NOT NULL,
  nickname TEXT, public_id TEXT, display_name TEXT,
  status TEXT NOT NULL DEFAULT 'unknown', status_message TEXT, checked_at TEXT,
  uid_source TEXT NOT NULL DEFAULT 'unknown', first_seen_at TEXT NOT NULL,
  last_login_at TEXT, PRIMARY KEY (platform, platform_uid)
);
"""


@pytest.fixture()
def ledger_settings(tmp_path, monkeypatch):
    sau_dir = tmp_path / "sau"
    (sau_dir / "db").mkdir(parents=True)
    conn = sqlite3.connect(sau_dir / "db" / "accounts.db")
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO accounts (platform, platform_uid, identity, nickname, status,"
        " uid_source, first_seen_at) VALUES ('kuaishou','5362435333','搞AI的罗辑同学',"
        "'金融破壁人','valid','cookie','2026-09-22T00:00:00+08:00')"
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("SAU_DIR", str(sau_dir))
    return Settings()


def test_stale_name_is_rejected_loudly(ledger_settings):
    """旧名字不再兜底解析——响亮失败，避免静默发到错账号。"""
    with pytest.raises(ConfigError):
        _resolve_account_identity("kuaishou", "破壁人", ledger_settings)


def test_uid_resolves_to_current_identity(ledger_settings):
    assert _resolve_account_identity("kuaishou", "5362435333", ledger_settings) == "搞AI的罗辑同学"


def test_nickname_resolves_to_current_identity(ledger_settings):
    assert _resolve_account_identity("kuaishou", "金融破壁人", ledger_settings) == "搞AI的罗辑同学"


def test_unresolvable_name_raises(ledger_settings):
    with pytest.raises(ConfigError):
        _resolve_account_identity("kuaishou", "不存在的号", ledger_settings)


def test_missing_ledger_falls_back_to_reference(tmp_path, monkeypatch):
    monkeypatch.setenv("SAU_DIR", str(tmp_path / "nope"))
    assert _resolve_account_identity("kuaishou", "搞AI的罗辑同学", Settings()) == "搞AI的罗辑同学"


def test_expired_account_blocks_before_upload(tmp_path, monkeypatch):
    """账本里 expired 的账号必须早失败。"""
    sau_dir = tmp_path / "sau_expired"
    (sau_dir / "db").mkdir(parents=True)
    conn = sqlite3.connect(sau_dir / "db" / "accounts.db")
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO accounts (platform, platform_uid, identity, nickname, status,"
        " uid_source, first_seen_at) VALUES ('kuaishou','5362435333','搞AI的罗辑同学',"
        "'金融破壁人','expired','cookie','2026-09-22T00:00:00+08:00')"
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("SAU_DIR", str(sau_dir))
    with pytest.raises(ConfigError) as excinfo:
        _resolve_account_identity("kuaishou", "5362435333", Settings())
    assert "sau kuaishou login" in str(excinfo.value)


def test_drift_warning_is_logged_not_raised(ledger_settings, caplog):
    with caplog.at_level("WARNING"):
        identity = _resolve_account_identity("kuaishou", "5362435333", ledger_settings)
    assert identity == "搞AI的罗辑同学"
    assert "金融破壁人" in caplog.text
