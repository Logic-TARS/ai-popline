from __future__ import annotations

import logging
import shlex
from pathlib import Path
from typing import Any

from content_pipeline.account_ledger import ensure_account_publishable, resolve_publish_account
from content_pipeline.core.publishing.policy import validate_publish_request
from content_pipeline.errors import ConfigError, ExternalToolError, PrivateVisibilityUnsupportedError
from content_pipeline.models import PublishTarget
from content_pipeline.profiles import UploadProfile
from content_pipeline.rendering import render_template
from content_pipeline.settings import Settings
from content_pipeline.tools.common import run_command

__all__ = ["call_sau_target", "call_sau_upload", "call_sau_xiaohongshu_note"]

_logger = logging.getLogger(__name__)


def _resolve_account_identity(platform: str, ref: str, settings: Settings) -> str:
    """把账号引用解析成 SAU 的当前身份名（cookie 文件名）。

    SAU 用身份名当账号标识，改标识后名字会变；账本以平台 UID 锚定账号，
    所以这里把旧引用换成当前身份名。账本不可用时返回原值（fail-open）；
    账本可用但引用查不到时抛 ConfigError —— 绝不静默沿用旧名字。
    解析成功后按状态策略告警，``expired`` 直接早失败。

    解析在 ``dry_run`` 下也执行，这样 ``--dry-run`` 打印出的命令行能直接
    核对解析结果；``_enforce_publish_policy`` 的 dry_run 行为不变。
    """
    resolved = resolve_publish_account(settings, platform, ref)
    if resolved is None:
        return str(ref or "")
    for warning in ensure_account_publishable(resolved):
        _logger.warning("%s", warning)
    return resolved.identity


def _bilibili_private_args(settings: Settings) -> list[str]:
    private_args = shlex.split(settings.sau_bilibili_private_args)
    for index, item in enumerate(private_args):
        if item == "--is-only-self" and index + 1 < len(private_args) and private_args[index + 1] == "1":
            return private_args
        if item == "--is-only-self=1":
            return private_args
    raise PrivateVisibilityUnsupportedError(
        "Bilibili private upload requires SAU_BILIBILI_PRIVATE_ARGS='--is-only-self 1'."
    )


def _already_published_result(error: ExternalToolError) -> dict[str, Any] | None:
    text = str(error)
    if "此内容已在" not in text or "小时内发布过" not in text:
        return None
    return {
        "success": False,
        "skipped": True,
        "reason": "already_published_unverified",
        "visibility": "unknown",
        "error": "上传工具命中本地发布历史去重，但平台未返回本次发布成功凭证。请检查平台后台，或绕过去重后重试。",
        "publication_proof": None,
    }


def call_sau_xiaohongshu_note(
    *,
    images: list[Path],
    title: str,
    note: str = "",
    tags: list[str] | None = None,
    account: str,
    settings: Settings,
    schedule: str | None = None,
    debug: bool = False,
    headed: bool = False,
    dry_run: bool = False,
    visibility: str = "private",
) -> dict[str, Any]:
    if visibility not in {"private", "public"}:
        raise ConfigError(f"unsupported xiaohongshu visibility: {visibility}")
    if not images:
        raise ConfigError("xiaohongshu image note upload requires at least one image")
    account_identity = _resolve_account_identity("xiaohongshu", account, settings)
    command = [
        str(settings.sau_exe),
        "xiaohongshu",
        "upload-note",
        "--account",
        account_identity,
        "--images",
        *[str(image.resolve()) for image in images],
        "--title",
        title,
        "--visibility",
        visibility,
    ]
    if note:
        command.extend(["--note", note])
    if tags:
        command.extend(["--tags", ",".join(tags)])
    if schedule:
        command.extend(["--schedule", schedule])
    if debug:
        command.append("--debug")
    command.append("--headed" if headed else "--headless")

    if dry_run:
        return {
            "success": True,
            "dry_run": True,
            "command": command,
            "visibility": visibility,
            "publication_proof": "skipped_in_dry_run",
        }

    _enforce_publish_policy("xiaohongshu", settings, visibility=visibility)
    result = run_command(command, cwd=settings.sau_dir, timeout=1800, retries=0)
    output = "\n".join((result.stdout, result.stderr))
    visibility_proof = "已设置为“仅自己可见”" if visibility == "private" else "已设置为“公开可见”"
    proof = "图文发布成功" if "图文发布成功" in output else "发布成功" if "发布成功" in output else None
    if visibility_proof not in output or proof is None:
        raise PrivateVisibilityUnsupportedError(
            "Xiaohongshu note upload did not provide proof of the selected visibility and successful publication."
        )
    return {
        "success": True,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
        "visibility": visibility,
        "publication_proof": proof,
    }


