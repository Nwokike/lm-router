"""Headless BOOT smoke: run the real AppController.init() + render + send path.

The entire boot sequence (services, portal, ads/consent task, connectivity
task, gateway autostart thread, update check task) had never been executed in
any test — only isolated screen renders. This runs init() with a fake page,
drains every scheduled coroutine/thread, and fails on the FIRST unhandled
exception, so a boot hang or crash surfaces here instead of in the GUI.

Network/engine/ad surfaces are stubbed so the test is deterministic and
offline: no gateway bind, no downloads, no flet_ads native calls.
"""

from __future__ import annotations

import asyncio
import threading
import time
import traceback

import flet as ft
import pytest
from flet.components.component import Renderer
from flet.controls.context import _context_page

from core.state import state


class _FakeWindow:
    prevent_close = False
    on_event = None
    visible = True

    def destroy(self) -> None:
        pass


class _BootPage:
    """Fake page: captures run_task coroutines and run_thread callables."""

    def __init__(self) -> None:
        self.platform = ft.PagePlatform.WINDOWS
        self.window = _FakeWindow()
        self.theme = None
        self.dark_theme = None
        self.theme_mode = ft.ThemeMode.SYSTEM
        self.fonts: dict[str, str] = {}
        self.services: list = []
        self.on_error = None
        self.tasks: list = []
        self.threads: list[threading.Thread] = []
        self.dialogs: list = []
        self.updates = 0

    def run_task(self, fn, *args) -> None:
        if callable(fn):
            coro = fn(*args)
        else:
            coro = fn
        self.tasks.append(coro)

    def run_thread(self, fn, *args) -> None:
        thread = threading.Thread(target=fn, args=args, daemon=True)
        thread.start()
        self.threads.append(thread)

    def show_dialog(self, dialog) -> None:
        self.dialogs.append(dialog)

    def pop_dialog(self):
        return self.dialogs.pop() if self.dialogs else None

    def update(self) -> None:
        self.updates += 1

    def drain(self, timeout: float = 20.0) -> list[tuple[str, BaseException]]:
        """Run captured coroutines on a loop and join worker threads."""
        failures: list[tuple[str, BaseException]] = []
        if self.tasks:

            async def _all() -> None:
                for coro in self.tasks:
                    await coro

            try:
                asyncio.run(asyncio.wait_for(_all(), timeout=timeout))
            except BaseException as exc:
                failures.append(("drain_tasks", exc))
            self.tasks.clear()
        for thread in self.threads:
            thread.join(timeout=timeout)
        self.threads.clear()
        return failures


@pytest.fixture
def boot_page(monkeypatch, tmp_path):
    """Fake page + fully stubbed boot surface; yields the page."""
    monkeypatch.setenv("FLET_APP_STORAGE_DATA", str(tmp_path))

    # Engine: adopted-gateway stub, no bind, no download.
    import services.engine as engine_mod

    class _StubEngine:
        @staticmethod
        def acquire_server(want_port: int):
            return None, want_port

    monkeypatch.setattr(
        engine_mod,
        "load_engine",
        lambda: (_StubEngine(), "test 0.0.0"),
    )

    # Update check: no network.
    from services.update_service import UpdateService

    async def _no_update(*_a, **_k):
        return None

    monkeypatch.setattr(UpdateService, "check_for_updates", _no_update)

    # Tokenizer prewarm: no downloads.
    monkeypatch.setattr("main.prewarm_tokenizers", lambda: None)

    # Ads/consent + connectivity: no native flet_ads, no 5s poll loop.
    from services.ad_service import AdService

    async def _no_consent(self) -> None:
        return

    async def _no_preload(self) -> None:
        return

    monkeypatch.setattr(AdService, "gather_consent", _no_consent)
    monkeypatch.setattr(AdService, "preload_interstitial", _no_preload)

    async def _no_monitor(_page) -> None:
        return

    monkeypatch.setattr("components.connectivity_monitor.start_connectivity_monitor", _no_monitor)
    monkeypatch.setattr("main.start_connectivity_monitor", _no_monitor)

    page = _BootPage()
    _context_page.set(page)

    # Reset the observable singleton to a clean boot.
    state.busy = False
    state.gateway_running = False
    state.messages = []
    state.notice = ""
    state.selected_tab = 0
    state.mcp_testing = frozenset()
    yield page

    from services.agent import AgentService  # noqa: F401  (ensure import graph is warm)


def test_boot_init_and_render_produce_no_hidden_failure(boot_page) -> None:
    from main import AppController

    start = time.monotonic()
    controller = AppController(boot_page)
    controller.init()
    init_seconds = time.monotonic() - start
    assert init_seconds < 15.0, f"init() took {init_seconds:.1f}s — UI would freeze"

    # First render of the shell (Chat screen by default).
    from app_shell import AppShell
    from state.controller_ctx import ControllerMethodsCtx

    root = Renderer().render(
        lambda: ControllerMethodsCtx(controller.methods, lambda: AppShell()),
    )
    assert root is not None

    # Drain boot-scheduled work (ads task, update task) and worker threads.
    failures = boot_page.drain()
    details = "\n".join(
        f"{where}: {type(exc).__name__}: {exc}\n{traceback.format_exception(exc)}"
        for where, exc in failures
    )
    assert not failures, f"boot background work failed:\n{details}"


def test_send_while_gateway_down_shows_visible_error(boot_page) -> None:
    """A blocked/failed send must ALWAYS leave a visible signal."""
    from main import AppController

    controller = AppController(boot_page)
    controller.init()
    boot_page.drain()

    state.gateway_running = False
    state.messages = []
    controller.methods.send_message("hello")
    # Send is dispatched to a worker; join it.
    for thread in boot_page.threads:
        thread.join(timeout=10)

    errors = [m for m in state.messages if m.get("role") == "error"]
    assert errors, f"send with gateway down produced no visible error: {state.messages}"
    assert "Gateway" in errors[0]["content"]
    assert state.busy is False
