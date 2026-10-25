"""Publishing safety policy — fail-closed rules for each platform.

All publishing defaults to OFF. Uploads default to private (self-only)
visibility; public visibility must be selected explicitly. Either way the
platform must prove the selected visibility before an upload is considered
successful. Any uncertainty results in a BLOCKED status.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "BILIBILI",
    "DOUYIN",
    "KUAISHOU",
    "PLATFORM_POLICIES",
    "XIAOHONGSHU",
    "PlatformPolicy",
    "get_policy",
    "validate_publish_request",
]


@dataclass(frozen=True)
class PlatformPolicy:
    platform: str
    visibility_requirement: str
    proof_required: str
    fail_closed_rule: str


# ---------------------------------------------------------------------------
# Platform policies
# ---------------------------------------------------------------------------

DOUYIN = PlatformPolicy(
    platform="douyin",
    visibility_requirement="支持 private（仅自己可见，默认）或 public（公开可见）",
    proof_required="SAU stdout must contain evidence of the selected visibility mode",
    fail_closed_rule="If the selected visibility is not confirmed in upload output, the target reports PUBLISH_FAILED.",
)

KUAISHOU = PlatformPolicy(
    platform="kuaishou",
    visibility_requirement="支持 private（仅自己可见，默认）或 public（公开可见）",
    proof_required="SAU stdout must contain evidence of the selected visibility mode",
    fail_closed_rule="If the selected visibility is not confirmed, the target reports PUBLISH_FAILED.",
)

BILIBILI = PlatformPolicy(
    platform="bilibili",
    visibility_requirement="支持 private（仅自己可见，默认）或 public（公开可见）",
    proof_required="SAU_BILIBILI_PRIVATE_ARGS must be configured and passed to the upload command for private uploads",
    fail_closed_rule=(
        "Bilibili upload is BLOCKED entirely if visibility is private and "
        "SAU_BILIBILI_PRIVATE_ARGS='--is-only-self 1' is not configured. "
        "A configured but failed upload reports PUBLISH_FAILED."
    ),
)

XIAOHONGSHU = PlatformPolicy(
    platform="xiaohongshu",
    visibility_requirement="支持 private（仅自己可见，默认）或 public（公开可见）",
    proof_required=(
        "SAU stdout or stderr must contain evidence of the selected visibility mode plus 图文发布成功 or 发布成功"
    ),
    fail_closed_rule=(
        "If the selected visibility or publication success is not confirmed, the target reports PUBLISH_FAILED."
    ),
)

PLATFORM_POLICIES: dict[str, PlatformPolicy] = {p.platform: p for p in [DOUYIN, KUAISHOU, BILIBILI, XIAOHONGSHU]}


def get_policy(platform: str) -> PlatformPolicy:
    """Return the publishing policy for *platform*."""
    return PLATFORM_POLICIES[platform]


def validate_publish_request(
    platform: str,
    publish_enabled: bool,
    private_args_configured: bool,
    *,
    visibility: str = "private",
) -> list[str]:
    """Return a list of blocking reasons. Empty list means the publish can proceed."""
    blockers: list[str] = []

    if not publish_enabled:
        blockers.append("publish is not enabled on this task")
        return blockers

    policy = get_policy(platform)

    if platform == "bilibili" and visibility == "private" and not private_args_configured:
        blockers.append(f"Bilibili requires SAU_BILIBILI_PRIVATE_ARGS: {policy.fail_closed_rule}")

    return blockers
