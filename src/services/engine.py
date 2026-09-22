"""Gateway engine service: download, import, run, watch, stop.

The engine is kiri-router's run.py (stdlib-only, main-guarded). It is fetched
from router.kiri.ng at startup and falls back to the bundled, sha-pinned copy
at src/assets/engine/run.py. Everything here is sync urllib + threads on
purpose: it must never share an event loop with Flet or kani (anyio study).
"""

import importlib.util
import re
import threading
import urllib.request
from pathlib import Path

from core import constants
from core.logging import LOG
from core.settings import AppSettings
from core.state import state
from core.storage import base_dir

MODULE_NAME = "lm_router_engine"
BUNDLED_ENGINE = Path(__file__).resolve().parent.parent / "assets" / "engine" / "run.py"
VERSION_RE = re.compile(rb'VERSION = "([^"]+)"')


def _download(url: str) -> bytes:
    # URL is the app-controlled https constant from constants.ENGINE_URL.
    with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310
        return resp.read()


def _validate(data: bytes) -> str:
    if b'if __name__ == "__main__":' not in data:
        raise ValueError("missing main guard")
    match = VERSION_RE.search(data)
    if not match:
        raise ValueError("missing VERSION token")
    return match.group(1).decode("ascii")


def _load_module(path: Path) -> object:
    spec = importlib.util.spec_from_file_location(MODULE_NAME, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load engine from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_engine() -> tuple[object, str]:
    """Return (module, source label). Live download first, bundled fallback."""
    try:
        data = _download(constants.ENGINE_URL)
        version = _validate(data)
        cache = base_dir() / "engine_local.py"
        cache.write_bytes(data)
        return _load_module(cache), f"fetched {version}"
    except Exception as exc:
        LOG.warning("engine download unusable (%s), using bundled copy", exc)
    data = BUNDLED_ENGINE.read_bytes()
    version = _validate(data)
    return _load_module(BUNDLED_ENGINE), f"bundled {version}"


class EngineService:
    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self._mod: object | None = None
        self._server: object | None = None
        self._thread: threading.Thread | None = None
        self._watch_stop = threading.Event()
        self._watch_thread: threading.Thread | None = None
        self._healthy = False
        self.source = ""
        self.port = 0

    def load(self) -> str:
        if self._mod is None:
            self._mod, self.source = load_engine()
            state.gateway_source = self.source
            LOG.info("engine %s", self.source)
        return self.source

    @staticmethod
    def _health(port: int) -> dict | None:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as resp:
                data = json_loads(resp.read())
        except Exception:
            return None
        if data.get("adapter") != "kiri-router":
            return None
        return data

    def start(self) -> tuple[str, int]:
        """Start or adopt the gateway. Returns (status, port)."""
        if self._server is not None:
            return "started", self.port
        self.load()
        if self._mod is None:
            raise RuntimeError("engine not loaded")
        server, port = self._mod.acquire_server(self.settings.gateway_port)

        if server is None:
            # Another current-gen gateway already serves this port.
            self.port = port
            health = self._health(port)
            self._apply_health(health or {})
            state.gateway_running = health is not None
            state.gateway_port = port
            state.gateway_base_url = f"http://127.0.0.1:{port}/v1"
            self._start_watch(port)
            LOG.info("adopted existing gateway on port %d", port)
            return "adopted", port

        server.access_log = False  # our ring is the UI log
        self._server = server
        self.port = int(server.server_address[1])
        self._thread = threading.Thread(target=server.serve_forever, name="gateway", daemon=True)
        self._thread.start()

        state.gateway_port = self.port
        state.gateway_base_url = f"http://127.0.0.1:{self.port}/v1"
        health = self._health(self.port)
        self._apply_health(health or {})
        state.gateway_running = True
        self._healthy = True
        self._start_watch(self.port)
        threading.Thread(target=self._warm_discover, daemon=True).start()
        LOG.info("gateway started on port %d (%s)", self.port, self.source)
        return "started", self.port

    def stop(self) -> None:
        self._watch_stop.set()
        if self._watch_thread is not None:
            self._watch_thread.join(timeout=2)
            self._watch_thread = None
        server, thread = self._server, self._thread
        self._server = self._thread = None
        if server is not None:
            shutdowner = threading.Thread(target=server.shutdown, daemon=True)
            shutdowner.start()
            shutdowner.join(timeout=4)
            server.server_close()
        state.gateway_running = False
        self._healthy = False
        if thread is not None:
            thread.join(timeout=1)
        LOG.info("gateway stopped")

    def refresh_models(self) -> None:
        if self._mod is None:
            raise RuntimeError("engine not loaded")
        threading.Thread(target=self._safe_discover, kwargs={"force": True}, daemon=True).start()

    def _warm_discover(self) -> None:
        self._safe_discover(force=False)

    def _safe_discover(self, force: bool) -> None:
        if self._mod is None:
            raise RuntimeError("engine not loaded")
        try:
            self._mod.discover(force=force, quiet=True)
        except Exception as exc:
            LOG.warning("model catalog refresh failed: %s", exc)

    def _apply_health(self, health: dict) -> None:
        state.gateway_version = str(health.get("version", ""))
        state.gateway_uptime = int(health.get("uptime_sec", 0))

    def _start_watch(self, port: int) -> None:
        self._watch_stop.clear()
        self._watch_thread = threading.Thread(
            target=self._watch, args=(port,), name="gateway-watch", daemon=True
        )
        self._watch_thread.start()

    def _watch(self, port: int) -> None:
        while not self._watch_stop.wait(3.0):
            health = self._health(port)
            if health is not None:
                self._apply_health(health)
                if not self._healthy:
                    self._healthy = True
                    state.gateway_running = True
                    LOG.info("gateway healthy on port %d", port)
            elif self._healthy:
                self._healthy = False
                state.gateway_running = False
                LOG.warning("gateway health check failed on port %d", port)


def json_loads(data: bytes) -> dict:
    import json

    return json.loads(data)
