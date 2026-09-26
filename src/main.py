"""LM Router entry point."""

import asyncio
import contextlib
import contextvars
import os
import threading
import time
from collections.abc import Callable
from typing import Any

import flet as ft
from pydantic import ValidationError

from app_shell import AppShell
from components.about_dialog import build_about_dialog
from components.connectivity_monitor import recheck_connectivity, start_connectivity_monitor
from components.log_terminal import build_log_terminal
from components.update_dialog import build_update_dialog
from core import constants, theme
from core.catalog import AUTO_MODEL_ID, chat_models
from core.logging import LOG
from core.logging import tail as log_tail
from core.notify import show_snack
from core.settings import AppSettings, MCPServerConfig, ProviderConfig, pop_load_warnings
from core.state import state
from services import history, model_bench
from services.ad_service import AdService
from services.agent import AgentService
from services.clock import build_time_tool, with_clock
from services.engine import EngineService
from services.file_save import save_text_file
from services.http import HttpService
from services.mcp import MCPHub
from services.reasoning import final_reasoning
from services.search import build_search_tool
from services.share import ShareSession, generate_key
from services.tokenizer import prewarm_tokenizers
from services.tunnel import LocalTunnel
from services.update_service import UpdateService
from state.controller_ctx import ControllerMethods, ControllerMethodsCtx
from state.service_ctx import ServiceCtx, Services

THEME_MODES = {
    "system": ft.ThemeMode.SYSTEM,
    "light": ft.ThemeMode.LIGHT,
    "dark": ft.ThemeMode.DARK,
}


