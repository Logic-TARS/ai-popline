"""Central publish policy: one place that decides whether and where each content type publishes.

Resolution order for a task:
1. ``task.publish`` / ``task.publish_targets`` — explicit per-task choice always wins.
2. Operator overrides saved via the web console (``data_dir/publish_policy.json``).
3. Packaged defaults in ``config/publish.defaults.yaml`` (or PUBLISH_DEFAULTS_FILE).
"""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from content_pipeline.errors import ConfigError
from content_pipeline.models import PublishTarget, TaskInput
from content_pipeline.settings import Settings

__all__ = [
    "POLICY_CONTENT_TYPES",
    "SUPPORTED_POLICY_PLATFORMS",
    "load_publish_policy",
    "resolve_publish_plan",
    "save_publish_policy",
    "validate_publish_policy",
]

POLICY_CONTENT_TYPES = ("finance", "ai_briefing", "script_video", "grouped_anime", "ai_art")
SUPPORTED_POLICY_PLATFORMS = ("douyin", "kuaishou", "bilibili")


def resolve_publish_plan(
    content_type: str,
    task: TaskInput,
    *,
    settings: Settings,
) -> dict[str, Any]:
    """Return the effective publish decision for a task: {"requested": bool, "targets": [...]}."""
    entry = load_publish_policy(settings).get(content_type) or {}
    requested = task.publish if task.publish is not None else bool(entry.get("enabled"))
    targets = list(task.publish_targets or entry.get("targets") or [])
    return {"requested": bool(requested), "targets": targets}


def load_publish_policy(settings: Settings) -> dict[str, dict[str, Any]]:
    """Load packaged YAML defaults merged with the saved operator override."""
    policy = _normalize_policy(_load_yaml_defaults(settings.publish_defaults_file))
    override_path = _override_path(settings)
    if override_path.is_file():
        try:
            override = json.loads(override_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            override = {}
        for content_type, entry in _normalize_policy(override).items():
            policy.setdefault(content_type, {}).update(entry)
    return policy


def save_publish_policy(settings: Settings, payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Validate and persist an operator override; returns the effective policy."""
    normalized = _normalize_policy(payload)
    unknown = sorted(set(normalized) - set(POLICY_CONTENT_TYPES))
    if unknown:
        raise ConfigError(f"publish policy does not support content types: {', '.join(unknown)}")
    serializable = {
        content_type: {
            "enabled": entry["enabled"],
            "targets": [target.model_dump(mode="json") for target in entry["targets"]],
        }
        for content_type, entry in normalized.items()
    }
    path = _override_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(serializable, ensure_ascii=False, indent=2), encoding="utf-8")
    return load_publish_policy(settings)


def validate_publish_policy(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Public validator used by the API layer."""
    return _normalize_policy(payload)


def _override_path(settings: Settings) -> Path:
    return settings.data_dir / "publish_policy.json"


def _load_yaml_defaults(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8") if path.is_file() else _packaged_defaults_text(path)
        data = yaml.safe_load(text) or {}
    except OSError as exc:
        raise ConfigError(f"cannot read publish defaults {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid publish defaults {path}: {exc}") from exc
    return data if isinstance(data, dict) else {}


def _packaged_defaults_text(path: Path) -> str:
    if path != Settings.model_fields["publish_defaults_file"].default:
        return ""
    try:
        return resources.files("content_pipeline.config").joinpath("publish.defaults.yaml").read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError):
        return ""


def _normalize_policy(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if not isinstance(payload, dict):
        raise ConfigError("publish policy must be a mapping of content type to settings")
    normalized: dict[str, dict[str, Any]] = {}
    for content_type, raw_entry in payload.items():
        if not isinstance(raw_entry, dict):
            raise ConfigError(f"publish policy entry for {content_type} must be a mapping")
        entry: dict[str, Any] = {"enabled": bool(raw_entry.get("enabled")), "targets": []}
        raw_targets = raw_entry.get("targets") or []
        if not isinstance(raw_targets, list):
            raise ConfigError(f"publish targets for {content_type} must be a list")
        seen: set[str] = set()
        for raw_target in raw_targets:
            try:
                target = PublishTarget.model_validate(raw_target)
            except ValidationError as exc:
                raise ConfigError(f"invalid publish target for {content_type}: {raw_target!r}") from exc
            if target.platform not in SUPPORTED_POLICY_PLATFORMS:
                raise ConfigError(f"publish policy does not support platform: {target.platform}")
            if target.platform in seen:
                raise ConfigError(f"publish targets for {content_type} contain duplicate platform: {target.platform}")
            seen.add(target.platform)
            entry["targets"].append(target)
        normalized[str(content_type)] = entry
    return normalized
