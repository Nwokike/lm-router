"""Engine service lifecycle: bundled fallback, start, adopt, health, stop."""

import socket
import urllib.error
import urllib.request

from core.settings import AppSettings
from core.state import state
from services import engine as engine_mod
from services.engine import EngineService


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


def test_bundled_fallback_when_download_fails(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FLET_APP_STORAGE_DATA", str(tmp_path))
    monkeypatch.setattr(
        engine_mod, "_download", lambda url: (_ for _ in ()).throw(OSError("offline"))
    )
    svc = EngineService(AppSettings())
    label = svc.load()
    assert label.startswith("bundled")
    assert svc._mod is not None
    assert callable(getattr(svc._mod, "acquire_server"))


def test_fetched_engine_when_download_valid(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FLET_APP_STORAGE_DATA", str(tmp_path))
    data = engine_mod.BUNDLED_ENGINE.read_bytes()
    monkeypatch.setattr(engine_mod, "_download", lambda url: data)
    svc = EngineService(AppSettings())
    label = svc.load()
    assert label.startswith("fetched")
    assert (tmp_path / "engine_local.py").exists()


def test_start_health_adopt_stop(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FLET_APP_STORAGE_DATA", str(tmp_path))
    monkeypatch.setattr(
        engine_mod, "_download", lambda url: (_ for _ in ()).throw(OSError("offline"))
    )
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
    assert state.gateway_source.startswith("bundled")

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
