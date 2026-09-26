"""Gateway engine service: download, import, run, watch, stop.

The engine is kiri-router's run.py (stdlib-only, main-guarded). It is fetched
LIVE from router.kiri.ng on every startup and cached to user storage; nothing
about the gateway is hardcoded in this app, because upstream changes constantly
and a bundled snapshot would silently shadow it. Everything here is sync urllib
+ threads on purpose: it must never share an event loop with Flet or kani
(anyio study).
"""

import contextlib
import importlib.util
import json
import os
import re
import threading
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core import constants, storage
from core.logging import LOG
from core.settings import AppSettings
from core.state import state
from services.netinfo import get_lan_ip

MODULE_NAME = "lm_router_engine"
VERSION_RE = re.compile(rb'VERSION = "([^"]+)"')
USER_AGENT = f"LM-Router/{constants.APP_VERSION} (+https://router.kiri.ng)"


class EngineUnavailable(RuntimeError):
    """No gateway could be obtained: live fetch failed and no cache exists."""


def _download(url: str) -> bytes:
    # router.kiri.ng answers 403 to urllib's default User-Agent
    # ("Python-urllib/3.x") and 200 once a real one is sent, so the app could
    # never fetch its own gateway without this header.
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})  # noqa: S310
    # url is the app-controlled https constant from constants.ENGINE_URL.
    with urllib.request.urlopen(request, timeout=30) as resp:  # noqa: S310
        return resp.read()


def _validate(data: bytes) -> str:
    """Structural check of a downloaded engine + its VERSION label.

    The label is only ever displayed ("fetched 1.0.0"), never compared, so a
    local normalisation is enough and the `packaging` dependency is not needed.
    """
    if b'if __name__ == "__main__":' not in data:
        raise ValueError("missing main guard")
    match = VERSION_RE.search(data)
    if not match:
        raise ValueError("missing VERSION token")
    raw = match.group(1).decode("ascii", errors="replace").strip()
    if not raw:
        raise ValueError("empty VERSION token")
    # Keep only what is safe to print in a status label.
    label = re.sub(r"[^A-Za-z0-9.+_-]", "", raw)[:40]
    if not label:
        raise ValueError(f"unusable VERSION token: {raw!r}")
    return label


