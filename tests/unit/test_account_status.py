import json

from content_pipeline.account_status import fetch_account_identities
from content_pipeline.settings import Settings


class _FakeResponse:
    """Minimal stand-in for the SAU bridge's ``/api/accounts`` HTTP response."""

    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def _fake_bridge(payload: dict):
    return lambda request, timeout=0: _FakeResponse(payload)


def _reset_account_cache(monkeypatch) -> None:
    import content_pipeline.account_status as account_status_module

    monkeypatch.setattr(account_status_module, "_account_identities_cache", None)


def test_account_identities_are_grouped_by_platform(monkeypatch) -> None:
    _reset_account_cache(monkeypatch)
    monkeypatch.setattr(
        "urllib.request.urlopen",
        _fake_bridge(
            {
                "accounts": [
                    {"platform": "douyin", "account": "金融破壁人"},
                    {"platform": "douyin", "account": "硅基思维TARS"},
                    {"platform": "kuaishou", "account": " 搞AI的罗辑同学 "},
                    {"platform": "kuaishou", "account": "   "},
                    {"platform": "", "account": "缺少平台"},
                    {"platform": "bilibili", "account": "硅基思维TARS"},
                ]
            }
        ),
    )

    identities = fetch_account_identities(_settings(sau_bridge_url="http://bridge.test:5800"))

    assert identities == {
        "douyin": ["金融破壁人", "硅基思维TARS"],
        "kuaishou": ["搞AI的罗辑同学"],
        "bilibili": ["硅基思维TARS"],
    }


def test_account_identities_request_uses_the_configured_bridge_and_token(monkeypatch) -> None:
    _reset_account_cache(monkeypatch)
    seen = []

    def recording_bridge(request, timeout=0):
        seen.append({"url": request.full_url, "auth": request.headers.get("Authorization")})
        return _FakeResponse({"accounts": []})

    monkeypatch.setattr("urllib.request.urlopen", recording_bridge)

    fetch_account_identities(_settings(sau_bridge_url="http://bridge.test:5800/", sau_bridge_token="secret-token"))

    assert seen == [{"url": "http://bridge.test:5800/api/accounts", "auth": "Bearer secret-token"}]


def test_account_identities_are_unknown_when_the_bridge_fails(monkeypatch) -> None:
    _reset_account_cache(monkeypatch)

    def failing_bridge(request, timeout=0):
        raise OSError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", failing_bridge)

    assert fetch_account_identities(_settings(sau_bridge_url="http://bridge.test:5800")) is None


def test_account_identities_are_unknown_without_a_configured_bridge(monkeypatch) -> None:
    _reset_account_cache(monkeypatch)

    def unexpected_bridge(request, timeout=0):
        raise AssertionError("no bridge request without a configured bridge URL")

    monkeypatch.setattr("urllib.request.urlopen", unexpected_bridge)

    assert fetch_account_identities(_settings(sau_bridge_url="")) is None


def test_account_identities_are_unknown_when_the_payload_is_malformed(monkeypatch) -> None:
    _reset_account_cache(monkeypatch)
    monkeypatch.setattr("urllib.request.urlopen", _fake_bridge({"accounts": "not-a-list"}))

    assert fetch_account_identities(_settings(sau_bridge_url="http://bridge.test:5800")) is None


def test_account_identities_are_cached_between_calls(monkeypatch) -> None:
    _reset_account_cache(monkeypatch)
    calls = []

    def counting_bridge(request, timeout=0):
        calls.append(request.full_url)
        return _FakeResponse({"accounts": [{"platform": "kuaishou", "account": "搞AI的罗辑同学"}]})

    monkeypatch.setattr("urllib.request.urlopen", counting_bridge)
    settings = _settings(sau_bridge_url="http://bridge.test:5800")

    first = fetch_account_identities(settings)
    second = fetch_account_identities(settings)

    assert first == second == {"kuaishou": ["搞AI的罗辑同学"]}
    assert calls == ["http://bridge.test:5800/api/accounts"]


def test_account_identities_cache_does_not_leak_between_bridges(monkeypatch) -> None:
    _reset_account_cache(monkeypatch)
    calls = []

    def counting_bridge(request, timeout=0):
        calls.append(request.full_url)
        host = "first" if "first" in request.full_url else "second"
        return _FakeResponse({"accounts": [{"platform": "kuaishou", "account": f"{host}-account"}]})

    monkeypatch.setattr("urllib.request.urlopen", counting_bridge)

    first = fetch_account_identities(_settings(sau_bridge_url="http://first.test:5800"))
    second = fetch_account_identities(_settings(sau_bridge_url="http://second.test:5800"))

    assert first == {"kuaishou": ["first-account"]}
    assert second == {"kuaishou": ["second-account"]}
    assert calls == ["http://first.test:5800/api/accounts", "http://second.test:5800/api/accounts"]


def test_collect_account_status_still_reads_live_accounts(monkeypatch, tmp_path) -> None:
    from content_pipeline.account_status import collect_account_status

    monkeypatch.setattr(
        "urllib.request.urlopen",
        _fake_bridge({"accounts": [{"platform": "kuaishou", "account": "搞AI的罗辑同学", "status": "valid"}]}),
    )

    status = collect_account_status(_settings(sau_bridge_url="http://bridge.test:5800", sau_dir=tmp_path / "sau"))

    assert status["source"] == "bridge"
    assert [item["account"] for item in status["accounts"]] == ["搞AI的罗辑同学"]
