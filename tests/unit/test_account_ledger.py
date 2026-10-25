import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from content_pipeline.account_ledger import (
    ensure_account_publishable,
    load_accounts,
    resolve_publish_account,
)
from content_pipeline.core.errors import ConfigError
from content_pipeline.settings import Settings

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
def settings(tmp_path, monkeypatch):
    sau_dir = tmp_path / "sau"
    (sau_dir / "db").mkdir(parents=True)
    conn = sqlite3.connect(sau_dir / "db" / "accounts.db")
    conn.executescript(SCHEMA)
    conn.executemany(
        "INSERT INTO accounts (platform, platform_uid, identity, nickname, status,"
        " checked_at, uid_source, first_seen_at) VALUES (?,?,?,?,?,?,?,?)",
        [
            (
                "douyin",
                "7640519636165559355",
                "金融破壁人",
                "金融破壁人",
                "valid",
                "2026-09-22T20:30:46+08:00",
                "cookie",
                "2026-09-22T00:00:00+08:00",
            ),
            (
                "douyin",
                "unresolved:硅基思维TARS",
                "硅基思维TARS",
                "山下富士",
                "valid",
                "2026-09-22T22:02:00+08:00",
                "synthetic",
                "2026-09-22T00:00:00+08:00",
            ),
            (
                "kuaishou",
                "5362435333",
                "搞AI的罗辑同学",
                "金融破壁人",
                "valid",
                "2026-09-22T22:03:07+08:00",
                "cookie",
                "2026-09-22T00:00:00+08:00",
            ),
        ],
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("SAU_DIR", str(sau_dir))
    return Settings()


def _write_row(sau_dir: Path, **overrides) -> None:
    """按本文件的库结构写一行，用于构造 expired 等场景。"""
    row = {
        "platform": "kuaishou",
        "platform_uid": "5362435333",
        "identity": "搞AI的罗辑同学",
        "nickname": "金融破壁人",
        "status": "valid",
        "checked_at": "2026-09-22T22:03:07+08:00",
        "uid_source": "cookie",
        "first_seen_at": "2026-09-22T00:00:00+08:00",
    }
    row.update(overrides)
    connection = sqlite3.connect(sau_dir / "db" / "accounts.db")
    connection.execute(
        "INSERT OR REPLACE INTO accounts (platform, platform_uid, identity, nickname,"
        " status, checked_at, uid_source, first_seen_at) VALUES (?,?,?,?,?,?,?,?)",
        (
            row["platform"],
            row["platform_uid"],
            row["identity"],
            row["nickname"],
            row["status"],
            row["checked_at"],
            row["uid_source"],
            row["first_seen_at"],
        ),
    )
    connection.commit()
    connection.close()


def _settings_with_status(tmp_path, monkeypatch, **overrides) -> Settings:
    sau_dir = tmp_path / "sau_status"
    (sau_dir / "db").mkdir(parents=True)
    connection = sqlite3.connect(sau_dir / "db" / "accounts.db")
    connection.executescript(SCHEMA)
    connection.commit()
    connection.close()
    _write_row(sau_dir, **overrides)
    monkeypatch.setenv("SAU_DIR", str(sau_dir))
    return Settings()


def test_load_accounts_groups_by_platform(settings):
    accounts = load_accounts(settings)
    assert accounts is not None
    assert [row.identity for row in accounts["kuaishou"]] == ["搞AI的罗辑同学"]


def test_load_accounts_returns_none_when_ledger_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("SAU_DIR", str(tmp_path / "nope"))
    assert load_accounts(Settings()) is None


def test_resolve_by_uid_identity_and_nickname(settings):
    by_uid = resolve_publish_account(settings, "douyin", "7640519636165559355")
    assert by_uid.identity == "金融破壁人"
    by_identity = resolve_publish_account(settings, "douyin", "金融破壁人")
    assert by_identity.platform_uid == "7640519636165559355"
    by_nickname = resolve_publish_account(settings, "douyin", "山下富士")
    assert by_nickname.identity == "硅基思维TARS"


def test_resolve_empty_ref_uses_the_only_account(settings):
    assert resolve_publish_account(settings, "kuaishou", "").identity == "搞AI的罗辑同学"


def test_resolve_empty_ref_is_ambiguous_with_two_accounts(settings):
    with pytest.raises(ConfigError) as excinfo:
        resolve_publish_account(settings, "douyin", "")
    assert "硅基思维TARS" in str(excinfo.value)


def test_resolve_unknown_ref_lists_available_accounts(settings):
    with pytest.raises(ConfigError) as excinfo:
        resolve_publish_account(settings, "kuaishou", "破壁人")
    message = str(excinfo.value)
    assert "搞AI的罗辑同学" in message and "sau accounts sync" in message


def test_resolve_returns_none_when_ledger_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv("SAU_DIR", str(tmp_path / "nope"))
    assert resolve_publish_account(Settings(), "douyin", "金融破壁人") is None


def test_identity_nickname_drift_warns_but_does_not_block(settings):
    account = resolve_publish_account(settings, "kuaishou", "5362435333")
    # 2026-09-22T22:03:07+08:00 == 14:03 UTC，取当天晚些时候，避开"过期"告警
    warnings = ensure_account_publishable(account, now=datetime(2026, 9, 22, 15, 0, tzinfo=UTC))
    assert any("昵称是「金融破壁人」" in warning for warning in warnings)


def test_expired_account_fails_early(tmp_path, monkeypatch):
    settings = _settings_with_status(tmp_path, monkeypatch, status="expired")
    account = resolve_publish_account(settings, "kuaishou", "5362435333")
    with pytest.raises(ConfigError) as excinfo:
        ensure_account_publishable(account)
    assert "sau kuaishou login" in str(excinfo.value)


def test_stale_check_warns(settings):
    account = resolve_publish_account(settings, "douyin", "金融破壁人")
    far_future = datetime(2027, 1, 1, tzinfo=UTC)
    warnings = ensure_account_publishable(account, now=far_future)
    assert any("状态可能过期" in warning for warning in warnings)


def test_resolve_empty_ref_with_zero_accounts_raises_clear_error(settings):
    with pytest.raises(ConfigError) as excinfo:
        resolve_publish_account(settings, "bilibili", "")
    message = str(excinfo.value)
    assert "sau accounts sync" in message
    assert "没有任何账号" in message
    assert "多个可选账号" not in message
