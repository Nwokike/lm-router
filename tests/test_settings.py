"""Settings persistence: round-trip, corruption fallback, validation."""

import json
from pathlib import Path

import pytest

from core.settings import AppSettings, ValidationError
from core.storage import settings_path


def _iso(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FLET_APP_STORAGE_DATA", str(tmp_path))


def test_roundtrip_preserves_settings(tmp_path, monkeypatch) -> None:
    _iso(tmp_path, monkeypatch)
    settings = AppSettings(theme="dark", gateway_port=9090, last_model="auto")
    settings.save()

    loaded = AppSettings.load()
    assert loaded.theme == "dark"
    assert loaded.gateway_port == 9090
    assert loaded.last_model == "auto"


def test_corrupt_settings_file_starts_fresh(tmp_path, monkeypatch) -> None:
    _iso(tmp_path, monkeypatch)
    settings_path().parent.mkdir(parents=True, exist_ok=True)
    settings_path().write_text("{not json", encoding="utf-8")

    loaded = AppSettings.load()
    assert loaded.gateway_port == AppSettings().gateway_port
    # The unreadable file is preserved, not destroyed.
    assert settings_path().with_suffix(".json.bak").exists()


def test_corrupt_scalars_degrade_to_defaults(tmp_path, monkeypatch) -> None:
    """A hand-corrupted scalar (theme: null, port: 0) must never abort boot."""
    _iso(tmp_path, monkeypatch)
    settings_path().parent.mkdir(parents=True, exist_ok=True)
    settings_path().write_text(
        json.dumps({"theme": None, "gateway_port": 0, "interstitial_every": None}),
        encoding="utf-8",
    )
    loaded = AppSettings.load()  # must not raise
    assert loaded.theme == "system"
    assert loaded.gateway_port == AppSettings().gateway_port
    from core.settings import pop_load_warnings

    assert pop_load_warnings()  # the user is told settings were reset


def test_env_cannot_override_settings(tmp_path, monkeypatch) -> None:
    _iso(tmp_path, monkeypatch)
    monkeypatch.setenv("LM_ROUTER_THEME", "light")
    AppSettings(theme="dark").save()
    assert AppSettings.load().theme == "dark"


def test_port_validation_rejects_out_of_range() -> None:
    with pytest.raises(ValidationError):
        AppSettings(gateway_port=0)
    with pytest.raises(ValidationError):
        AppSettings(gateway_port=70000)


def test_removed_keys_in_stored_files_are_ignored(tmp_path, monkeypatch) -> None:
    """Legacy files carrying kani/MCP/provider keys must load without error."""
    _iso(tmp_path, monkeypatch)
    settings_path().parent.mkdir(parents=True, exist_ok=True)
    settings_path().write_text(
        json.dumps(
            {
                "providers": [{"name": "x", "base_url": "https://x.dev/v1"}],
                "mcp_servers": [{"name": "y", "transport": "stdio", "command": "ls"}],
                "system_prompt": "be brief",
                "temperature": 0.7,
                "theme": "dark",
            },
        ),
        encoding="utf-8",
    )
    loaded = AppSettings.load()  # must not raise
    assert loaded.theme == "dark"
    assert not hasattr(loaded, "providers")
    assert not hasattr(loaded, "mcp_servers")


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


def test_temp_dir_is_the_third_storage_leg(tmp_path, monkeypatch) -> None:
    """Flet's README contract: data / cache / temp. temp is scratch only —
    FLET_APP_STORAGE_TEMP when the launcher provides it, system temp for
    bare runs (scratch must never live beside durable data)."""
    import tempfile

    from core import storage

    # Launcher-provided absolute path is used verbatim (and created).
    target = tmp_path / "flet-temp"
    monkeypatch.setenv("FLET_APP_STORAGE_TEMP", str(target))
    assert storage.temp_dir() == target
    assert target.is_dir()

    # A relative env value is ignored (same guard as base_dir).
    monkeypatch.setenv("FLET_APP_STORAGE_TEMP", "relative/path")
    got = storage.temp_dir()
    assert got.is_absolute()
    assert got == Path(tempfile.gettempdir()) / "lm_router"
    assert got.is_dir()

    # No env at all (bare run): system temp, never ~/.lm_router.
    monkeypatch.delenv("FLET_APP_STORAGE_TEMP", raising=False)
    got = storage.temp_dir()
    assert got == Path(tempfile.gettempdir()) / "lm_router"
    assert "lm_router" in str(got)