def call_sau_target(
    *,
    target: PublishTarget,
    video: Path,
    title: str,
    desc: str,
    tags: list[str],
    settings: Settings,
    dry_run: bool = False,
) -> dict[str, Any]:
    if target.platform not in {"douyin", "kuaishou", "bilibili"}:
        raise ConfigError(f"unsupported upload platform: {target.platform}")
    account_identity = _resolve_account_identity(target.platform, target.account, settings)
    if not dry_run:
        _enforce_publish_policy(target.platform, settings, visibility=target.visibility)

    if target.platform == "douyin":
        command = [
            str(settings.sau_exe),
            "douyin",
            "upload-video",
            "--account",
            account_identity,
            "--file",
            str(video.resolve()),
            "--title",
            title[:30],
            "--desc",
            desc,
            "--headless",
        ]
        if tags:
            command.extend(["--tags", ",".join(tags)])
        if dry_run:
            return {"success": True, "dry_run": True, "command": command, "visibility": target.visibility}
        try:
            result = run_command(command, cwd=settings.sau_dir, timeout=1800, retries=0)
        except ExternalToolError as exc:
            already_published = _already_published_result(exc)
            if already_published:
                return already_published
            raise
        output = "\n".join((result.stdout, result.stderr))
        if target.visibility == "public":
            if "已设置为“公开可见”" not in output or "视频发布成功" not in output:
                raise PrivateVisibilityUnsupportedError(
                    "Douyin upload did not provide proof of public visibility and successful publication."
                )
            return {
                "success": True,
                "stdout": result.stdout.strip(),
                "stderr": result.stderr.strip(),
                "visibility": "public",
                "publication_proof": "视频发布成功",
            }
        if "已设置为“仅自己可见”" not in output or "视频发布成功" not in output:
            raise PrivateVisibilityUnsupportedError(
                "Douyin upload did not provide proof of private visibility and successful publication."
            )
        return {
            "success": True,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
            "visibility": "private",
            "private_visibility_proof": "已设置为“仅自己可见”",
            "publication_proof": "视频发布成功",
        }

    if target.platform == "kuaishou":
        command = [
            str(settings.sau_exe),
            "kuaishou",
            "upload-video",
            "--account",
            account_identity,
            "--file",
            str(video.resolve()),
            "--title",
            title[:30],
            "--desc",
            desc,
            "--headed",
            "--debug",
            "--visibility",
            target.visibility,
        ]
        if tags:
            command.extend(["--tags", ",".join(tags)])
        if dry_run:
            return {
                "success": True,
                "dry_run": True,
                "command": command,
                "visibility": target.visibility,
                "private_visibility_check": "skipped_in_dry_run",
            }
        try:
            result = run_command(command, cwd=settings.sau_dir, timeout=1800, retries=0)
        except ExternalToolError as exc:
            already_published = _already_published_result(exc)
            if already_published:
                return already_published
            raise
        output = "\n".join((result.stdout, result.stderr))
        if target.visibility == "public":
            if "已设置为“公开可见”" not in output or "视频发布成功" not in output:
                raise PrivateVisibilityUnsupportedError(
                    "Kuaishou upload did not provide proof of public visibility and successful publication."
                )
            return {
                "success": True,
                "stdout": result.stdout.strip(),
                "stderr": result.stderr.strip(),
                "visibility": "public",
                "publication_proof": "视频发布成功",
            }
        if "已设置为“仅自己可见”" not in output or "视频发布成功" not in output:
            raise PrivateVisibilityUnsupportedError(
                "Kuaishou upload did not provide proof of private visibility and successful publication."
            )
        return {
            "success": True,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
            "visibility": "private",
            "private_visibility_proof": "已设置为“仅自己可见”",
            "publication_proof": "视频发布成功",
        }

    if target.platform == "bilibili":
        command = [
            str(settings.sau_exe),
            "bilibili",
            "upload-video",
            "--account",
            account_identity,
            "--file",
            str(video.resolve()),
            "--title",
            title[:80],
            "--desc",
            desc,
            "--tid",
            str(target.tid),
        ]
        if tags:
            command.extend(["--tags", ",".join(tags)])
        if dry_run:
            return {"success": True, "dry_run": True, "command": command, "visibility": target.visibility}
        if target.visibility == "public":
            result = run_command(command, cwd=settings.sau_dir, timeout=1800, retries=2)
            return {
                "success": True,
                "stdout": result.stdout.strip(),
                "stderr": result.stderr.strip(),
                "visibility": "public",
                "publication_proof": "biliup exited successfully",
            }
        private_args = _bilibili_private_args(settings)
        command.extend(private_args)
        result = run_command(command, cwd=settings.sau_dir, timeout=1800, retries=2)
        return {
            "success": True,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
            "visibility": "private",
            "private_visibility_proof": "biliup --is-only-self 1",
            "publication_proof": "biliup exited successfully",
        }
    raise ConfigError(f"unsupported upload platform: {target.platform}")


