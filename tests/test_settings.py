"""Settings persistence: round-trip, encryption at rest, corruption fallback."""

from pathlib import Path

from pydantic import SecretStr

from core.settings import AppSettings, MCPServerConfig, ProviderConfig


def _iso(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FLET_APP_STORAGE_DATA", str(tmp_path))


def test_roundtrip_with_encrypted_key(tmp_path, monkeypatch) -> None:
    _iso(tmp_path, monkeypatch)
    settings = AppSettings(
        theme="dark",
        gateway_port=9090,
        providers=[
            ProviderConfig(
                name="Localbox",
                base_url="http://127.0.0.1:11434/v1",
                api_key=SecretStr("super-secret-key"),
                models=["llama3"],
            )
        ],
        active_provider_id="abc",
    )
    settings.save()

    raw = (tmp_path / "app_settings.json").read_text(encoding="utf-8")
    assert "super-secret-key" not in raw
    assert "gAAAA" in raw  # fernet ciphertext marker
    assert (tmp_path / "master.key").exists()

    loaded = AppSettings.load()
    assert loaded.theme == "dark"
    assert loaded.gateway_port == 9090
    assert loaded.active_provider_id == "abc"
    assert loaded.providers[0].name == "Localbox"
    assert str(loaded.providers[0].base_url).rstrip("/") == "http://127.0.0.1:11434/v1"
    assert loaded.providers[0].api_key is not None
    assert loaded.providers[0].api_key.get_secret_value() == "super-secret-key"


def test_corrupt_settings_file_starts_fresh(tmp_path, monkeypatch) -> None:
    _iso(tmp_path, monkeypatch)
    (tmp_path / "app_settings.json").write_text("{ not json", encoding="utf-8")
    settings = AppSettings.load()
    assert settings.theme == "system"
    assert (tmp_path / "app_settings.json.bak").exists()


def test_env_cannot_override_settings(tmp_path, monkeypatch) -> None:
    _iso(tmp_path, monkeypatch)
    monkeypatch.setenv("GATEWAY_PORT", "1")
    monkeypatch.setenv("APP_GATEWAY_PORT", "1")
    settings = AppSettings()
    assert settings.gateway_port == 8082


def test_mcp_server_validation() -> None:
    good = MCPServerConfig(name="files", transport="stdio", command="npx", args=["-y", "pkg"])
    assert good.transport == "stdio"
    try:
        MCPServerConfig(name="bad", transport="stdio")
        raise AssertionError("stdio without command must fail")
    except ValueError:
        pass
    try:
        MCPServerConfig(name="bad", transport="streamable_http")
        raise AssertionError("remote without url must fail")
    except ValueError:
        pass
