"""Engine service lifecycle: live fetch, cache, start, adopt, health, stop.

There is no bundled engine any more — the gateway is always fetched live from
router.kiri.ng and cached to user storage, so these tests drive a fixture
engine (tests/fixture_engine.py) through the same contract.
"""

import socket
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from core import storage
from core.settings import AppSettings
from core.state import state
from services import engine as engine_mod
from services.engine import EngineService

FIXTURE_ENGINE = Path(__file__).resolve().parent / "fixture_engine.py"
FIXTURE_BYTES = FIXTURE_ENGINE.read_bytes()


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _reset_state(port: int) -> None:
    state.gateway_running = False
    state.gateway_port = port
    state.gateway_version = ""
    state.gateway_uptime = 0
    state.gateway_base_url = f"http://127.0.0.1:{port}/v1"
    state.gateway_source = ""


def test_no_bundled_engine_is_shipped() -> None:
    """The gateway is never vendored: upstream changes constantly."""
    assert not hasattr(engine_mod, "BUNDLED_ENGINE")
    assert not (FIXTURE_ENGINE.parent.parent / "src" / "assets" / "engine").exists()


def test_raises_clear_error_when_offline_with_no_cache(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FLET_APP_STORAGE_DATA", str(tmp_path))
    monkeypatch.setattr(
        engine_mod,
        "_download",
        lambda url: (_ for _ in ()).throw(OSError("offline")),
    )
    svc = EngineService(AppSettings())
    with pytest.raises(engine_mod.EngineUnavailable) as excinfo:
        svc.load()
    assert "Connect to the internet" in str(excinfo.value)


def test_fetched_engine_when_download_valid(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FLET_APP_STORAGE_DATA", str(tmp_path))
    monkeypatch.setattr(engine_mod, "_download", lambda url: FIXTURE_BYTES)
    svc = EngineService(AppSettings())
    label = svc.load()
    assert label.startswith("fetched")
    assert storage.engine_cache_path().exists()
    # The engine is a regenerable artefact: it belongs in the cache,
    # not in the backed-up data directory.
    assert storage.engine_cache_path() != storage.settings_path().parent


def test_start_health_adopt_stop(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FLET_APP_STORAGE_DATA", str(tmp_path))
    monkeypatch.setattr(engine_mod, "_download", lambda url: FIXTURE_BYTES)
    monkeypatch.setattr(EngineService, "_warm_discover", lambda self: None)

    port = _free_port()
    _reset_state(port)
    settings = AppSettings(gateway_port=port)
    svc = EngineService(settings)
    status, bound = svc.start()
    assert status == "started"
    assert bound == port
    assert state.gateway_running is True
    assert state.gateway_base_url == f"http://127.0.0.1:{port}/v1"
    assert state.gateway_source.startswith("fetched")

    # health endpoint answers with our adapter identity
    import json

    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as resp:
        health = json.loads(resp.read())
    assert health["adapter"] == "kiri-router"
    assert health["version"]

    # a second service adopts the same port instead of fighting for it
    svc2 = EngineService(AppSettings(gateway_port=port))
    status2, port2 = svc2.start()
    assert status2 == "adopted"
    assert port2 == port

    # stopping the adopter must not kill the original gateway
    svc2.stop()
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as resp:
        assert resp.status == 200

    # stopping the owner frees the port
    svc.stop()
    assert state.gateway_running is False
    refused = False
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)
    except urllib.error.URLError, ConnectionError, OSError:
        refused = True
    assert refused


def test_validate_engine_pep440_versions() -> None:
    # Valid PEP 440 versions get normalized
    valid_script = b'VERSION = "1.0.0"\nif __name__ == "__main__":\n    pass'
    assert engine_mod._validate(valid_script) == "1.0.0"

    valid_prerelease = b'VERSION = "1.2.0rc1"\nif __name__ == "__main__":\n    pass'
    assert engine_mod._validate(valid_prerelease) == "1.2.0rc1"

    import pytest

    # Missing main guard / missing token: structural failures still reject
    with pytest.raises(ValueError, match="missing main guard"):
        engine_mod._validate(b'VERSION = "1.0.0"')
    with pytest.raises(ValueError, match="missing VERSION token"):
        engine_mod._validate(b"print('no version')\nif __name__ == \"__main__\":\n    pass")

    # Non-PEP440 version: ACCEPTED raw — a live router.kiri.ng update must
    # never be rejected over version formatting (it would pin us to the
    # stale bundled copy). Strict validation lives in scripts/fetch_engine.py.
    weird_script = b'VERSION = "build-99x"\nif __name__ == "__main__":\n    pass'
    assert engine_mod._validate(weird_script) == "build-99x"


def test_cached_engine_when_offline(monkeypatch, tmp_path) -> None:
    """Download fails -> fall back to the last-known-good runtime cache."""
    monkeypatch.setenv("FLET_APP_STORAGE_DATA", str(tmp_path))
    monkeypatch.setattr(
        engine_mod,
        "_download",
        lambda url: (_ for _ in ()).throw(OSError("offline")),
    )
    # Seed a previous successful fetch
    storage.engine_cache_path().parent.mkdir(parents=True, exist_ok=True)
    storage.engine_cache_path().write_bytes(FIXTURE_BYTES)

    svc = EngineService(AppSettings())
    label = svc.load()
    assert label.startswith("cached")
    assert svc._mod is not None
    assert callable(svc._mod.acquire_server)


def test_gateway_lan_url_and_account_limits(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FLET_APP_STORAGE_DATA", str(tmp_path))
    monkeypatch.setattr(engine_mod, "_download", lambda url: FIXTURE_BYTES)
    monkeypatch.setattr(EngineService, "_warm_discover", lambda self: None)

    port = _free_port()
    _reset_state(port)
    settings = AppSettings(gateway_port=port)
    svc = EngineService(settings)
    try:
        svc.start()
        assert state.gateway_lan_url.endswith(f":{port}/v1")
        assert state.gateway_lan_ip != ""

        # Mock listed_models to avoid slow network discovery during test
        if svc._mod is not None:
            fake_models = [
                {"id": "test-model", "endpoint_type": "chat", "status": "active", "latency_ms": 15},
            ]
            monkeypatch.setattr(svc._mod, "listed_models", lambda **kw: fake_models)

        limits = svc._fetch_account_limits(port)
        assert isinstance(limits, dict)
        assert limits.get("zero_auth_supported") is True
    finally:
        svc.stop()


def test_download_sends_a_user_agent(monkeypatch) -> None:
    """router.kiri.ng answers 403 to urllib's default UA ("Python-urllib/3.x").

    Without this header the app could never fetch its own gateway — every
    start failed with HTTP 403 and the user was left with no router at all.
    """
    seen: dict = {}

    class _Resp:
        def read(self):
            return b'VERSION = "1.0.0"\nif __name__ == "__main__":\n    pass\n'

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(request, timeout=None):
        seen["ua"] = (getattr(request, "headers", {}) or {}).get("User-agent")
        seen["url"] = getattr(request, "full_url", None)
        return _Resp()

    monkeypatch.setattr(engine_mod.urllib.request, "urlopen", fake_urlopen)
    data = engine_mod._download("https://router.kiri.ng/run.py")

    assert b"VERSION" in data
    assert seen["url"] == "https://router.kiri.ng/run.py"
    assert seen["ua"], "no User-Agent header was sent"
    assert "Python-urllib" not in seen["ua"]
    assert "LM-Router" in seen["ua"]