class AppController:
    def __init__(self, page: ft.Page) -> None:
        self.page = page
        self.settings = AppSettings.load()
        self.services = Services()
        self.methods = ControllerMethods()
        self._url_launcher: object | None = None
        self.ads: AdService | None = None
        self._stop_requested = False
        self._ui_loop: asyncio.AbstractEventLoop | None = None
        # 2Hz log-render budget (Sherlock's flusher pattern): log records
        # mark dirty; ONE task turns bursts into at most two renders/second.
        self._log_dirty = False
        self._log_last_emit = 0.0
        self._log_flush_task: asyncio.Task | None = None
        self._mcp_future: Any = None
        self._share: ShareSession | None = None
        self._tunnel: LocalTunnel | None = None
        self._share_url = ""
        self._quitting = False  # run-once guard: on_close + Quit can race
        self._retest_stop: threading.Event | None = None
        self._back_seq = 0  # route re-key counter for view_pop restores
        # flet page context captured at init: chat callbacks run on the anyio
        # portal thread, which has no context of its own (kani thread study).
        self._flet_ctx = contextvars.copy_context()

    def init(self) -> None:
        page = self.page
        # House escape hatch (DDGS pattern): dialogs and imperative helpers
        # outside the component tree can reach the controller through the page.
        page._lmrouter_controller = self
        settings = self.settings

        # Capture the Flet UI event loop. Observable repaints are scheduled
        # through an asyncio.Event (session.schedule_update) which is NOT
        # thread-safe — every state mutation from a worker MUST be marshaled
        # onto this loop or the UI silently never updates (the "button does
        # nothing" class of bugs; Sherlock's run_coroutine_threadsafe rule).
        try:
            self._ui_loop = asyncio.get_running_loop()
        except RuntimeError:
            try:
                self._ui_loop = page.session.connection.loop
            except Exception:
                self._ui_loop = None

        state.theme_mode = settings.theme
        state.gateway_port = settings.gateway_port
        state.onboarding_done = settings.onboarding_done
        state.terms_accepted = settings.terms_accepted
        state.search_enabled = settings.search_enabled
        # Remembered model before any catalog arrives: a running gateway
        # whose /models fetch failed used to fail every send with "No model
        # selected yet." even though last_model was known.
        state.model = settings.last_model or ""

        # Surface anything load() had to skip/drop (dropped provider keys,
        # malformed stored items) — never silently lose user data state.
        load_warnings = pop_load_warnings()
        if load_warnings:
            state.notice = "; ".join(load_warnings)
        # KTV/Sherlock/DDGS all zero the page chrome: insets come from
        # ft.SafeArea in the shell, never from implicit page padding.
        page.padding = 0
        page.spacing = 0
        page.theme = theme.get_light_theme()
        page.dark_theme = theme.get_dark_theme()
        page.theme_mode = THEME_MODES.get(settings.theme, ft.ThemeMode.SYSTEM)
        page.fonts = theme.FONTS

        # Keep-alive: closing the window hides it to the taskbar so the
        # gateway thread keeps running (locked v1 decision).
        try:
            if page.platform.is_desktop():
                page.window.prevent_close = True
                page.window.on_event = self._on_window_event
        except Exception as exc:
            LOG.info("window close hook unavailable: %s", exc)

        # Surface uncaught UI errors in-app (the terminal may be invisible).
        try:
            page.on_error = self._on_page_error
        except Exception as exc:
            LOG.info("page error hook unavailable: %s", exc)

        # Lifecycle: flet 1.0 exits without running atexit/buffered writes
        # (DDGS), and on Android these are the ONLY teardown signals — the
        # window.on_event hook above is desktop-gated. on_close/on_disconnect
        # are session events (web/session-expiry) and double as the desktop
        # safety net; both handlers are idempotent.
        try:
            page.on_view_pop = self._on_view_pop
            page.on_app_lifecycle_state_change = self._on_lifecycle
            page.on_close = self._quit_app
            page.on_disconnect = self._on_disconnect
        except Exception as exc:
            LOG.info("lifecycle hooks unavailable: %s", exc)

        # services
        http = HttpService()
        engine = EngineService(settings, on_ui=self._run_on_ui)
        # Phones cannot spawn stdio MCP servers; capture the platform once on
        # the Flet thread so the hub can refuse them with a clear message
        # instead of hanging on a subprocess that will never start.
        try:
            is_mobile = bool(page.platform and page.platform.is_mobile())
        except Exception:
            is_mobile = False
        hub = MCPHub(settings, is_mobile=is_mobile, on_status=self._mcp_status)
        agent = AgentService(settings, extra_tools=self._extra_tools)
        self.services.settings = settings
        self.services.http = http
        self.services.engine = engine
        self.services.mcp = hub
        self.services.agent = agent
        self._search_tool = build_search_tool(http)
        self._time_tool = build_time_tool()
        agent.start()  # anyio portal thread: hosts kani turns and httpx calls
        try:
            self._url_launcher = ft.UrlLauncher()
            page.services.append(self._url_launcher)
        except Exception as exc:
            LOG.info("url launcher unavailable: %s", exc)
        try:
            page.services.append(ft.HapticFeedback())
        except Exception as exc:
            LOG.info("haptics unavailable: %s", exc)

        # Conversation catalog OFF the UI thread: list_conversations parses
        # every conversation file (O(n x size)) and init() runs before the
        # first paint — DDGS moved the identical call for the identical
        # reason. The fresh id is a cheap uuid and can stay synchronous.
        page.run_task(self._boot_conversations)
        if not state.active_conversation:
            history.start_conversation()

        # connectivity: OS events + HTTP confirm, banner rendered by the shell
        try:
            conn = ft.Connectivity()
            page.services.append(conn)
            # flet 1.0 never sets page.connectivity itself (Connectivity is a
            # Service; register_service only appends) — bind it here so the
            # monitor's fast path and on_change wiring actually run, the way
            # all three references retain the handle.
            page.connectivity = conn
        except Exception as exc:
            LOG.info("connectivity service unavailable: %s", exc)
        page.run_task(start_connectivity_monitor, page)

        # ads: UMP consent then preload (gates live inside AdService)
        self.ads = AdService(page)
        page.run_task(self._boot_ads)
        page.run_thread(prewarm_tokenizers)

        # MCP owner task: ONE long-lived portal task owns the session
        # context (enter+exit in the same task). Config changes just signal
        # it; it never shares a cancel scope across portal tasks. Spawned
        # UNCONDITIONALLY — with the old `if settings.mcp_servers:` guard a
        # fresh install had no consumer for request_reconnect(), so the first
        # server added was silently dead until restart. An empty config is a
        # no-op inside _connect() (it short-circuits and the loop parks).
        agent.on_portal_restart = self._ensure_mcp_owner
        self._ensure_mcp_owner()

        # controllers wired for this step (later steps extend the same object)
        m = self.methods
        m.set_tab = self._set_tab
        m.set_theme = self._set_theme
        m.start_gateway = self._start_gateway
        m.stop_gateway = self._stop_gateway
        m.refresh_models = self._refresh_models
        m.open_gateway_console = self._open_console
        m.start_share = self._start_share
        m.stop_share = self._stop_share
        m.regenerate_share_key = self._regenerate_share_key
        m.test_model = self._test_model
        m.retest_models = self._retest_models
        m.stop_retest = self._stop_retest
        m.quit_app = self._quit_app
        m.send_message = self._send_message
        m.regenerate_last = self._regenerate_last
        m.edit_last_user = self._edit_last_user
        m.stop_generation = self._stop_generation
        m.set_model = self._set_model
        m.new_conversation = self._new_conversation
        m.finish_onboarding = self._finish_onboarding
        m.open_url = self._open_url
        m.add_mcp_server = self._add_mcp_server
        m.remove_mcp_server = self._remove_mcp_server
        m.toggle_mcp_server = self._toggle_mcp_server
        m.toggle_mcp_tool = self._toggle_mcp_tool
        m.test_mcp_server = self._test_mcp_server
        m.save_settings = self._save_settings
        m.add_provider = self._add_provider
        m.remove_provider = self._remove_provider
        m.select_provider = self._select_provider
        m.clear_history = self._clear_history
        m.open_conversation = self._open_conversation
        m.delete_conversation = self._delete_conversation
        m.copy_text = self._copy_text
        m.copy_logs = self._copy_logs
        m.export_conversation = self._export_conversation
        m.toggle_search_tool = self._toggle_search_tool
        m.open_mcp_tools = self._open_mcp_tools
        m.dismiss_notice = self._dismiss_notice
        m.check_update = lambda: self.page.run_task(self._check_update)
        m.open_update_dialog = self._open_update_dialog
        m.open_about = self._open_about
        m.open_log_terminal = self._open_log_terminal
        m.dismiss_update = self._dismiss_update

        import core.logging as applog

        applog.on_record = self._bump_logs
        LOG.info("LM Router %s starting", constants.APP_VERSION)

        if settings.gateway_autostart:
            page.run_thread(self._start_gateway_quiet)
        if settings.update_check:
            page.run_task(self._check_update, True)

    # controller implementations

    def _run_on_ui(self, fn: Callable) -> Callable:
        """Return a wrapper that runs fn ON the Flet event loop.

        Flet schedules repaints via an asyncio.Event that is not thread-safe:
        an observable mutation from a worker thread sets the event on the wrong
        loop and the UI silently never repaints. This blocks the calling
        worker (never the UI) until the loop has executed fn. Falls back to a
        direct call when the loop cannot be determined (tests/headless), and
        a timed-out or failed marshal ALSO falls back to a direct call: the
        payload is a state write, and dropping it strands a flag forever.
        """
        loop = self._ui_loop
        ctx = self._flet_ctx

        def run(*args: object, **kwargs: object) -> object:
            call = lambda: fn(*args, **kwargs)  # noqa: E731
            if loop is None:
                return ctx.copy().run(call)
            try:
                on_loop = asyncio.get_running_loop() is loop
            except RuntimeError:
                on_loop = False
            if on_loop:
                return ctx.copy().run(call)

            async def _call() -> object:
                return ctx.copy().run(call)

            def _direct(why: str) -> object:
                """Last resort: run it here rather than drop the payload.

                Losing the write is how state flags got stranded: the worker's
                update (share_starting=False, a cleared share URL) was silently
                discarded and the button stayed dead forever. Off-loop is not
                ideal, but writing the state beats never writing it.
                """
                LOG.error("UI callback %s: %s", why, getattr(fn, "__name__", fn))
                try:
                    return ctx.copy().run(call)
                except Exception:
                    LOG.error(
                        "UI callback direct fallback failed: %s",
                        getattr(fn, "__name__", fn),
                        exc_info=True,
                    )
                    return None

            try:
                return asyncio.run_coroutine_threadsafe(_call(), loop).result(timeout=30)
            except TimeoutError:
                return _direct("timed out")
            except Exception:
                # Closed loop, dead connection, anything but a timeout.
                return _direct("failed")

        return run

    def _notify_error(self, message: str) -> None:
        """Surface a failure as a banner AND a scroll-independent snack.

        The banner alone is invisible whenever the shell is scrolled away:
        during onboarding, mid-conversation, or on another tab. The snack is
        an overlay, so the message is always seen. Never swallow.
        """

        def _apply() -> None:
            state.notice = message
            with contextlib.suppress(Exception):
                show_snack(self.page, message, bgcolor=theme.ERROR, duration=6000)

        LOG.warning("notice: %s", message)
        self._run_on_ui(_apply)()

    def _dismiss_notice(self) -> None:
        state.notice = ""

    def _notify_info(self, message: str) -> None:
        """Non-error feedback (floating SnackBar); falls back to the banner.

        A second info message within 2.5s used to hit the "Dialog is already
        opened" RuntimeError and drop into the banner, which can be off-screen.
        Use the same replace-a-lingering-snack dance as core/notify.py.
        """
        LOG.info("notice: %s", message)

        def _apply() -> None:
            def _show() -> None:
                self.page.show_dialog(
                    ft.SnackBar(
                        content=ft.Text(message, color=ft.Colors.WHITE),
                        behavior=ft.SnackBarBehavior.FLOATING,
                        duration=2500,
                    ),
                )

            def _fallback(exc: Exception) -> None:
                LOG.warning("info snack failed: %s", exc)
                state.notice = message

            try:
                _show()
            except RuntimeError:
                popped = self.page.pop_dialog()
                if popped is None or isinstance(popped, ft.SnackBar):
                    try:
                        _show()
                    except Exception as exc:
                        _fallback(exc)
            except Exception as exc:
                _fallback(exc)

        self._run_on_ui(_apply)()

    def _toggle_search_tool(self) -> None:
        self.settings.search_enabled = not self.settings.search_enabled
        self.settings.save()
        state.search_enabled = self.settings.search_enabled
        state.settings_version += 1
        LOG.info("web search tool: %s", "on" if state.search_enabled else "off")

    def _open_mcp_tools(self) -> None:
        if not state.mcp_tools:
            self._notify_error("No MCP tools active. Add a server in Settings -> MCP.")
            return
        rows: list[ft.Control] = []
        for server in self.settings.mcp_servers:
            if not server.enabled:
                continue
            for full_name in state.mcp_tools:
                if not full_name.startswith(f"{server.name}."):
                    continue
                tool = full_name.split(".", 1)[1]
                active = tool not in server.disabled_tools

                def _flip(e: ft.ControlEvent, sid: str = server.id, tn: str = tool) -> None:
                    self._toggle_mcp_tool(sid, tn)

                rows.append(
                    ft.Row(
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        controls=[
                            ft.Text(full_name, size=13, selectable=True),
                            ft.Switch(value=active, on_change=_flip),
                        ],
                    )
                )
        if not rows:
            self._notify_error("MCP servers are enabled but exposed no tools.")
            return
        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("MCP tools"),
            content=ft.Container(
                width=420,
                height=320,
                content=ft.ListView(spacing=4, controls=rows),
            ),
            actions=[ft.TextButton("Close", on_click=lambda _: self.page.pop_dialog())],
        )
        self.page.show_dialog(dialog)

    def _bump_logs(self) -> None:
        """A log record arrived. Mark dirty; one flusher renders at ~2Hz.

        Called from arbitrary threads by RingHandler.emit. This never writes
        an observable directly: in Flet 1.0 any AppState write re-renders
        every use_context(AppStateCtx) component (~250ms of event-loop time
        each, measured) and a gateway start emits dozens of records back to
        back. Unthrottled, that storm saturated the loop and froze the whole
        app; Sherlock funnels the identical problem through this same 2Hz
        render budget.
        """
        self._log_dirty = True
        now = time.monotonic()
        if now - self._log_last_emit < 0.5:
            return  # a flush is already pending for this window
        self._log_last_emit = now
        loop = self._ui_loop
        if loop is None:
            # Pre-loop boot record: no components are mounted, so a direct
            # bump notifies nobody and cannot schedule a repaint.
            self._log_dirty = False
            with contextlib.suppress(RuntimeError):
                state.log_version += 1
            return
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(self._start_log_flusher)

    def _start_log_flusher(self) -> None:
        """Schedule the flusher on the UI loop (idempotent)."""
        if self._log_flush_task is not None and not self._log_flush_task.done():
            return
        self._log_flush_task = asyncio.create_task(self._log_flusher())

    async def _log_flusher(self) -> None:
        """One render per window; keeps the cadence while records keep landing."""
        try:
            await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            self._log_flush_task = None
            return
        if self._log_dirty:
            self._log_dirty = False
            state.log_version += 1
        self._log_flush_task = None
        if self._log_dirty:
            # Records landed during the window; keep the ~2Hz cadence.
            self._start_log_flusher()

    def _in_flet_ctx(self, fn: Callable) -> Callable:
        """Run fn on the Flet event loop under the captured context.

        Kept as the call-site name; the implementation is _run_on_ui (repaint
        scheduling is not thread-safe in Flet 1.0).
        """
        return self._run_on_ui(fn)

    def _on_window_event(self, event: object) -> None:
        # WindowEvent carries the event kind on `.type` (WindowEventType);
        # `.data` is None, so the old check never matched and the X button
        # looked dead while prevent_close kept the window open.
        if getattr(event, "type", None) == ft.WindowEventType.CLOSE:
            if self.settings.keep_running_when_closed:
                # Hide, keep serving. The user always has a visible Quit
                # (header + Settings), so this can never look like a crash.
                self.page.window.visible = False
                LOG.info("window hidden; gateway still running in the background")
            else:
                LOG.info("window closed; shutting the gateway down")
                self._quit_app()

    def _set_tab(self, index: int) -> None:
        state.selected_tab = index

    def _set_theme(self, mode: str) -> None:
        state.theme_mode = mode
        self.page.theme_mode = THEME_MODES.get(mode, ft.ThemeMode.SYSTEM)
        self.settings.theme = mode  # type: ignore[assignment]
        self.settings.save()

    def _start_gateway_quiet(self, *, manual: bool = False) -> None:
        def _mark_starting() -> None:
            state.gateway_starting = True

        def _mark_done() -> None:
            state.gateway_starting = False

        self._in_flet_ctx(_mark_starting)()
        try:
            if manual:
                # Sherlock shows an interstitial on a user-initiated action
                # (a search), never on automatic work. Starting the gateway by
                # hand is exactly that kind of action; boot autostart is not.
                self._show_interstitial()
            status, _port = self.services.engine.start()
            if status == "started" and manual:
                LOG.info("gateway started by user request")
            # The catalog fetch runs BEFORE the button flips back. It can
            # take seconds; marking done first made the app look idle while
            # it was still working.
            self._fetch_models(refresh=False)
        except Exception as exc:
            LOG.error("gateway start failed: %s", exc)
            self._notify_error(f"Gateway failed to start: {str(exc)[:200]}")
        finally:
            self._in_flet_ctx(_mark_done)()

    def _start_gateway(self) -> None:
        if state.gateway_starting:
            LOG.info("start ignored: already starting")
            return
        # Flip the guard synchronously on the loop: the flag used to be set
        # only after the run_thread hop, so a fast second tap raced past it.
        state.gateway_starting = True
        self.page.run_thread(self._start_gateway_quiet, manual=True)

    def _show_interstitial(self) -> None:
        ads = self.ads
        if ads is None:
            return
        try:
            self.page.run_task(ads.show_interstitial)
        except Exception as exc:
            LOG.warning("interstitial failed: %s", exc)

    def _stop_gateway(self) -> None:
        def work() -> None:
            # The share card unmounts with the gateway (gated on `running`),
            # so the session must die with it — a live tunnel would keep
            # answering 502 with no UI left to stop it, and the stale
            # share_url would resurface as "Stop sharing" on restart.
            try:
                if self._share is not None or self._tunnel is not None:
                    self._in_flet_ctx(self._stop_share)()
            except Exception as exc:
                LOG.debug("share stop on gateway stop: %s", exc)
            self.services.engine.stop()
            state.gateway_lan_url = ""
            state.gateway_lan_ip = ""

        self.page.run_thread(work)

    def _refresh_models(self) -> None:
        # refresh=true makes the engine force a fresh upstream probe server-side
        self.page.run_thread(lambda: self._fetch_models(refresh=True))

    def _fetch_models(self, refresh: bool) -> None:
        agent = self.services.agent
        if agent.portal is None:
            LOG.warning("cannot fetch models: agent portal down")
            self._notify_error("Cannot load models: agent is not running.")
            return

        async def fetch() -> list:
            suffix = "?refresh=true" if refresh else ""
            resp = await self.services.http.get(f"{state.gateway_base_url}/models{suffix}")
            if resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code}")
            payload = resp.json()
            return list(payload.get("data", []))

        async def fetch_sidecars() -> tuple[dict, dict]:
            # /account-limits carries the per-model rate hints; /status carries
            # aggregate counters. Both are best-effort: the catalog is still
            # correct without them, so a failure must never block the picker.
            limits: dict = {}
            counts: dict = {}
            root = state.gateway_base_url.rsplit("/v1", 1)[0]
            try:
                resp = await self.services.http.get(f"{root}/account-limits")
                if resp.status_code == 200:
                    limits = resp.json()
            except Exception as exc:
                LOG.debug("account-limits unavailable: %s", exc)
            try:
                resp = await self.services.http.get(f"{root}/status")
                if resp.status_code == 200:
                    counts = resp.json()
            except Exception as exc:
                LOG.debug("gateway status unavailable: %s", exc)
            return limits, counts

        try:
            data = agent.call(fetch)
        except Exception as exc:
            LOG.warning("model catalog fetch failed: %s", exc)
            self._notify_error(f"Model catalog unavailable: {str(exc)[:200]}")
            return
        if not data:
            self._notify_error(
                "Gateway returned no models. Hit 'Refresh models' on the Server tab.",
            )
            return
        try:
            limits, counts = agent.call(fetch_sidecars)
        except Exception as exc:
            LOG.debug("sidecar fetch failed: %s", exc)
            limits, counts = {}, {}
        self._run_on_ui(self._apply_models)(data, limits, counts)

    def _apply_rate_hints(self, limits: dict) -> None:
        """Fold /account-limits hints onto the catalog rows, keyed by model id."""
        hints: dict = {}
        for row in limits.get("free_models") or []:
            if not isinstance(row, dict):
                continue
            hint = row.get("rate_hint")
            if isinstance(hint, dict) and hint:
                hints[str(row.get("id") or "")] = hint
        for model in state.models:
            model_id = str(model.get("id") or "")
            if model_id in hints:
                model["rate_hint"] = hints[model_id]
        state.rate_hints = hints

    def _apply_models(
        self, data: list, limits: dict | None = None, counts: dict | None = None
    ) -> None:
        # Full, unfiltered catalog for the Server screen (honest gateway
        # mirror); selection is restricted to chat-eligible models only.
        state.models = data
        if limits:
            self._apply_rate_hints(limits)
        if counts:
            state.gateway_counts = {
                "models": counts.get("models") or {},
                "sources": counts.get("sources") or {},
                "uptime_sec": counts.get("uptime_sec") or 0,
            }
        eligible = chat_models([m for m in data if isinstance(m, dict)])
        ids = [m["id"] for m in eligible if m.get("id")]
        if state.model not in ids:
            # Prefer the router's rotating `auto` model over an arbitrary
            # first entry: it composes the healthy pool and is tool-aware.
            preferred = (
                self.settings.last_model
                if self.settings.last_model in ids
                else (AUTO_MODEL_ID if AUTO_MODEL_ID in ids else (ids[0] if ids else ""))
            )
            if preferred:
                state.model = preferred
                if self.settings.last_model != preferred:
                    self.settings.last_model = preferred
                    self.settings.save()
            else:
                state.model = ""
        if data and not ids:
            self._notify_error(
                "No active chat models. Use Refresh models on the Server tab.",
            )
        LOG.info("model catalog: %d total, %d chat-active", len(data), len(ids))

    def _set_model(self, model: str) -> None:
        if not model or model == state.model:
            return
        state.model = model
        self.settings.last_model = model
        self.settings.save()
        LOG.info("model selected: %s", model)

    def _new_conversation(self) -> None:
        if state.busy:
            # Switching conversations mid-stream orphaned the turn's message
            # index and could wedge the UI in busy (forensics R6).
            self._notify_info("Stop the current generation first, or wait for it to finish.")
            return
        state.messages = []
        state.context_used_tokens = 0
        self.services.agent.reset_conversation()
        history.start_conversation()

    def _on_page_error(self, event: object) -> None:
        text = str(getattr(event, "data", "") or event)
        LOG.error("unhandled UI error: %s", text)
        self._notify_error(f"UI error: {text[:180]}")

    def _reset_busy(self) -> None:
        state.busy = False

    def _send_failed(self, content: str, kind: str = "setup") -> None:
        state.busy = False
        state.messages.append({"role": "error", "content": content, "kind": kind})

    def _begin_turn(self, text: str) -> int | None:
        if self._stop_requested or not state.busy:
            return None
        state.messages.append({"role": "user", "content": text})
        state.messages.append({"role": "assistant", "content": ""})
        return len(state.messages) - 1

    def _finish_rejected(self) -> None:
        if state.messages:
            tail = state.messages[-1]
            if tail.get("role") == "assistant" and tail.get("content") == "":
                state.messages.pop()
        state.busy = False
        # Guard failures (gateway/model/portal) fire on_error and replace
        # the empty assistant with an error row. The busy-rejection is the
        # ONLY silent path — surface it instead of doing nothing.
        if not state.messages or state.messages[-1].get("role") != "error":
            self._notify_error(
                "Message not sent: the agent is still finishing the previous request.",
            )

    def _stop_generation(self) -> None:
        self._stop_requested = True
        self.services.agent.stop_turn()
        if not self.services.agent.busy:
            # Stop can land BEFORE the worker got as far as starting a turn
            # (e.g. during first-time tokenizer warm-up) — never leave the UI
            # locked in "busy" forever.
            state.busy = False

    def _send_message(self, text: str) -> None:
        text = (text or "").strip()
        if not text or state.busy:
            return
        if not state.gateway_running:
            state.messages.append(
                {
                    "role": "error",
                    "content": "Gateway is not running. Start it on the Server tab.",
                    "kind": "offline",
                },
            )
            return
        state.busy = True
        self._stop_requested = False
        # ALL heavy work happens OFF the Flet event thread: constructing the
        # kani engine can trigger a one-time tiktoken BPE download
        # (requests.get with NO timeout inside tiktoken) — running it inline
        # froze the whole UI with no error and no log.
        try:
            self.page.run_thread(self._send_message_worker, text)
        except Exception as exc:
            # Never leave the UI stuck in busy if dispatch itself fails.
            LOG.error("send dispatch failed: %s", exc)
            state.busy = False
            self._notify_error(f"Could not start the request: {str(exc)[:150]}")

    def _arm_turn(self) -> None:
        """Set the turn flags exactly as _send_message does before dispatch.

        _begin_turn refuses any dispatch where busy is False (or a stale
        _stop_requested survives), so retry paths that skip this land on a
        silent no-op AFTER _truncate_for_retry already destroyed the last
        exchange — the user watches their message vanish with no send.
        """
        state.busy = True
        self._stop_requested = False

    def _truncate_for_retry(self) -> str:
        """Roll the conversation back to before the last user turn — the UI
        list and kani's history together, so the two never diverge — and
        return that turn's text for a resend."""
        msgs = state.messages
        idx = max((i for i, m in enumerate(msgs) if m.get("role") == "user"), default=-1)
        if idx < 0:
            return ""
        text = str(msgs[idx].get("content") or "")
        state.messages = msgs[:idx]
        agent = self.services.agent
        history = agent.kani.chat_history if agent.kani is not None else None
        if history is not None:
            for i in range(len(history) - 1, -1, -1):
                if "user" in str(getattr(history[i], "role", "")).lower():
                    del history[i:]
                    break
        return text

    def _regenerate_last(self) -> None:
        """Re-run the last user turn: drop the previous reply and ask again."""
        if state.busy:
            self._notify_info("Stop the current generation first.")
            return
        if not state.gateway_running:
            self._notify_error("Gateway is not running. Start it on the Server tab.")
            return

        def work() -> None:
            text = self._in_flet_ctx(self._truncate_for_retry)()
            if not text.strip():
                self._notify_error("Nothing to regenerate yet.")
                return
            self._in_flet_ctx(self._arm_turn)()
            self._send_message_worker(text)

        self.page.run_thread(work)

    def _edit_last_user(self, new_text: str) -> None:
        """Edit-and-resend the last user turn with replacement text."""
        new_text = (new_text or "").strip()
        if not new_text or state.busy:
            return
        if not state.gateway_running:
            self._notify_error("Gateway is not running. Start it on the Server tab.")
            return

        def work() -> None:
            self._in_flet_ctx(self._truncate_for_retry)()
            self._in_flet_ctx(self._arm_turn)()
            self._send_message_worker(new_text)

        self.page.run_thread(work)

    def _send_message_worker(self, text: str) -> None:
        agent = self.services.agent
        # Reasoning + answer buffers. Reasoning arrives first and is kept
        # separate so the UI can collapse it when the answer starts.
        buffer = {"text": "", "thought": "", "last": 0.0, "thought_last": 0.0}

        def flush() -> None:
            try:
                state.messages[index] = {
                    "role": "assistant",
                    "content": buffer["text"],
                    "reasoning": buffer["thought"],
                }
            except (IndexError, RuntimeError) as exc:
                # Conversation was cleared/switched mid-stream (forensics R6):
                # never let this kill the portal task and wedge `busy`.
                LOG.debug("flush skipped: %s", exc)

        def on_thought(chunk: str) -> None:
            buffer["thought"] += chunk
            now = time.monotonic()
            # Reasoning is a slow trickle; 150ms keeps it visibly live without
            # rebuilding the markdown answer on every fragment.
            if now - buffer["thought_last"] >= 0.15:
                buffer["thought_last"] = now
                flush()

        def on_delta(chunk: str) -> None:
            buffer["text"] += chunk
            now = time.monotonic()
            if now - buffer["last"] >= 0.08:
                buffer["last"] = now
                flush()

        try:
            kani = agent.ensure_kani(
                state.model,
                self._system_prompt(),
                self._in_flet_ctx(on_thought),
            )
        except Exception as exc:
            LOG.error("model setup failed: %s", exc)
            self._in_flet_ctx(self._send_failed)(f"Model setup failed: {str(exc)[:200]}")
            return
        if self._stop_requested:
            self._in_flet_ctx(self._reset_busy)()
            return
        if kani is None:
            self._in_flet_ctx(self._send_failed)("No model selected yet.")
            return
        tool_err = agent.consume_tools_error()
        if tool_err:
            # A broken tool registry used to silently drop search/MCP
            # (audit D): tell the user the turn is running degraded.
            self._in_flet_ctx(self._notify_error)(
                f"Tools unavailable. Running without search/MCP ({tool_err[:100]}).",
            )
        index = self._in_flet_ctx(self._begin_turn)(text)
        if index is None:
            return

        def on_tool(name: str, content: str, is_error: bool) -> None:
            state.messages.append(
                {
                    "role": "tool",
                    "name": name,
                    "content": content[:6000],
                    "is_error": is_error,
                },
            )
            LOG.info("tool result: %s%s", name, " (error)" if is_error else "")

        def on_done(message: Any, usage: dict | None) -> None:
            final = buffer["text"] or str(getattr(message, "text", "") or "")
            # A non-streamed reply carries its reasoning on the completion's
            # message rather than in deltas; merge it in so the Thinking block
            # exists in both modes.
            completed_reasoning = final_reasoning(message)
            if completed_reasoning and not buffer["thought"]:
                buffer["thought"] = completed_reasoning
            if not final.strip():
                # Non-chat-shaped replies (systemone/response endpoints) parse
                # to None text — never show a silent empty bubble.
                LOG.warning("empty/unrecognized reply model=%s; surfacing error", state.model)
                state.messages[index] = {
                    "role": "error",
                    "content": (
                        "The model returned an empty or unrecognized response. "
                        "Pick another model (the picker lists active chat models only) "
                        "or hit Refresh models on the Server tab."
                    ),
                    "kind": "empty",
                    "reasoning": buffer["thought"],
                }
                state.busy = False
                return
            entry: dict = {"role": "assistant", "content": final}
            # Keep the reasoning the model showed: collapsed in the UI, but
            # always re-expandable afterwards.
            if buffer["thought"]:
                entry["reasoning"] = buffer["thought"]
            if usage:
                entry["usage"] = usage
                total = int(usage.get("total_tokens") or 0)
                state.context_used_tokens = total
            state.messages[index] = entry
            state.busy = False
            state.sent_count += 1
            # File IO + re-list never runs on the UI loop (this callback does).
            self.page.run_task(self._save_history_task)
            self._maybe_show_interstitial()
            LOG.info("turn done: %s tokens", (usage or {}).get("total_tokens", "?"))

        def on_error(kind: str, content: str) -> None:
            if kind == "stopped":
                flush()
                state.messages[index] = {**state.messages[index], "stopped": True}
            elif buffer["text"]:
                flush()
                state.messages.append({"role": "error", "content": content, "kind": kind})
                LOG.warning("turn error (%s): %s", kind, content)
            else:
                state.messages[index] = {"role": "error", "content": content, "kind": kind}
                LOG.warning("turn error (%s): %s", kind, content)
            state.busy = False

        try:
            started = agent.start_turn(
                text,
                self._in_flet_ctx(on_delta),
                self._in_flet_ctx(on_done),
                self._in_flet_ctx(on_error),
                on_tool=self._in_flet_ctx(on_tool),
                on_settled=self._in_flet_ctx(self._on_turn_settled),
            )
        except Exception as exc:
            # start_turn itself raising used to kill this worker silently:
            # traceback on invisible stderr, busy stuck True, send dead.
            LOG.error("turn dispatch failed: %s", exc)
            self._in_flet_ctx(self._send_failed)(
                f"Could not start the request: {str(exc)[:180]}",
            )
            return
        if not started:
            self._in_flet_ctx(self._finish_rejected)()

    def _on_turn_settled(self) -> None:
        """Last-resort busy reset: fires even when on_done/on_error explode,
        so a callback failure can never wedge the UI in 'busy' forever."""
        try:
            state.busy = False
        except Exception as exc:
            # Catch broad: only RuntimeError would leave a non-RuntimeError
            # from the context wrapper able to wedge busy (audit D).
            LOG.error("could not clear busy: %s", exc)

    async def _save_history_task(self) -> None:
        """Persist the conversation off the UI loop, then apply state on it."""
        try:
            ok = await asyncio.to_thread(history.save_conversation, self.services.agent)
            items = await asyncio.to_thread(history.list_conversations)
        except Exception as exc:
            LOG.warning("conversation save failed: %s", exc)
            self._notify_error("Conversation not saved. Check disk space. See the Activity log.")
            return
        state.conversations = items
        if not ok:
            self._notify_error("Conversation not saved. Check disk space. See the Activity log.")

    # ── sharing the gateway with someone else ───────────────────────────
    #
    # The gateway itself has no auth (the router console says api_key="any"),
    # so an optional key is enforced by our own stdlib proxy in front of it.
    # Tunneling uses the system `ssh`: no new dependency, and cloudflared is
    # far over the size budget.

    def _regenerate_share_key(self) -> None:
        self.settings.share_key = generate_key()
        self.settings.save()
        # The key field AND the Copy button read the settings_version-keyed
        # snapshot — without the bump they kept showing the OLD key while
        # the snackbar claimed a new one.
        state.settings_version += 1
        LOG.info("share key regenerated")
        self._refresh_share_key()
        self._notify_info("New key generated. Copy it to the other client.")

    # ── model test bench (console parity: Test button, verdict chips,
    # serial sweeps with live progress; see services/model_bench.py) ────

    def _apply_test_result(self, result: dict) -> None:
        model_id = str(result.get("id") or "")
        state.model_testing = state.model_testing - {model_id}
        if model_id:
            state.model_test_results = {**state.model_test_results, model_id: result}

    def _test_model(self, model_id: str) -> None:
        """One model, one probe: the verdict chip doubles as re-test."""
        if not state.gateway_running:
            self._notify_error("Gateway is not running. Start it on the Server tab.")
            return
        if state.retesting:
            self._notify_info("A retest sweep is already running.")
            return
        if model_id in state.model_testing:
            return
        state.model_testing = state.model_testing | {model_id}
        base = state.gateway_base_url
        endpoint = "chat.completion"
        for row in state.models:
            if isinstance(row, dict) and str(row.get("id") or "") == model_id:
                endpoint = str(row.get("endpoint_type") or "chat.completion")
                break
        http = self.services.http
        agent = self.services.agent

        def work() -> None:
            try:
                result = agent.call(model_bench.test_model, http, base, model_id, endpoint)
            except Exception as exc:
                LOG.warning("model test failed: %s", exc)
                result = {
                    "id": model_id,
                    "verdict": "FAIL",
                    "ms": 0,
                    "snippet": str(exc)[:120],
                }
            self._in_flet_ctx(self._apply_test_result)(result)

        self.page.run_thread(work)

    def _retest_models(self, only_not_ready: bool) -> None:
        """Serial sweep. `only_not_ready` = every row whose status is not
        "active" — the owner's "retry the untested / rate-limited" ask."""
        if not state.gateway_running:
            self._notify_error("Gateway is not running. Start it on the Server tab.")
            return
        if state.retesting:
            self._notify_info("A retest sweep is already running.")
            return
        rows = [
            row
            for row in state.models
            if isinstance(row, dict)
            and str(row.get("id") or "")
            and (not only_not_ready or str(row.get("status", "active")).lower() != "active")
        ]
        if not rows:
            self._notify_info("Nothing to retest: every model is active.")
            return
        state.retesting = True
        state.retest_progress = (0, len(rows))
        stop = threading.Event()
        self._retest_stop = stop
        base = state.gateway_base_url
        http = self.services.http
        agent = self.services.agent

        def on_row(result: dict, done: int, total: int) -> None:
            self._in_flet_ctx(self._retest_row)(result, done, total)

        def work() -> None:
            try:
                agent.call(model_bench.retest, http, base, rows, on_row, stop)
            except Exception as exc:
                LOG.warning("retest sweep failed: %s", exc)
                self._in_flet_ctx(self._notify_error)(f"Retest stopped: {str(exc)[:120]}")
            finally:
                self._in_flet_ctx(self._finish_retest)()

        self.page.run_thread(work)

    def _retest_row(self, result: dict, done: int, total: int) -> None:
        self._apply_test_result(result)
        state.retest_progress = (done, total)

    def _finish_retest(self) -> None:
        state.retesting = False
        state.retest_progress = (0, 0)
        state.model_testing = frozenset()
        self._retest_stop = None

    def _stop_retest(self) -> None:
        if self._retest_stop is not None:
            self._retest_stop.set()
        self._notify_info("Stopping the retest sweep after the current model…")

    def _refresh_share_key(self) -> None:
        """Apply share-key settings to the LIVE proxy (owner decision: if the
        toggle is touched at all while sharing, restart the proxy afresh and
        show the new details). The rebind keeps the same port, so the tunnel
        and public URL never move."""
        session = self._share
        if session is None:
            return
        # Same derivation as _start_share, including auto-generation.
        if self.settings.require_share_key and not self.settings.share_key:
            self.settings.share_key = generate_key()
            self.settings.save()
            state.settings_version += 1
        key = self.settings.share_key if self.settings.require_share_key else ""
        if not session.rekey(key):
            LOG.warning("share proxy rebind failed")
            self._stop_share()
            self._set_share_error("Sharing stopped: could not apply the new key settings.")
            return
        LOG.info("share proxy re-keyed (auth=%s)", bool(key))
        self._notify_info(
            "Sharing restarted with your new key settings. The URL is unchanged.",
        )

    def _start_share(self) -> None:
        if not state.gateway_running:
            self._notify_error("Gateway is not running. Start it on the Server tab.")
            return
        if self._share is not None:
            self._stop_share()
            return
        # The tunnel claim retries for up to 30s. Without this guard a second
        # click in that window starts a SECOND proxy and the owner sees two
        # log lines and one seemingly dead button.
        if state.share_starting:
            LOG.warning("share start ignored: already in flight")
            # A log line nobody reads is the whole "sharing never started"
            # complaint: say it on screen too.
            self._notify_info("Sharing is already starting.")
            return

        key = self.settings.share_key if self.settings.require_share_key else ""
        if self.settings.require_share_key and not key:
            self.settings.share_key = key = generate_key()
            self.settings.save()
            state.settings_version += 1  # same snapshot rule as regenerate

        def _mark_starting() -> None:
            state.share_starting = True
            state.share_error = ""

        def _mark_done() -> None:
            state.share_starting = False

        self._in_flet_ctx(_mark_starting)()

        def work() -> None:
            # Every exit clears the flag. The unguarded version could raise
            # anywhere (proxy bind, tunnel claim, state write), the thread
            # died, and Start sharing stayed disabled with no signal at all.
            try:
                port = self.services.engine.port or state.gateway_port
                session = ShareSession(port, key=key)
                if not session.start_proxy():
                    self._in_flet_ctx(self._set_share_error)(
                        "Share proxy failed to bind. Another app may hold the port."
                    )
                    return

                def _tunnel_failed(reason: str) -> None:
                    # LocalTunnel calls this from its pump thread, so it must
                    # be marshaled like any other worker write. Without it a
                    # dead relay left its URL on screen looking alive.
                    LOG.warning("share tunnel error: %s", reason)

                    def _apply() -> None:
                        self._share_url = ""
                        state.share_url = ""
                        state.share_error = f"Tunnel closed: {reason}"

                    self._in_flet_ctx(_apply)()

                # localtunnel protocol, pure stdlib: works on a phone, where
                # there is no ssh client. The old ssh -R path is gone, not a
                # fallback. on_error was accepted but never supplied, so a
                # dropped relay produced nothing but a log line.
                tunnel = LocalTunnel(session.proxy_port, on_error=_tunnel_failed)
                try:
                    url = tunnel.start()
                except Exception as exc:
                    session.stop()
                    self._in_flet_ctx(self._set_share_error)(
                        f"Could not open a tunnel: {str(exc)[:160]}"
                    )
                    return

                def done() -> None:
                    # done() runs on the Flet loop from inside a worker thread.
                    # Anything raised here used to die with the thread, so the
                    # button read as dead and the log card stayed empty.
                    try:
                        self._share = session
                        self._tunnel = tunnel
                        self._share_url = url
                        state.share_url = url
                        state.share_error = ""
                        state.share_starting = False
                        LOG.info("sharing gateway at %s (auth=%s)", url, bool(key))
                    except Exception as exc:
                        LOG.error("share state update failed: %s", exc, exc_info=True)
                        tunnel.stop()
                        session.stop()
                        self._share = None
                        self._tunnel = None
                        self._share_url = ""
                        state.share_url = ""
                        state.share_starting = False
                        self._notify_error(
                            f"Sharing started but the UI did not update: {str(exc)[:160]}"
                        )

                self._in_flet_ctx(done)()
            except Exception as exc:
                LOG.error("share worker failed: %s", exc, exc_info=True)
                with contextlib.suppress(Exception):
                    self._in_flet_ctx(self._set_share_error)(f"Sharing failed: {str(exc)[:160]}")
            finally:
                self._in_flet_ctx(_mark_done)()

        try:
            self.page.run_thread(work)
        except Exception as exc:
            # The worker never ran, so its finally never runs either. The flag
            # has to be cleared here or Start sharing stays dead.
            LOG.error("share dispatch failed: %s", exc, exc_info=True)
            with contextlib.suppress(Exception):
                self._in_flet_ctx(self._set_share_error)(f"Sharing failed: {str(exc)[:160]}")
                self._in_flet_ctx(_mark_done)()

    def _stop_share(self) -> None:
        session, self._share = self._share, None
        tunnel, self._tunnel = self._tunnel, None
        self._share_url = ""
        state.share_url = ""
        state.share_starting = False
        state.share_error = ""  # a cleared session must not keep a red banner
        if tunnel is not None:
            tunnel.stop()
        if session is not None:
            self.page.run_thread(session.stop)
        LOG.info("sharing stopped")

    def _set_share_error(self, message: str) -> None:
        state.share_error = message
        self._notify_error(message)

    def _open_console(self) -> None:
        url = f"http://127.0.0.1:{self.services.engine.port}/"
        launcher = self._url_launcher
        if launcher is None:
            LOG.warning("cannot open console: url launcher missing")
            self._notify_error("Cannot open console: URL launcher unavailable.")
            return
        try:
            # launch_url is a coroutine: run_task awaits it on the Flet loop,
            # run_thread would build an un-awaited coroutine and do nothing.
            self.page.run_task(launcher.launch_url, url)
        except Exception as exc:
            LOG.warning("open console failed: %s", exc)
            self._notify_error(f"Cannot open console: {str(exc)[:150]}")

    def _extra_tools(self) -> tuple[list, object]:
        """(tools, generation) for AgentService; rebuilds kani on change."""
        tools: list = []
        if self.settings.tell_model_time:
            # Pair with the system-prompt clock line: the line handles routine
            # "what's today" questions, the tool covers exact readings.
            tools.append(self._time_tool)
        if self.settings.search_enabled and self._search_tool is not None:
            tools.append(self._search_tool)
        tools.extend(self.services.mcp.tools)
        return tools, (
            f"{self.services.mcp.generation}:{self.settings.search_enabled}"
            f":{self.settings.tell_model_time}"
        )

    def _system_prompt(self) -> str:
        """The stored prompt, plus a clock line when the user opted in."""
        return with_clock(self.settings.system_prompt, self.settings.tell_model_time)

    def _ensure_mcp_owner(self) -> None:
        """Spawn the MCP owner task unless one is already running.

        Idempotent; called at boot, from every MCP settings mutation (via
        _reapply_mcp) and after every portal restart (via the agent's
        on_portal_restart callback). The old future reports done() once its
        portal died, which is exactly when a respawn is required.
        """
        future = self._mcp_future
        if future is not None and not future.done():
            return
        try:
            self._mcp_future = self.services.agent.spawn(self.services.mcp.serve)
        except Exception as exc:
            LOG.warning("mcp owner task not started: %s", exc)
            self._notify_error(f"MCP subsystem unavailable: {str(exc)[:150]}")

    def _reapply_mcp(self) -> None:
        """Signal the MCP owner task (thread-safe, instant).

        The owner task owns the session context for the app's lifetime: a
        per-call apply/close from different portal tasks poisons the anyio
        task group ("cancel scope in a different task") and permanently
        bricks the agent portal — chat then never runs again (boot-diff
        reproduction: an enabled unreachable server killed every send).
        A live owner task is the precondition for the signal to mean
        anything, so ensure it first.
        """
        self._ensure_mcp_owner()
        self.services.mcp.request_reconnect()

    def _mcp_status(self, names: list[str] | None, error: str | None) -> None:
        """Owner-task callback → UI loop (observable writes are loop-bound)."""

        def _apply() -> None:
            state.mcp_tools = names or []
            if names:
                LOG.info("mcp tools active: %d", len(names))
            if error:
                self._notify_error(error)

        self._run_on_ui(_apply)()

    def _add_mcp_server(self, data: dict) -> None:
        name = str(data.get("name", "")).strip()
        if any(s.name == name for s in self.settings.mcp_servers):
            # Names prefix tool ids (server.tool) — duplicates corrupt the
            # per-tool disable mapping.
            self._notify_error(
                f"MCP server '{name}' already exists.",
            )
            return
        try:
            self.settings.mcp_servers.append(MCPServerConfig(**data))
        except ValidationError as exc:
            msg = self._format_validation_error(exc)
            LOG.warning("invalid mcp server: %s", msg)
            self._notify_error(f"MCP server rejected: {msg}")
            return
        except Exception as exc:
            LOG.warning("invalid mcp server: %s", exc)
            self._notify_error(f"MCP server rejected: {str(exc)[:200]}")
            return
        self.settings.save()
        # The Settings MCP list re-reads only on this snapshot bump — without
        # it, add/remove/toggle saved to disk but the list stayed stale until
        # a tab switch remounted the screen.
        state.settings_version += 1
        self._reapply_mcp()

    def _remove_mcp_server(self, server_id: str) -> None:
        self.settings.mcp_servers = [s for s in self.settings.mcp_servers if s.id != server_id]
        self.settings.save()
        # The Settings MCP list re-reads only on this snapshot bump — without
        # it, add/remove/toggle saved to disk but the list stayed stale until
        # a tab switch remounted the screen.
        state.settings_version += 1
        self._reapply_mcp()

    def _toggle_mcp_server(self, server_id: str) -> None:
        for server in self.settings.mcp_servers:
            if server.id == server_id:
                server.enabled = not server.enabled
        self.settings.save()
        # The Settings MCP list re-reads only on this snapshot bump — without
        # it, add/remove/toggle saved to disk but the list stayed stale until
        # a tab switch remounted the screen.
        state.settings_version += 1
        self._reapply_mcp()

    def _toggle_mcp_tool(self, server_id: str, tool_name: str) -> None:
        for server in self.settings.mcp_servers:
            if server.id == server_id:
                if tool_name in server.disabled_tools:
                    server.disabled_tools.remove(tool_name)
                else:
                    server.disabled_tools.append(tool_name)
        self.settings.save()
        # The Settings MCP list re-reads only on this snapshot bump — without
        # it, add/remove/toggle saved to disk but the list stayed stale until
        # a tab switch remounted the screen.
        state.settings_version += 1
        self._reapply_mcp()

    def _test_mcp_server(self, server_id: str, done_cb) -> None:
        hub = self.services.mcp
        agent = self.services.agent
        target = next((s for s in self.settings.mcp_servers if s.id == server_id), None)
        if target is None:
            done_cb(("err", "Server not found."))
            return

        def work() -> None:
            async def probe() -> list:
                # Bounded: MCP's own read timeout is 300s — a stalled server
                # must never look like a dead button (settings audit: 75s+
                # hangs with no done_cb). 20s cap on the portal loop.
                return await asyncio.wait_for(hub.test(target), timeout=20.0)

            def deliver(result: tuple) -> None:
                # done() writes observables — must run in the Flet context or
                # the spinner can stick forever (forensics R5).
                self._in_flet_ctx(done_cb)(result)

            try:
                names = agent.call(probe)
            except TimeoutError:
                deliver(("err", "Timed out after 20s. Check the URL or command."))
                return
            except Exception as exc:
                LOG.warning("mcp test failed: %s", exc)
                deliver(("err", str(exc)[:300] or exc.__class__.__name__))
                return
            except BaseException as exc:
                # CancelledError et al pass through `except Exception` — still
                # deliver a result so the button never stays stuck, then re-raise.
                deliver(("err", f"Test aborted ({exc.__class__.__name__})."))
                raise
            deliver(("ok", names))

        self.page.run_thread(work)

    @staticmethod
    def _format_validation_error(exc: ValidationError) -> str:
        errors = exc.errors()
        if not errors:
            return str(exc)
        err = errors[0]
        loc = ".".join(str(p) for p in err.get("loc", []))
        msg = err.get("msg", "invalid")
        return f"Field '{loc}': {msg}"

    def _save_settings(self, data: dict) -> None:
        allowed = {
            "theme",
            "gateway_port",
            "gateway_autostart",
            "keep_running_when_closed",
            "share_enabled",
            "require_share_key",
            "share_key",
            "system_prompt",
            "search_enabled",
            "interstitial_every",
            "update_check",
            "max_context_tokens",
            # Generation controls -> kani/OpenAI per-turn parameters.
            "temperature",
            "top_p",
            "max_reply_tokens",
            "presence_penalty",
            "frequency_penalty",
            "reasoning_effort",
            "json_mode",
            "tool_max_rounds",
            "tool_retry_attempts",
            "tell_model_time",
        }
        updates = {k: v for k, v in data.items() if k in allowed}
        if not updates:
            return
        previous = {k: getattr(self.settings, k) for k in updates}
        try:
            for key, value in updates.items():
                setattr(self.settings, key, value)
        except ValidationError as exc:
            msg = self._format_validation_error(exc)
            LOG.warning("settings validation error (%s); reverting", msg)
            self._notify_error(f"Settings not saved: {msg}")
            for key, value in previous.items():
                try:
                    setattr(self.settings, key, value)
                except Exception as revert_exc:
                    LOG.debug("revert %s failed: %s", key, revert_exc)
            return
        except Exception as exc:
            LOG.warning("settings value rejected (%s); reverting", exc)
            self._notify_error(f"Settings not saved: {str(exc)[:200]}")
            for key, value in previous.items():
                try:
                    setattr(self.settings, key, value)
                except Exception as revert_exc:
                    LOG.debug("revert %s failed: %s", key, revert_exc)
            return
        self.settings.save()
        state.settings_version += 1
        if "search_enabled" in updates:
            # The chat pill reads the observable, not the settings model —
            # without this the Settings switch and the pill disagreed until
            # restart (the chat-side toggle writes both).
            state.search_enabled = self.settings.search_enabled
        if self._share is not None and {"require_share_key", "share_key"} & set(updates):
            # The proxy captured its key at bind time; rebind live (owner
            # decision: touching the toggle/key mid-share restarts sharing
            # and the card immediately shows the new details).
            self._refresh_share_key()

    def _add_provider(self, data: dict) -> None:
        name = str(data.get("name", "")).strip()
        url = str(data.get("base_url", "")).strip().rstrip("/")
        if any(
            p.name == name and str(p.base_url).rstrip("/") == url for p in self.settings.providers
        ):
            self._notify_error(f"Provider '{name}' with this URL already exists.")
            return
        try:
            provider = ProviderConfig(**data)
        except ValidationError as exc:
            msg = self._format_validation_error(exc)
            LOG.warning("invalid provider: %s", msg)
            self._notify_error(f"Provider rejected: {msg}")
            return
        except Exception as exc:
            LOG.warning("invalid provider: %s", exc)
            self._notify_error(f"Provider rejected: {str(exc)[:200]}")
            return
        self.settings.providers.append(provider)
        self.settings.save()
        state.settings_version += 1

    def _remove_provider(self, provider_id: str) -> None:
        self.settings.providers = [p for p in self.settings.providers if p.id != provider_id]
        if self.settings.active_provider_id == provider_id:
            self.settings.active_provider_id = ""
        self.settings.save()
        state.settings_version += 1

    def _select_provider(self, provider_id: str) -> None:
        if not any(p.id == provider_id for p in self.settings.providers):
            return
        self.settings.active_provider_id = provider_id
        self.settings.save()
        state.settings_version += 1
        LOG.info("active provider: %s", provider_id)

    async def _boot_conversations(self) -> None:
        """Load the conversation list off the UI thread (runs on the loop).

        Disk work during init delays the app appearing; state lands directly
        because run_task coroutines already execute on the Flet loop.
        """
        try:
            items = await asyncio.to_thread(history.list_conversations)
        except Exception as exc:
            LOG.warning("conversation list failed: %s", exc)
            return
        state.conversations = items

    def _clear_history(self) -> None:
        if state.busy:
            # The in-flight turn would rewrite the file just cleared —
            # refuse with a reason instead of racing it (DDGS _busy_refuse).
            self._notify_info("Stop the current generation before clearing the history.")
            return

        # File IO + full re-list/parse (O(n x size)) runs on a worker, never
        # the UI thread (forensics R4); state lands via the Flet context.
        def work() -> None:
            failures = history.clear_all()
            items = history.list_conversations()
            self._in_flet_ctx(self._apply_history_change)(items, failures, start_new=True)

        self.page.run_thread(work)

    def _open_conversation(self, conversation_id: str) -> None:
        if state.busy:
            self._notify_info("Stop the current generation before opening a conversation.")
            return

        def work() -> None:
            agent = self.services.agent
            try:
                kani = agent.ensure_kani(state.model, self._system_prompt())
            except Exception as exc:
                LOG.error("conversation open setup failed: %s", exc)
                self._in_flet_ctx(self._notify_error)(f"Cannot open conversation: {str(exc)[:180]}")
                return
            if kani is None:
                self._in_flet_ctx(self._notify_error)(
                    "Cannot open conversation: no model selected yet.",
                )
                return
            try:
                ok = history.load_conversation(agent, conversation_id, apply_state=False)
            except Exception as exc:
                LOG.error("conversation load failed: %s", exc)
                self._in_flet_ctx(self._notify_error)(
                    f"Could not load conversation: {str(exc)[:180]}"
                )
                return
            self._in_flet_ctx(self._conversation_loaded)(ok, agent, conversation_id)

        self.page.run_thread(work)

    def _conversation_loaded(self, ok: bool, agent: Any, conversation_id: str) -> None:
        if not ok:
            self._notify_error("Could not load conversation. See the Activity log.")
            return
        state.active_conversation = conversation_id
        state.messages = history.messages_from_history(agent)
        self._set_tab(0)

    def _delete_conversation(self, conversation_id: str) -> None:
        was_active = conversation_id == state.active_conversation
        if was_active and state.busy:
            # The running turn writes into this id when it finishes; deleting
            # the ACTIVE chat mid-stream is the race DDGS guards exactly.
            # Deleting a DIFFERENT chat while streaming is safe.
            self._notify_info("Stop the current generation before deleting this conversation.")
            return

        def work() -> None:
            ok = history.delete_conversation(conversation_id)
            items = history.list_conversations()
            # Deleting the conversation we are IN rotates to a fresh one —
            # otherwise active_conversation keeps pointing at a deleted file
            # and the next send recreates it (DDGS keep_current=False).
            self._in_flet_ctx(self._apply_history_change)(
                items,
                0 if ok else 1,
                start_new=was_active,
            )

        self.page.run_thread(work)

    def _apply_history_change(
        self,
        items: list,
        failures: int = 0,
        start_new: bool = False,
    ) -> None:
        state.conversations = items
        if failures:
            self._notify_error(
                f"Could not delete {failures} conversation file(s). See the Activity log.",
            )
        if start_new:
            self._new_conversation()

    def _copy_text(self, text: str) -> None:
        try:
            clipboard = ft.Clipboard()
            self.page.services.append(clipboard)

            async def _copy() -> None:
                await clipboard.set(text)
                self.page.show_dialog(
                    ft.SnackBar(
                        content=ft.Text("Copied to clipboard"),
                        behavior=ft.SnackBarBehavior.FLOATING,
                        duration=1500,
                    ),
                )

            self.page.run_task(_copy)
        except Exception as exc:
            LOG.warning("clipboard copy failed: %s", exc)
            self._notify_error(f"Copy failed: {str(exc)[:150]}")

    def _copy_logs(self) -> None:
        """Copy the rolling log file — the terminal is not always visible."""
        self._copy_text(log_tail())

    def _export_conversation(self, conversation_id: str) -> None:
        # File read + full JSON parse runs on a worker, never on the UI
        # thread (same rule as delete/open); the save dialog then follows
        # from the Flet context once the content is ready.
        def work() -> None:
            content, filename = history.export_conversation_markdown(conversation_id)
            if not content:
                self._in_flet_ctx(self._notify_error)(
                    "Export failed. Conversation unreadable.",
                )
                return
            self._in_flet_ctx(self._start_export_write)(
                content,
                filename,
                conversation_id,
            )

        self.page.run_thread(work)

    def _start_export_write(self, content: str, filename: str, conversation_id: str) -> None:
        async def _write() -> None:
            path = await save_text_file(
                self.page,
                content,
                filename,
                dialog_title=f"Export {filename}",
            )
            if path:
                LOG.info("exported conversation %s to %s", conversation_id, path)
                self._notify_info(f"Saved to {path}")
            else:
                self._notify_error("Could not save the file. Check that the folder is writable.")

        self.page.run_task(_write)

    async def _check_update(self, silent: bool = False) -> None:
        try:
            # Manual check surfaces network failure; boot check stays quiet.
            data = await UpdateService.check_for_updates(raise_on_error=not silent)
        except Exception as exc:
            self._notify_error(f"Update check failed: {str(exc)[:150]}")
            return
        if not data:
            if not silent:
                self._notify_info(f"Up to date. Build {constants.BUILD_NUMBER}.")
            return
        state.update_info = data
        if not silent:
            self._open_update_dialog()

    def _open_log_terminal(self) -> None:
        """Live log in its own dialog: readable, copyable, and it stops the
        nested scrollable from eating the page's scroll gestures."""
        try:
            self.page.show_dialog(build_log_terminal(self.page, state, self.methods))
        except Exception as exc:
            LOG.warning("log terminal failed: %s", exc)
            self._notify_error(f"Could not open the log: {str(exc)[:120]}")

    def _open_about(self) -> None:
        """Version chip: show what this build is and where it came from."""
        try:
            self.page.show_dialog(build_about_dialog(self.page, state, self.methods))
        except Exception as exc:
            LOG.warning("about dialog failed: %s", exc)
            self._notify_error(f"Could not open app details: {str(exc)[:120]}")

    def _open_update_dialog(self) -> None:
        if not state.update_info:
            LOG.info("no update to show")
            return
        dialog = build_update_dialog(
            self.page,
            state.update_info,
            self._url_launcher,
            on_close=self._dismiss_update,
        )
        self.page.show_dialog(dialog)

    def _dismiss_update(self) -> None:
        try:
            self.page.pop_dialog()
        except Exception as exc:
            LOG.debug("dismiss update: %s", exc)
        state.update_info = None

    async def _boot_ads(self) -> None:
        if self.ads is None:
            return
        await self.ads.gather_consent()
        await self.ads.preload_interstitial()

    def _maybe_show_interstitial(self) -> None:
        every = self.settings.interstitial_every
        ads = self.ads
        if ads is None or every < 1:
            return
        if state.sent_count and state.sent_count % every == 0:
            self.page.run_task(ads.show_interstitial)

    def _finish_onboarding(self) -> None:
        self.settings.onboarding_done = True
        self.settings.terms_accepted = True
        self.settings.save()
        state.onboarding_done = True
        state.terms_accepted = True
        state.selected_tab = 0  # land on Chat (homepage) after onboarding
        # Explicit update: observable flips alone were not swapping the gate
        # in this app; ffmpeg's working pattern pairs the flip with update().
        # Retry once: the first update can race the just-finished render.
        for attempt in (1, 2):
            try:
                self.page.update()
                break
            except Exception as exc:
                LOG.warning("post-onboarding update failed (attempt %d): %s", attempt, exc)
        LOG.info("onboarding complete")

    def _open_url(self, url: str) -> None:
        launcher = self._url_launcher
        if launcher is None:
            LOG.warning("cannot open %s: url launcher missing", url)
            self._notify_error("Cannot open link: URL launcher unavailable.")
            return
        try:
            self.page.run_task(launcher.launch_url, url)
        except Exception as exc:
            LOG.warning("open url failed: %s", exc)
            self._notify_error(f"Cannot open link: {str(exc)[:150]}")

    # --- lifecycle / system-back -------------------------------------------

    def _android_activity(self) -> object | None:
        """The running Android activity (KTV's proven fallback chain)."""
        import os

        try:
            from jnius import autoclass
        except Exception:
            return None
        for cls_name in (
            os.getenv("MAIN_ACTIVITY_HOST_CLASS_NAME"),
            "ng.kiri.lmrouter.MainActivity",
            "net.flet.MainActivity",
            "com.flet.flet_android.MainActivity",
            "org.kivy.android.PythonActivity",
        ):
            if not cls_name:
                continue
            with contextlib.suppress(Exception):
                host = autoclass(cls_name)
                activity = getattr(host, "mActivity", None) or getattr(
                    host, "mCurrentActivity", None
                )
                if activity is not None:
                    return activity
        return None

    def _ensure_back_underlay(self) -> None:
        """Put a view beneath the shell so Android back reaches Python.

        flet's Dart system-back handler bails when the top view is the only
        one (`page.dart` `_handleSystemPopRoute`: `views.length <= 1` →
        returns null) — the framework finishes the activity WITHOUT emitting
        `view_pop`, so no handler could save state, stop the gateway, or
        navigate. Same underlay KTV installs for the identical reason.
        """
        try:
            views = self.page.views
            if not views:
                return
            if any(getattr(v, "route", None) == "/blank" for v in views):
                return
            views.insert(
                0,
                ft.View(route="/blank", bgcolor=ft.Colors.BLACK, padding=0),
            )
            self.page.update()
            LOG.info("back underlay installed beneath the shell")
        except Exception as exc:
            LOG.debug("back underlay install failed: %s", exc)

    def _restore_shell(self, popped: ft.View | None) -> None:
        """Re-show the shell the framework already hid.

        On system back, `page.dart` marks the popped route in
        `_pendingPoppedViewRoutes` and filters it out of the Navigator BEFORE
        Python ever sees the event; the pending mark clears only when the
        route leaves the Python tree. A route re-key is the single-patch
        restore: old route vanishes → pending clears → the same View shows
        again. Cost: the Navigator re-keys the page, so per-component hook
        state (composer draft) remounts — global AppState survives.
        """
        views = self.page.views
        target = (
            popped
            if popped is not None and any(v is popped for v in views)
            else (views[-1] if views else None)
        )
        if target is None or getattr(target, "route", None) == "/blank":
            return
        self._back_seq += 1
        target.route = f"/back-{self._back_seq}"
        try:
            self.page.update()
        except Exception as exc:
            LOG.info("shell restore failed: %s", exc)

    def _background_app(self) -> None:
        """Hide without dying, keeping the gateway serving — the desktop
        window.on_event path does the same thing via window.visible."""
        try:
            is_desktop = bool(self.page.platform.is_desktop())
        except Exception as exc:
            LOG.debug("platform probe failed: %s", exc)
            is_desktop = False
        if is_desktop:
            try:
                self.page.window.visible = False
                return
            except Exception as exc:
                LOG.info("window hide failed: %s", exc)
        try:
            activity = self._android_activity()
            if activity is not None:
                activity.moveTaskToBack(True)
                return
            LOG.info("no android activity to background; shell restored only")
        except Exception as exc:
            LOG.info("android background failed: %s", exc)

    def _on_view_pop(self, e: ft.ViewPopEvent) -> None:
        """Owner rule (Sherlock): system back must never tear down the shell
        — it maps to in-app navigation. At the Chat root the owner chose
        desktop parity: keep-running ON → background, OFF → full teardown."""
        try:
            popped = getattr(e, "view", None)
            if state.selected_tab != 0:
                state.selected_tab = 0
                self._restore_shell(popped)
                return
            if self.settings.keep_running_when_closed:
                self._restore_shell(popped)
                self._background_app()
            else:
                self._quit_app()
        except Exception as exc:
            LOG.warning("view_pop handling failed: %s", exc)

    async def _on_lifecycle(self, e: ft.AppLifecycleStateChangeEvent) -> None:
        """DDGS shape: flush when backgrounded, re-probe when foregrounded.

        Flet 1.0 exits without running atexit/buffered writes, and the OS can
        drop the connection while we're backgrounded (Sherlock). Compare the
        enum member — `e.data` is always None on a dataclass payload, which
        is why KTV's string comparison never fires.
        """
        try:
            if e.state in (
                ft.AppLifecycleState.HIDE,
                ft.AppLifecycleState.PAUSE,
                ft.AppLifecycleState.DETACH,
            ):
                try:
                    self.settings.save()
                except Exception as exc:
                    LOG.warning("lifecycle settings flush failed: %s", exc)
                return
            if e.state in (ft.AppLifecycleState.RESUME, ft.AppLifecycleState.SHOW):
                await recheck_connectivity(self.page)
        except Exception as exc:
            LOG.info("lifecycle handling failed: %s", exc)

    def _on_disconnect(self, e: object = None) -> None:
        # Flet 1.0 exits without running atexit/buffered writes — flush
        # synchronously: the loop is already closing, so page.run_task would
        # leave the coroutine un-awaited (DDGS's rationale, verbatim).
        try:
            self.settings.save()
        except Exception as exc:
            LOG.debug("disconnect flush failed: %s", exc)

    # --- teardown ----------------------------------------------------------

    def _quit_app(self) -> None:
        # Idempotent: on_close, the Quit button and the back-at-root path can
        # race. Gateway shutdown joins up to ~7s — keep it off the UI thread
        # (forensics R8); window teardown runs in the Flet context. Ordered so
        # every resource dies before the loop that owns it.
        if self._quitting:
            return
        self._quitting = True

        def work() -> None:
            try:
                self.services.mcp.request_stop()
            except Exception as exc:
                LOG.debug("mcp stop signal: %s", exc)
            # Bounded join: the owner task's finally closes every MCP context
            # (and reaps stdio children) — give it a window before the portal
            # that owns those contexts dies.
            future = self._mcp_future
            if future is not None:
                try:
                    future.result(timeout=5)
                except Exception as exc:
                    LOG.debug("mcp owner join: %s", exc)
            # httpx client is portal-loop-bound; close it ON that loop before
            # stopping the portal (aclose on a dead portal would raise).
            try:
                self.services.agent.call(self.services.http.aclose)
            except Exception as exc:
                LOG.debug("http client close: %s", exc)
            try:
                self.services.agent.stop()
            except Exception as exc:
                LOG.warning("agent shutdown: %s", exc)
            try:
                self.services.engine.stop()
            except Exception as exc:
                LOG.warning("gateway shutdown: %s", exc)
            try:
                # Share proxy + tunnel outlived quit: daemon threads die with
                # the process, but the session must be marked stopped first.
                self._stop_share()
            except Exception as exc:
                LOG.debug("share shutdown: %s", exc)
            self._in_flet_ctx(self._finish_quit)()

        self.page.run_thread(work)

    def _finish_quit(self) -> None:
        """Flet-context step: release the ads, THEN destroy the window — one
        run_task chain so window teardown can never race the ad release."""

        async def _close_then_destroy() -> None:
            ads = self.ads
            if ads is not None:
                try:
                    await ads.close()
                except Exception as exc:
                    LOG.debug("ad close: %s", exc)
            self._destroy_window()

        try:
            self.page.run_task(_close_then_destroy)
        except Exception as exc:
            LOG.warning("quit failed: %s", exc)

    def _destroy_window(self) -> None:
        # Re-enable the close guard first so destroy is never intercepted.
        self.page.window.prevent_close = False
        # Android: window.close/destroy are Dart-guarded to desktop (KTV's
        # documented finding), so finishing the activity directly is the only
        # real exit there.
        try:
            if self.page.platform.is_mobile():
                activity = self._android_activity()
                if activity is not None:
                    activity.finish()
                    return
                LOG.warning("android exit: no activity handle; trying window.destroy")
        except Exception as exc:
            LOG.warning("android finish failed: %s", exc)
        try:
            # Window.destroy is a coroutine in Flet 1.0: calling it bare
            # returns an un-awaited coroutine and the window never closes.
            self.page.run_task(self.page.window.destroy)
        except Exception as exc:
            LOG.warning("quit failed: %s", exc)


def main(page: ft.Page) -> None:
    controller = AppController(page)
    controller.init()
    services = controller.services
    methods = controller.methods
    page.render(
        lambda: ServiceCtx(services, lambda: ControllerMethodsCtx(methods, lambda: AppShell())),
    )
    # Must come AFTER render: it needs views[0] to exist to insert beneath.
    controller._ensure_back_underlay()


if __name__ == "__main__":
    # Flet resolves a relative assets_dir against THIS SCRIPT's directory, so
    # the fallback must be "assets" — "src/assets" resolved to src/src/assets
    # and silently dropped the icon and fonts.
    ft.run(main, assets_dir=os.environ.get("FLET_ASSETS_DIR") or "assets")