def _enforce_publish_policy(platform: str, settings: Settings, *, visibility: str = "private") -> None:
    blockers = validate_publish_request(
        platform=platform,
        publish_enabled=True,
        private_args_configured=bool(settings.sau_bilibili_private_args),
        visibility=visibility,
    )
    if blockers:
        raise PrivateVisibilityUnsupportedError("; ".join(blockers))


def call_sau_upload(
    *,
    profile: UploadProfile,
    topic: str,
    params: dict[str, Any],
    video: Path,
    settings: Settings,
) -> dict[str, Any]:
    if profile.platform != "bilibili":
        raise ConfigError(f"unsupported upload platform: {profile.platform}")
    if profile.visibility != "private":
        raise ConfigError("only private upload visibility is allowed")

    title = render_template(profile.title_template, topic, params)
    desc = render_template(profile.desc_template, topic, params)
    account_identity = _resolve_account_identity("bilibili", profile.account, settings)
    command = [
        str(settings.sau_exe),
        "bilibili",
        "upload-video",
        "--account",
        account_identity,
        "--file",
        str(video),
        "--title",
        title,
        "--desc",
        desc,
        "--tid",
        str(profile.tid),
    ]
    if profile.tags:
        command.extend(["--tags", ",".join(profile.tags)])
    if profile.schedule:
        command.extend(["--schedule", profile.schedule])
    if params.get("dry_run"):
        return {
            "success": True,
            "dry_run": True,
            "command": command,
            "visibility": "private",
            "private_visibility_check": "skipped_in_dry_run",
        }

    _enforce_publish_policy("bilibili", settings)
    private_args = _bilibili_private_args(settings)
    command.extend(private_args)

    result = run_command(command, cwd=settings.sau_dir, timeout=1800, retries=2)
    return {
        "success": True,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
        "visibility": "private",
        "private_visibility_proof": "biliup --is-only-self 1",
        "publication_proof": "biliup exited successfully",
    }
