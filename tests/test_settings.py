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


def test_base_dir_never_anchors_to_the_cwd(tmp_path, monkeypatch) -> None:
    """The audit found state scattered across five directories — the fallback
    is ~/.lm_router, and a RELATIVE env value must not re-anchor to cwd."""
    from pathlib import Path as _Path

    from core import storage

    monkeypatch.delenv("FLET_APP_STORAGE_DATA", raising=False)
    assert storage.base_dir() == _Path.home() / ".lm_router"

    monkeypatch.setenv("FLET_APP_STORAGE_DATA", "relative/path")
    assert storage.base_dir() == _Path.home() / ".lm_router", (
        "a relative env value must fall back, not follow the shell's cwd"
    )

    absolute = tmp_path / "data"
    monkeypatch.setenv("FLET_APP_STORAGE_DATA", str(absolute))
    assert storage.base_dir() == absolute


def test_live_tools_for_strips_only_its_own_server_prefix() -> None:
    """The dropdown lists ONE server's tools, short-named for the toggle API.

    Splitting on the first dot would corrupt a server whose own name
    contains one; prefix-strip is the only correct mapping, and a sibling
    server whose name merely STARTS with ours must never leak in.
    """
    from screens.settings_screen import live_tools_for

    tools = ["demo.echo", "demo.add", "demo.tools.calc", "demo2.hidden", "other.x"]
    assert live_tools_for(tools, "demo") == ["echo", "add", "tools.calc"]
    assert live_tools_for(tools, "demo2") == ["hidden"]
    assert live_tools_for(tools, "absent") == []


def test_apply_api_key_fills_authorization_header() -> None:
    """Owner ask: an MCP API key must not require hand-writing Headers JSON.

    The dedicated field fills the standard Bearer header, keeps custom
    headers, overrides a stale Authorization, and is a no-op when empty
    (not every server uses a key).
    """
    from screens.settings_screen import apply_api_key

    assert apply_api_key({}, "sk-1") == {"Authorization": "Bearer sk-1"}
    merged = apply_api_key({"X-Custom": "v"}, "sk-1")
    assert merged == {"X-Custom": "v", "Authorization": "Bearer sk-1"}
    # The dedicated field wins for Authorization (user's latest intent).
    assert apply_api_key({"Authorization": "Bearer old"}, " sk-2 ")["Authorization"] == (
        "Bearer sk-2"
    )
    # Optional: empty key never injects anything.
    assert apply_api_key({"A": "b"}, "") == {"A": "b"}