def _load_module(path: Path) -> object:
    spec = importlib.util.spec_from_file_location(MODULE_NAME, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load engine from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_engine() -> tuple[object, str]:
    """Live download first, then the last-known-good runtime cache.

    Nothing about the gateway is hardcoded in this app: router.kiri.ng changes
    constantly, so every startup fetches it fresh. The only fallback is the
    cache written by the previous successful fetch (user storage, not the
    repo), which keeps a cold offline start working without shipping a stale
    snapshot that would silently shadow the live router.
    """
    cache = storage.engine_cache_path()
    # MUST end in .py: spec_from_file_location returns None (no loader) for
    # unrecognized suffixes, which would reject even a perfect download.
    tmp = cache.with_name(cache.stem + ".new.py")
    try:
        data = _download(constants.ENGINE_URL)
        version = _validate(data)
        cache.parent.mkdir(parents=True, exist_ok=True)
        # Prove the download BEFORE touching the cache: a bad upstream that
        # still passes _validate (main guard + VERSION) used to overwrite the
        # good copy and then fail to import — permanently bricking offline
        # starts. Only a loadable download replaces the last-known-good file.
        tmp.write_bytes(data)
        module = _load_module(tmp)
        os.replace(tmp, cache)
        return module, f"fetched {version}"
    except Exception as exc:
        LOG.warning("engine download unusable (%s); trying last cached copy", exc)
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
    try:
        data = cache.read_bytes()
        version = _validate(data)
        return _load_module(cache), f"cached {version}"
    except Exception as exc:
        raise EngineUnavailable(
            f"Could not reach {constants.ENGINE_URL} and no cached gateway is "
            f"available ({exc}). Connect to the internet and retry.",
        ) from exc


class EngineService:
    def __init__(
        self,
        settings: AppSettings,
        on_ui: Callable[[Callable[[], None]], Any] | None = None,
    ) -> None:
        self.settings = settings
        # Scheduler that runs a mutation ON the Flet UI loop (observable
        # repaints are scheduled via a non-thread-safe asyncio.Event). The
        # controller injects its _run_on_ui; None means direct calls (tests).
        self._on_ui = on_ui
        self._mod: object | None = None
        self._server: object | None = None
        self._thread: threading.Thread | None = None
        self._watch_stop = threading.Event()
        self._watch_thread: threading.Thread | None = None
        self._healthy = False
        self.source = ""
        self.port = 0
        # Autostart runs on a worker thread while the user may press
        # Start at the same moment. Without this, both call acquire_server
        # on the same port: one wins, the other reports a bind failure —
        # which looked like 'the first press does nothing, the second works'.
        self._lifecycle = threading.Lock()

    def _ui(self, fn: Callable[[], None]) -> None:
        if self._on_ui is None:
            fn()
            return
        try:
            self._on_ui(fn)()
        except Exception as exc:
            LOG.error("engine UI update failed: %s", exc)

    def load(self) -> str:
        if self._mod is None:
            self._mod, self.source = load_engine()
            self._ui(lambda: setattr(state, "gateway_source", self.source))
            LOG.info("engine %s", self.source)
        return self.source

    @staticmethod
    def _health(port: int) -> dict | None:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            LOG.debug("health probe failed: %s", exc)
            return None
        if data.get("adapter") != "kiri-router":
            return None
        return data

    @staticmethod
    def _fetch_account_limits(port: int) -> dict | None:
        try:
            url = f"http://127.0.0.1:{port}/account-limits"
            with urllib.request.urlopen(url, timeout=2) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            LOG.debug("account-limits probe failed: %s", exc)
            return None

    def start(self) -> tuple[str, int]:
        """Start or adopt the gateway. Returns (status, port).

        Serialised: boot autostart and a manual press can arrive together, and
        two threads racing to bind one port made the first press look broken.
        """
        with self._lifecycle:
            return self._start_locked()

    def _start_locked(self) -> tuple[str, int]:
        if self._server is not None:
            return "started", self.port
        self.load()
        if self._mod is None:
            raise RuntimeError("engine not loaded")
        server, port = self._mod.acquire_server(self.settings.gateway_port)

        lan_ip = get_lan_ip()
        self._ui(lambda: setattr(state, "gateway_lan_ip", lan_ip))

        if server is None:
            # Another current-gen gateway already serves this port.
            self.port = port
            health = self._health(port)
            self._apply_health(health or {})

            def _adopted() -> None:
                state.gateway_running = health is not None
                state.gateway_port = port
                state.gateway_base_url = f"http://127.0.0.1:{port}/v1"
                state.gateway_lan_url = f"http://{lan_ip}:{port}/v1"

            self._ui(_adopted)
            self._start_watch(port)
            LOG.info("adopted existing gateway on port %d", port)
            return "adopted", port

        server.access_log = False  # our ring is the UI log
        self._server = server
        self.port = int(server.server_address[1])
        self._thread = threading.Thread(target=server.serve_forever, name="gateway", daemon=True)
        self._thread.start()

        def _started() -> None:
            state.gateway_port = self.port
            state.gateway_base_url = f"http://127.0.0.1:{self.port}/v1"
            state.gateway_lan_url = f"http://{lan_ip}:{self.port}/v1"
            state.gateway_running = True

        self._ui(_started)
        health = self._health(self.port)
        self._apply_health(health or {})
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
        self._ui(lambda: setattr(state, "gateway_running", False))
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
        def _apply() -> None:
            state.gateway_version = str(health.get("version", ""))
            state.gateway_uptime = int(health.get("uptime_sec", 0))

        self._ui(_apply)

    def _start_watch(self, port: int) -> None:
        self._watch_stop.clear()
        # The watch thread only probes; every state mutation goes through
        # self._ui (the Flet loop scheduler), so no context copy is needed.
        self._watch_thread = threading.Thread(
            target=self._watch,
            args=(port,),
            name="gateway-watch",
            daemon=True,
        )
        self._watch_thread.start()

    def _watch(self, port: int) -> None:
        while not self._watch_stop.wait(3.0):
            try:
                health = self._health(port)
                if health is not None:
                    self._apply_health(health)
                    if not self._healthy:
                        self._healthy = True
                        self._ui(lambda: setattr(state, "gateway_running", True))
                        LOG.info("gateway healthy on port %d", port)
                elif self._healthy:
                    self._healthy = False
                    self._ui(lambda: setattr(state, "gateway_running", False))
                    LOG.warning("gateway health check failed on port %d", port)
            except Exception as exc:
                # never let one bad tick kill the watchdog thread
                LOG.warning("gateway watch tick failed: %s", exc)


def json_loads(data: bytes) -> dict:
    import json

    return json.loads(data)
