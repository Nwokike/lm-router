"""Headless BOOT smoke: run the real AppController.init() + render + send path.

The entire boot sequence (services, portal, ads/consent task, connectivity
task, update check task) had never been executed in any test — only isolated
screen renders. This runs init() with a fake page, drains every scheduled
coroutine/thread, and fails on the FIRST unhandled exception, so a boot hang
or crash surfaces here instead of in the GUI. Gateway autostart is off in the
fixture (see the settings stub below); gateway behaviour lives in
test_engine_service.

Network/engine/ad surfaces are stubbed so the test is deterministic and
offline: no gateway bind, no downloads, no flet_ads native calls.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
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

    async def destroy(self) -> None:
        # Coroutines must be awaitable: the quit path hands window.destroy
        # to page.run_task (flet 1.0: destroy is a coroutine function).
        pass


class _StubServices(list):
    """Absorbs Service auto-registration: flet's Service.__post_init__ calls
    page._services.register_service during construction (url launcher,
    haptics, connectivity), which raised on a bare fake page and silently
    disabled every service in the boot tests."""

    def register_service(self, svc):
        with contextlib.suppress(Exception):
            list.append(self, svc)
        return svc

    def unregister_services(self):
        pass


class _BootPage:
    """Fake page: captures run_task coroutines and run_thread callables."""

    def __init__(self) -> None:
        self.platform = ft.PagePlatform.WINDOWS
        self._services = _StubServices()
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
        self.views: list = []

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

    # Gateway autostart OFF: the real _health() probe hangs ~10s against an
    # empty port (sandbox/CI loopback), and drain() joins that thread — every
    # boot test would pay it. Gateway behaviour is engine_service's job.
    (tmp_path / "app_settings.json").write_text(
        json.dumps({"gateway_autostart": False}),
        encoding="utf-8",
    )

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
    assert errors[0].get("kind") == "offline", "error rows carry their kind"
    assert state.busy is False


def test_regenerate_and_edit_dispatch_a_real_turn(boot_page, monkeypatch) -> None:
    """Regenerate/Edit must arm the turn flags before dispatching.

    Only _send_message ever set state.busy=True; the retry paths truncated
    the transcript first and then hit _begin_turn's `not state.busy` guard,
    returning silently — the user's last exchange vanished with no resend.
    A stale _stop_requested (left by any prior Stop) killed the retry twice
    over. Both must now behave like a real send.
    """
    from main import AppController

    controller = AppController(boot_page)
    controller.init()
    boot_page.drain()

    agent = controller.services.agent
    monkeypatch.setattr(agent, "ensure_kani", lambda *a, **k: object())
    monkeypatch.setattr(agent, "start_turn", lambda *a, **k: False)
    state.gateway_running = True

    def _seed() -> None:
        state.messages = [
            {"role": "user", "content": "original question"},
            {"role": "assistant", "content": "stale reply"},
        ]

    # Regenerate, with a stale stop request from an earlier turn.
    _seed()
    state.busy = False
    controller._stop_requested = True
    controller.methods.regenerate_last()
    boot_page.drain()

    assert controller._stop_requested is False, "retry must clear a stale stop request"
    assert state.busy is False, "rejected dispatch must not wedge the UI in busy"
    assert state.messages, "regenerate silently dropped the turn (no dispatch happened)"
    assert state.messages[-1] == {"role": "user", "content": "original question"}, (
        f"expected the re-dispatched user turn, got: {state.messages}"
    )

    # Edit & resend.
    _seed()
    state.busy = False
    controller._stop_requested = True
    controller.methods.edit_last_user("edited question")
    boot_page.drain()

    assert state.busy is False
    assert state.messages and state.messages[-1] == {
        "role": "user",
        "content": "edited question",
    }, f"edit never dispatched, got: {state.messages}"
    assert not any(m.get("content") == "stale reply" for m in state.messages)


def test_mcp_owner_spawned_without_configured_servers(boot_page) -> None:
    """The MCP owner task must exist even with zero servers configured.

    It used to spawn only `if settings.mcp_servers:` at boot, so on a fresh
    install `_reapply_mcp`'s reconnect event had no consumer: the FIRST
    server added was silently dead until restart.
    """
    from main import AppController

    controller = AppController(boot_page)
    controller.init()
    boot_page.drain()

    assert controller.services.settings.mcp_servers == [], "precondition: fresh config"
    assert controller._mcp_future is not None, "owner task must spawn unconditionally"

    # Idempotent: a live owner is never double-spawned.
    future = controller._mcp_future
    controller._ensure_mcp_owner()
    assert controller._mcp_future is future

    # A settings mutation keeps the same live owner while signalling it.
    controller._reapply_mcp()
    assert controller._mcp_future is future


def test_lifecycle_and_back_hooks_registered(boot_page) -> None:
    """The four lifecycle hooks are the only teardown signals on Android
    (window.on_event is desktop-gated), and flet 1.0 exits without atexit."""
    from main import AppController

    controller = AppController(boot_page)
    controller.init()
    boot_page.drain()

    assert boot_page.on_view_pop == controller._on_view_pop
    assert boot_page.on_app_lifecycle_state_change == controller._on_lifecycle
    assert boot_page.on_close == controller._quit_app
    assert boot_page.on_disconnect == controller._on_disconnect


def test_back_underlay_is_installed_once(boot_page) -> None:
    """Without a second view, flet's Dart handler returns null at root and
    the activity finishes without emitting view_pop (page.dart
    _handleSystemPopRoute: views.length <= 1)."""
    from main import AppController

    controller = AppController(boot_page)
    controller.init()
    boot_page.drain()

    boot_page.views = [ft.View(route="/")]
    controller._ensure_back_underlay()
    assert [v.route for v in boot_page.views] == ["/blank", "/"]
    controller._ensure_back_underlay()
    assert len(boot_page.views) == 2, "underlay install must be idempotent"


def test_view_pop_navigates_and_root_follows_keep_running(boot_page, monkeypatch) -> None:
    """Owner rule: system back maps to in-app navigation; at Chat root it
    mirrors the desktop X semantics (keep-running ON backgrounds, OFF quits)."""
    from main import AppController

    controller = AppController(boot_page)
    controller.init()
    boot_page.drain()

    shell = ft.View(route="/")
    boot_page.views = [ft.View(route="/blank", bgcolor=ft.Colors.BLACK, padding=0), shell]
    back_event = lambda: ft.ViewPopEvent(  # noqa: E731 - one-line event factory
        control=None,
        name="view_pop",
        data=None,
        route=shell.route,
    )

    # A non-root tab navigates to Chat and re-keys the popped route so
    # page.dart's pending-pop filter clears (single-patch restore).
    state.selected_tab = 2
    controller._on_view_pop(back_event())
    assert state.selected_tab == 0
    assert shell.route != "/", "restore must re-key the popped route"

    # Root + keep-running ON (default): background, never quit.
    quits: list[int] = []
    monkeypatch.setattr(controller, "_quit_app", lambda: quits.append(1))
    state.selected_tab = 0
    controller._on_view_pop(back_event())
    assert quits == [], "keep-running ON must background, not quit"

    # Root + keep-running OFF: full teardown.
    controller.settings.keep_running_when_closed = False
    controller._on_view_pop(back_event())
    assert quits == [1], "keep-running OFF must quit"


def test_lifecycle_flushes_on_hide_and_reprobes_on_resume(boot_page, monkeypatch) -> None:
    """DDGS shape: flush durable state when backgrounded, re-probe when
    foregrounded — compared on the enum, never e.data (always None)."""
    import asyncio

    from main import AppController

    controller = AppController(boot_page)
    controller.init()
    boot_page.drain()

    saved: list[int] = []
    monkeypatch.setattr(type(controller.settings), "save", lambda self: saved.append(1))

    def _event(state_value):
        return ft.AppLifecycleStateChangeEvent(
            control=None,
            name="app_lifecycle_state_change",
            data=None,
            state=state_value,
        )

    asyncio.run(controller._on_lifecycle(_event(ft.AppLifecycleState.HIDE)))
    assert saved, "HIDE must flush settings (flet exits without atexit)"

    probed: list = []

    async def _fake_recheck(page):
        probed.append(page)
        return True

    monkeypatch.setattr("main.recheck_connectivity", _fake_recheck)
    asyncio.run(controller._on_lifecycle(_event(ft.AppLifecycleState.RESUME)))
    assert probed == [boot_page], "RESUME must re-probe connectivity"

    asyncio.run(controller._on_lifecycle(_event(ft.AppLifecycleState.INACTIVE)))
    assert probed == [boot_page], "INACTIVE must not probe"
    assert saved == [1], "INACTIVE must not flush"


def test_quit_closes_resources_and_is_idempotent(boot_page, monkeypatch) -> None:
    """Quit must stop the share session and http pool, release the ads before
    destroying the window, and never run twice (on_close + button can race)."""
    from main import AppController

    controller = AppController(boot_page)
    controller.init()
    boot_page.drain()

    calls: list[str] = []

    async def _aclose() -> None:
        calls.append("http")

    monkeypatch.setattr(controller.services.http, "aclose", _aclose)
    monkeypatch.setattr(controller, "_stop_share", lambda: calls.append("share"))

    controller._quit_app()
    failures = boot_page.drain()
    assert not failures, failures
    assert "http" in calls, "http pool must close on the portal before stop"
    assert "share" in calls, "share session must stop on quit"
    assert controller._quitting is True

    # Idempotent: a second call dispatches nothing.
    threads_before = len(boot_page.threads)
    controller._quit_app()
    assert len(boot_page.threads) == threads_before, "second quit must be a no-op"


def test_boot_lists_conversations_off_thread(boot_page) -> None:
    """The conversation catalog must load via the async boot task — the old
    sync call parsed every conversation file BEFORE the first paint."""
    import os
    from pathlib import Path

    from main import AppController

    conv_dir = Path(os.environ["FLET_APP_STORAGE_DATA"]) / "conversations"
    conv_dir.mkdir(parents=True, exist_ok=True)
    (conv_dir / "bootseed00001.json").write_text(
        json.dumps(
            {
                "always_included_messages": [],
                "chat_history": [{"role": "user", "content": "seeded"}],
            },
        ),
        encoding="utf-8",
    )

    previous = state.conversations
    state.conversations = []
    try:
        controller = AppController(boot_page)
        controller.init()
        boot_page.drain()
        assert [c["id"] for c in state.conversations] == ["bootseed00001"]
    finally:
        state.conversations = previous


def test_history_mutations_obey_the_busy_guard(boot_page) -> None:
    """Clear/delete must refuse mid-stream (the running turn would rewrite
    the file), deleting a DIFFERENT chat mid-stream stays allowed, and
    deleting the ACTIVE chat while idle rotates to a fresh conversation."""
    import os
    from pathlib import Path

    from main import AppController

    controller = AppController(boot_page)
    controller.init()
    boot_page.drain()

    conv_dir = Path(os.environ["FLET_APP_STORAGE_DATA"]) / "conversations"
    conv_dir.mkdir(parents=True, exist_ok=True)

    def _seed(conv_id: str) -> Path:
        path = conv_dir / f"{conv_id}.json"
        path.write_text(
            json.dumps(
                {
                    "always_included_messages": [],
                    "chat_history": [{"role": "user", "content": conv_id}],
                },
            ),
            encoding="utf-8",
        )
        return path

    active = _seed("busyactive01")
    other = _seed("busyother0001")
    previous_id = state.active_conversation
    state.active_conversation = "busyactive01"
    state.messages = [{"role": "user", "content": "streaming"}]
    try:
        state.busy = True
        threads_before = len(boot_page.threads)

        controller.methods.clear_history()
        controller.methods.delete_conversation("busyactive01")
        assert len(boot_page.threads) == threads_before, "busy must refuse before spawning work"
        assert active.exists() and other.exists()
        # _notify_info surfaces via SnackBar (state.notice is the error path).
        snacks = [
            getattr(getattr(dialog, "content", None), "value", "") for dialog in boot_page.dialogs
        ]
        assert any("Stop the current generation" in text for text in snacks), snacks

        # Deleting a different chat mid-stream IS allowed (DDGS parity).
        controller.methods.delete_conversation("busyother0001")
        boot_page.drain()
        assert not other.exists() and active.exists()

        # Idle: deleting the ACTIVE chat rotates to a fresh conversation.
        state.busy = False
        state.active_conversation = "busyactive01"
        old_id = state.active_conversation
        controller.methods.delete_conversation("busyactive01")
        boot_page.drain()
        assert not active.exists()
        assert state.active_conversation != old_id, "active delete must rotate the id"
        assert state.messages == [], "rotation must clear the transcript"
    finally:
        state.busy = False
        state.active_conversation = previous_id
        state.messages = []


def test_stop_gateway_tears_down_the_share_session(boot_page, monkeypatch) -> None:
    """The share card unmounts when the gateway stops, so the session must
    stop with it — a live tunnel with no UI would keep answering 502, and the
    stale share_url resurfaced as 'Stop sharing' on restart."""
    from main import AppController

    controller = AppController(boot_page)
    controller.init()
    boot_page.drain()

    class _SentinelSession:
        stopped = False

        def stop(self) -> None:
            _SentinelSession.stopped = True

    session = _SentinelSession()
    controller._share = session  # type: ignore[assignment]
    state.share_url = "https://example.lhr.life"
    state.share_error = "old failure"
    try:
        controller.methods.stop_gateway()
        boot_page.drain()
        assert _SentinelSession.stopped, "share session must stop with the gateway"
        assert controller._share is None
        assert state.share_url == "", "stale share_url must not survive a gateway stop"
        assert state.share_error == "", "a stopped session must not keep a red banner"
    finally:
        controller._share = None
        state.share_url = ""
        state.share_error = ""


def test_regenerate_share_key_reaches_the_settings_snapshot(boot_page) -> None:
    """The key field and Copy button read the settings_version-keyed
    snapshot — without the bump they kept showing the OLD key."""
    from main import AppController

    controller = AppController(boot_page)
    controller.init()
    boot_page.drain()

    before_version = state.settings_version
    before_key = controller.settings.share_key
    controller.methods.regenerate_share_key()
    assert state.settings_version == before_version + 1
    assert controller.settings.share_key != before_key
    assert controller.settings.share_key.startswith("sk-lm-")


def test_mcp_mutators_bump_the_settings_snapshot(boot_page) -> None:
    """The Settings MCP list re-reads only on settings_version — add, toggle,
    per-tool toggle and remove each used to save to disk while the list on
    screen stayed stale until a tab switch remounted it."""
    from main import AppController

    controller = AppController(boot_page)
    controller.init()
    boot_page.drain()

    before = state.settings_version
    controller.methods.add_mcp_server(
        {"name": "probe", "transport": "streamable_http", "url": "http://127.0.0.1:9/mcp"},
    )
    assert state.settings_version == before + 1, "add must bump the snapshot"
    assert controller.settings.mcp_servers, "precondition: the server was stored"
    server_id = controller.settings.mcp_servers[0].id

    before = state.settings_version
    controller.methods.toggle_mcp_server(server_id)
    assert state.settings_version == before + 1, "toggle must bump the snapshot"

    before = state.settings_version
    controller.methods.toggle_mcp_tool(server_id, "some_tool")
    assert state.settings_version == before + 1, "tool toggle must bump the snapshot"

    before = state.settings_version
    controller.methods.remove_mcp_server(server_id)
    assert state.settings_version == before + 1, "remove must bump the snapshot"
    assert not controller.settings.mcp_servers


def test_search_enabled_save_syncs_the_chat_pill_observable(boot_page) -> None:
    """The chat pill reads state.search_enabled, not the settings model —
    the Settings switch used to persist without ever moving the pill."""
    from main import AppController

    controller = AppController(boot_page)
    controller.init()
    boot_page.drain()

    state.search_enabled = False
    controller.methods.save_settings({"search_enabled": True})
    assert state.search_enabled is True, "observable must follow the saved setting"

    controller.methods.save_settings({"search_enabled": False})
    assert state.search_enabled is False


def test_boot_primes_the_model_and_keeps_the_connectivity_handle(boot_page) -> None:
    """A remembered model must exist before any catalog arrives (a failed
    /models fetch used to kill every send with 'No model selected yet.'),
    and the Connectivity service instance must be RETAINED — flet never sets
    page.connectivity itself, so discarding it left the OS listener dead."""
    import os
    from pathlib import Path

    from main import AppController

    # last_model must exist before AppController loads settings.
    data_dir = Path(os.environ["FLET_APP_STORAGE_DATA"])
    (data_dir / "app_settings.json").write_text(
        json.dumps({"gateway_autostart": False, "last_model": "remembered-model"}),
        encoding="utf-8",
    )

    previous_model = state.model
    try:
        controller = AppController(boot_page)
        controller.init()
        boot_page.drain()
        assert state.model == "remembered-model", "last_model must seed state.model"

        connectivity = getattr(boot_page, "connectivity", None)
        assert isinstance(connectivity, ft.Connectivity), (
            "the Connectivity instance must be bound so on_change can fire"
        )
        assert connectivity in boot_page.services
    finally:
        state.model = previous_model


def test_share_key_settings_rebind_the_live_proxy(boot_page) -> None:
    """Toggle/key changes while sharing must reach the LIVE proxy (the
    handler captured its key at bind time — both directions were stale
    until a manual restart). No session => the hooks are silent no-ops."""
    from main import AppController

    controller = AppController(boot_page)
    controller.init()
    boot_page.drain()

    class _FakeSession:
        def __init__(self) -> None:
            self.keys: list[str] = []

        def rekey(self, key: str) -> bool:
            self.keys.append(key)
            return True

    # Not sharing: save/regenerate must not try to rebind anything.
    controller.methods.save_settings({"require_share_key": True})
    controller.methods.regenerate_share_key()
    assert controller._share is None

    session = _FakeSession()
    controller._share = session  # type: ignore[assignment]

    # require_share_key ON: the derived key is a fresh sk-lm- (auto-generated
    # because the field starts empty).
    controller.methods.save_settings({"require_share_key": True})
    assert session.keys, "toggling the key while sharing must rebind the proxy"
    assert session.keys[-1].startswith("sk-lm-"), session.keys

    # require_share_key OFF: the live proxy must OPEN.
    controller.methods.save_settings({"require_share_key": False})
    assert session.keys[-1] == "", "turning the key off must rebind with no key"

    # Regenerating mid-share rebinds with the new key (key required again —
    # with the key OFF the live proxy correctly stays open even though a
    # stored key is generated for later).
    controller.methods.save_settings({"require_share_key": True})
    before = session.keys[-1]
    assert before.startswith("sk-lm-"), session.keys
    controller.methods.regenerate_share_key()
    assert session.keys[-1].startswith("sk-lm-") and session.keys[-1] != before

    controller._share = None


def test_bench_guards_and_live_sweep(boot_page) -> None:
    """Guards first (gateway off, nothing-to-retest, double sweep), then a
    REAL sweep through the portal with an injected transport: only not-ready
    rows are probed and the verdict lands in state."""
    import json as _json

    import httpx as _hx

    from main import AppController

    controller = AppController(boot_page)
    controller.init()
    boot_page.drain()

    saved = (
        state.gateway_running,
        state.models,
        state.model_testing,
        state.model_test_results,
        state.retesting,
        state.retest_progress,
    )
    try:
        # Guard 1: gateway off.
        state.gateway_running = False
        threads = len(boot_page.threads)
        controller.methods.test_model("x")
        assert len(boot_page.threads) == threads, "no probe while the gateway is down"
        assert any(
            "Gateway is not running" in str(getattr(getattr(d, "content", None), "value", ""))
            for d in boot_page.dialogs
        ), "the refusal must be visible"

        # Guard 2: nothing matches the filter.
        state.gateway_running = True
        state.models = [{"id": "a", "status": "active"}]
        controller.methods.retest_models(True)
        assert len(boot_page.threads) == threads, "no sweep when nothing is not-ready"
        assert any(
            "Nothing to retest" in str(getattr(getattr(d, "content", None), "value", ""))
            for d in boot_page.dialogs
        )

        # Guard 3: double sweep.
        state.retesting = True
        controller.methods.retest_models(False)
        assert len(boot_page.threads) == threads, "no second sweep while one runs"
        state.retesting = False

        # Live sweep through the portal: only the not-ready row is probed.
        probed: list[str] = []

        def handler(request):
            body = _json.loads(request.content or b"{}")
            probed.append(body["model"])
            return _hx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

        controller.services.http._client = _hx.AsyncClient(transport=_hx.MockTransport(handler))
        state.models = [
            {"id": "cap", "status": "untested"},
            {"id": "fine", "status": "active"},
        ]
        state.model_test_results = {}
        controller.methods.retest_models(True)
        boot_page.drain()

        assert probed == ["cap"], f"only not-ready rows are swept, probed={probed}"
        assert state.model_test_results["cap"]["verdict"] == "OK"
        assert state.retesting is False, "sweep must clear the flag"

        # Single-model probe (the per-row Test / retry).
        probed.clear()
        controller.methods.test_model("fine")
        boot_page.drain()
        assert probed == ["fine"]
        assert state.model_test_results["fine"]["verdict"] == "OK"
        assert state.model_testing == frozenset()
    finally:
        (
            state.gateway_running,
            state.models,
            state.model_testing,
            state.model_test_results,
            state.retesting,
            state.retest_progress,
        ) = saved


def test_boot_log_never_carries_gateway_tokens(boot_page) -> None:
    """The ring renders on the Server screen: a boot that logged a gateway
    token (invariant 3) would surface it to any user who opens the log."""
    from test_router_invariants import FORBIDDEN

    from core import logging as applog
    from main import AppController

    controller = AppController(boot_page)
    controller.init()
    boot_page.drain()

    blob = " ".join(record.get("msg", "") for record in applog.records()).lower()
    for token in FORBIDDEN:
        assert token not in blob, f"gateway token {token!r} surfaced in the log ring"
