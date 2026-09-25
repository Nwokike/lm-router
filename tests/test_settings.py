"""Settings persistence: round-trip, corruption fallback, provider keys.

Provider API keys are stored as-is in the local settings file. They used to be
Fernet-encrypted with a `master.key` kept in the SAME directory, so anyone who
could read the settings could read the key too — it protected nothing.
`SecretStr` still keeps keys out of logs, reprs and crash dumps, which is the
part that matters for a local-only app.
"""

from pathlib import Path

from core.settings import AppSettings, MCPServerConfig, ProviderConfig
from core.storage import settings_path


def _iso(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FLET_APP_STORAGE_DATA", str(tmp_path))


def test_roundtrip_preserves_settings_and_key(tmp_path, monkeypatch) -> None:
    _iso(tmp_path, monkeypatch)
    settings = AppSettings(
        theme="dark",
        gateway_port=9090,
        system_prompt="be brief",
        providers=[
            ProviderConfig(
                name="OpenRouter", base_url="https://openrouter.ai/api/v1", api_key="sk-test"
            ),
        ],
        mcp_servers=[MCPServerConfig(name="local", transport="stdio", command="python", args=[])],
    )
    settings.save()

    loaded = AppSettings.load()
    assert loaded.theme == "dark"
    assert loaded.gateway_port == 9090
    assert loaded.system_prompt == "be brief"
    assert loaded.providers[0].name == "OpenRouter"
    assert loaded.providers[0].api_key is not None
    assert loaded.providers[0].api_key.get_secret_value() == "sk-test"
    assert loaded.mcp_servers[0].command == "python"


def test_key_is_masked_in_repr(tmp_path, monkeypatch) -> None:
    """The real reason SecretStr is kept: keys must not leak into logs."""
    _iso(tmp_path, monkeypatch)
    provider = ProviderConfig(name="p", base_url="https://x.dev/v1", api_key="sk-super-secret")
    assert "sk-super-secret" not in repr(provider)
    assert "**********" in repr(provider.api_key)


def test_corrupt_settings_file_starts_fresh(tmp_path, monkeypatch) -> None:
    _iso(tmp_path, monkeypatch)
    settings_path().parent.mkdir(parents=True, exist_ok=True)
    settings_path().write_text("{not json", encoding="utf-8")

    loaded = AppSettings.load()
    assert loaded.gateway_port == AppSettings().gateway_port
    # The unreadable file is preserved, not destroyed.
    assert settings_path().with_suffix(".json.bak").exists()


def test_env_cannot_override_settings(tmp_path, monkeypatch) -> None:
    _iso(tmp_path, monkeypatch)
    monkeypatch.setenv("LM_ROUTER_THEME", "light")
    AppSettings(theme="dark").save()
    assert AppSettings.load().theme == "dark"


def test_mcp_server_validation() -> None:
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        MCPServerConfig(name="bad", transport="stdio")  # stdio needs a command
    with pytest.raises(pydantic.ValidationError):
        MCPServerConfig(name="bad", transport="streamable_http")  # remote needs a url
    ok = MCPServerConfig(name="ok", transport="streamable_http", url="https://x.dev/mcp")
    assert ok.url is not None


def test_resilient_loading_skips_malformed_provider(tmp_path, monkeypatch) -> None:
    """One bad provider must never cost the user the good ones."""
    _iso(tmp_path, monkeypatch)
    import json

    settings_path().parent.mkdir(parents=True, exist_ok=True)
    settings_path().write_text(
        json.dumps(
            {
                "providers": [
                    {"name": "good", "base_url": "https://good.dev/v1"},
                    {"name": "", "base_url": "not-a-url"},
                ],
            },
        ),
        encoding="utf-8",
    )

    from core.settings import pop_load_warnings

    loaded = AppSettings.load()
    assert [p.name for p in loaded.providers] == ["good"]
    assert pop_load_warnings()  # the user is told something was dropped


def test_non_string_key_is_dropped_not_fatal(tmp_path, monkeypatch) -> None:
    _iso(tmp_path, monkeypatch)
    import json

    settings_path().parent.mkdir(parents=True, exist_ok=True)
    settings_path().write_text(
        json.dumps(
            {"providers": [{"name": "p", "base_url": "https://x.dev/v1", "api_key": {"a": 1}}]},
        ),
        encoding="utf-8",
    )

    loaded = AppSettings.load()  # must not raise
    assert loaded.providers[0].api_key is None


def test_null_provider_list_is_survivable(tmp_path, monkeypatch) -> None:
    _iso(tmp_path, monkeypatch)
    import json

    settings_path().parent.mkdir(parents=True, exist_ok=True)
    settings_path().write_text(
        json.dumps({"providers": None, "mcp_servers": None}),
        encoding="utf-8",
    )
    assert AppSettings.load().providers == []


import pytest  # noqa: E402
