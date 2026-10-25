"""Read-only platform account login status for the settings center.

Live status comes from the SAU account-center bridge when it is reachable;
cookie files under ``<sau_dir>/cookies`` are always scanned so missing or
stale login state is visible even when the bridge is offline.
"""

from __future__ import annotations

import json
import time
import urllib.request
from datetime import datetime
from typing import Any

from content_pipeline.settings import Settings

__all__ = ["collect_account_status", "fetch_account_identities"]

_COOKIE_PLATFORMS = ("douyin", "kuaishou", "bilibili", "xiaohongshu")

ACCOUNT_IDENTITIES_CACHE_SECONDS = 60.0
_account_identities_cache: tuple[str, float, dict[str, list[str]]] | None = None


def collect_account_status(settings: Settings) -> dict[str, Any]:
    bridge_accounts = _fetch_bridge_accounts(settings)
    cookies = _scan_cookie_files(settings)
    accounts: dict[tuple[str, str], dict[str, Any]] = {}

    for item in bridge_accounts or []:
        platform = str(item.get("platform") or "")
        account = str(item.get("account") or "").strip()
        if not platform or not account:
            continue
        accounts[(platform, account)] = {
            "platform": platform,
            "account": account,
            "status": str(item.get("status") or ""),
            "status_label": str(item.get("status_label") or ""),
            "has_cookie": False,
            "cookie_modified": None,
        }
    for (platform, account), cookie in cookies.items():
        entry = accounts.setdefault(
            (platform, account),
            {
                "platform": platform,
                "account": account,
                "status": "",
                "status_label": "",
                "has_cookie": False,
                "cookie_modified": None,
            },
        )
        entry["has_cookie"] = True
        entry["cookie_modified"] = cookie

    return {
        "source": "bridge" if bridge_accounts is not None else "cookies",
        "accounts": sorted(accounts.values(), key=lambda item: (item["platform"], item["account"])),
    }


def fetch_account_identities(settings: Settings) -> dict[str, list[str]] | None:
    """SAU account identities per platform, or ``None`` when they are unknown.

    SAU identifies an account by its cookie-file name, so publishing must use
    exactly the identity the account center reports. ``None`` means the bridge
    is unconfigured or unreachable and the answer is unknown, which callers
    must not turn into a failure; a returned mapping (possibly empty) is
    authoritative. Results are cached briefly so one publish does not query the
    bridge once per target.
    """
    global _account_identities_cache
    base_url = settings.sau_bridge_url.strip().rstrip("/")
    if not base_url:
        return None
    now = time.monotonic()
    if (
        _account_identities_cache is not None
        and _account_identities_cache[0] == base_url
        and now - _account_identities_cache[1] < ACCOUNT_IDENTITIES_CACHE_SECONDS
    ):
        return _copy_identities(_account_identities_cache[2])
    bridge_accounts = _fetch_bridge_accounts(settings)
    if bridge_accounts is None:
        return None
    identities: dict[str, list[str]] = {}
    for item in bridge_accounts:
        platform = str(item.get("platform") or "")
        account = str(item.get("account") or "").strip()
        if not platform or not account:
            continue
        names = identities.setdefault(platform, [])
        if account not in names:
            names.append(account)
    _account_identities_cache = (base_url, now, identities)
    return _copy_identities(identities)


def _copy_identities(identities: dict[str, list[str]]) -> dict[str, list[str]]:
    return {platform: list(names) for platform, names in identities.items()}


def _fetch_bridge_accounts(settings: Settings) -> list[dict[str, Any]] | None:
    base_url = settings.sau_bridge_url.strip().rstrip("/")
    if not base_url:
        return None
    headers = {"Accept": "application/json"}
    token = settings.sau_bridge_token.get_secret_value()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(f"{base_url}/api/accounts", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None
    accounts = payload.get("accounts") if isinstance(payload, dict) else None
    return accounts if isinstance(accounts, list) else None


def _scan_cookie_files(settings: Settings) -> dict[tuple[str, str], str]:
    cookies_dir = settings.sau_dir / "cookies"
    found: dict[tuple[str, str], str] = {}
    if not cookies_dir.is_dir():
        return found
    for path in sorted(cookies_dir.glob("*_*.json")):
        platform, sep, account = path.stem.partition("_")
        if not sep or platform not in _COOKIE_PLATFORMS or not account:
            continue
        modified = datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds")
        key = (platform, account)
        if key not in found or modified > found[key]:
            found[key] = modified
    return found
