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
from core.settings import AppSettings, ValidationError, pop_load_warnings
from core.state import state
from services import model_bench
from services.ad_service import AdService
from services.async_runner import AsyncRunner
from services.engine import EngineService
from services.http import HttpService
from services.share import ShareSession, generate_key
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
        self._clipboard: ft.Clipboard | None = None
        self.ads: AdService | None = None
        self._async = AsyncRunner()
        self._ui_loop: asyncio.AbstractEventLoop | None = None
        # 2Hz log-render budget (Sherlock's flusher pattern): log records
        # mark dirty; ONE task turns bursts into at most two renders/second.
        self._log_dirty = False
        self._log_last_emit = 0.0
        self._log_flush_task: asyncio.Task | None = None
        self._share: ShareSession | None = None
        self._tunnel: LocalTunnel | None = None
        self._share_url = ""
        self._quitting = False  # run-once guard: on_close + Quit can race
        self._retest_stop: threading.Event | None = None
        self._retest_gen = 0  # sweep generation: stale finally must not clear flags
        self._back_seq = 0  # route re-key counter for view_pop restores
        self._notify: Any = None  # ServerNotificationService; None until init()
        # flet page context captured at construction: worker threads (engine
        # watcher, share tunnel) marshal state writes onto the UI loop UNDER
        # THIS context — a context copied on the worker thread lacks flet's
        # page binding and every marshaled write raises (the "UI callback
        # failed: _apply" spam, 2026-11-09).
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
        # Remembered model before any catalog arrives: the test screen keeps
        # a usable selection even if the first /models fetch fails.
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

        # The X button asks before quitting (see _on_window_event); on mobile
        # and web the session's on_close is the only exit signal, handled below.
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
        # window.on_event hook above is desktop-gated. on_close fires for the
        # session/console close paths AND when the framework closes the page
        # itself, so it asks before quitting exactly like the X button; with
        # prevent_close=True the window's own close never reaches here.
        # Both handlers are idempotent.
        try:
            page.on_view_pop = self._on_view_pop
            page.on_app_lifecycle_state_change = self._on_lifecycle
            page.on_close = self._on_page_close
            page.on_disconnect = self._on_disconnect
        except Exception as exc:
            LOG.info("lifecycle hooks unavailable: %s", exc)

        # services
        http = HttpService()
        engine = EngineService(settings, on_ui=self._run_on_ui)
        self.services.settings = settings
        self.services.http = http
        self.services.engine = engine
        try:
            self._url_launcher = ft.UrlLauncher()
            page.services.append(self._url_launcher)
        except Exception as exc:
            LOG.info("url launcher unavailable: %s", exc)
        try:
            page.services.append(ft.HapticFeedback())
        except Exception as exc:
            LOG.info("haptics unavailable: %s", exc)

        # Foreground service + wake lock: keeps the gateway alive when the app
        # is minimised on Android. Must mount AFTER services.append calls above
        # so the page.services list is already initialised.
        from services.server_notify import ServerNotificationService

        self._notify = ServerNotificationService(on_stop=self._stop_gateway)
        self._notify.mount(page)
        page.run_task(self._notify.request_permission)

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

        # controllers wired for this step
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
        m.quit_app = self._confirm_quit
        m.set_model = self._set_model
        m.finish_onboarding = self._finish_onboarding
        m.open_url = self._open_url
        m.save_settings = self._save_settings
        m.copy_text = self._copy_text
        m.copy_logs = self._copy_logs
        m.dismiss_notice = self._dismiss_notice
        m.check_update = lambda: self.page.run_task(self._check_update)
        m.open_update_dialog = self._open_update_dialog
        m.open_about = self._open_about
        m.open_ad_privacy_options = self._open_ad_privacy_options
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
                LOG.error(
                    "UI callback %s: %s",
                    why,
                    getattr(fn, "__name__", fn),
                    exc_info=True,
                )
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
        """Surface a failure as a scroll-independent SnackBar.

        The old top-of-header banner duplicated every message (banner plus
        snack) and sat there until dismissed; notices are SnackBars only
        now (Sherlock's pattern). Never swallow.
        """

        def _apply() -> None:
            show_snack(self.page, message, bgcolor=theme.ERROR, duration=6000)

        LOG.warning("notice: %s", message)
        self._run_on_ui(_apply)()

    def _dismiss_notice(self) -> None:
        state.notice = ""

    def _notify_info(self, message: str) -> None:
        """Non-error feedback as a floating SnackBar."""
        LOG.info("notice: %s", message)

        def _apply() -> None:
            show_snack(self.page, message, duration=2500, floating=True)

        self._run_on_ui(_apply)()

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
            try:
                state.log_version += 1
            except RuntimeError:
                # Page context detached (quit/disconnect): the observable
                # notify cannot reach a page. Stop the cadence quietly
                # instead of dying as an unretrieved task exception.
                return
        self._log_flush_task = None
        if self._log_dirty:
            # Records landed during the window; keep the ~2Hz cadence.
            self._start_log_flusher()

    def _in_flet_ctx(self, fn: Callable) -> Callable:
        """Run fn on the Flet event loop under the captured context."""
        return self._run_on_ui(fn)

    def _on_window_event(self, event: object) -> None:
        # WindowEvent carries the event kind on `.type` (WindowEventType);
        # `.data` is None, so a `.data` check never matches and the X button
        # looks dead while prevent_close keeps the window open.
        if getattr(event, "type", None) == ft.WindowEventType.CLOSE:
            # No silent hide-to-taskbar: the user pressed X expecting the app
            # to close. Ask first, and say what quitting costs: the gateway
            # stops, so anything talking to it (other chat apps, the public
            # share URL) loses the connection.
            self._confirm_quit()

    def _on_page_close(self, e: object = None) -> None:
        """Session/console close: ask before quitting, exactly like the X."""
        self._confirm_quit()

    def _confirm_quit(self) -> None:
        """Ask before quitting — quitting ends the gateway and the share."""
        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("Quit LM Router?"),
            content=ft.Text(
                "This stops your local gateway and disconnects anything "
                "talking to it, including the shared URL. Keys and "
                "settings stay on this device.",
            ),
            actions=[
                ft.TextButton("Cancel", on_click=lambda _: self.page.pop_dialog()),
                ft.TextButton("Quit", on_click=lambda _: self._quit_confirmed()),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self.page.show_dialog(dialog)

    def _quit_confirmed(self) -> None:
        with contextlib.suppress(Exception):
            self.page.pop_dialog()
        self._quit_app()

    def _set_tab(self, index: int) -> None:
        # Clamp once at the single setter so every caller (nav bar, header
        # buttons, deep links) lands on a mounted view by construction.
        index = index if 0 <= index < 3 else 0
        state.selected_tab = index
        state.last_nav_tab = index

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
            # Start the foreground service now the gateway is confirmed up.
            if state.gateway_running and self._notify is not None:
                port = self.services.engine.port or state.gateway_port
                self.page.run_task(self._notify.start, port)
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
            # Stop the foreground service now the gateway is down.
            if self._notify is not None:
                try:
                    self.page.run_task(self._notify.stop)
                except Exception as exc:
                    LOG.debug("notify stop on gateway stop: %s", exc)

        self.page.run_thread(work)

    def _refresh_models(self) -> None:
        # refresh=true makes the engine force a fresh upstream probe server-side
        self.page.run_thread(lambda: self._fetch_models(refresh=True))

    def _fetch_models(self, refresh: bool) -> None:
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
            data = self._async.run(fetch)
        except Exception as exc:
            # Cancellation and some runner errors stringify to ""; a notice
            # with an empty reason is useless ("unavailable: ").
            name = str(exc) or type(exc).__name__
            LOG.warning("model catalog fetch failed: %s", name)
            self._notify_error(f"Model catalog unavailable: {name[:200]}")
            return
        if not data:
            self._notify_error(
                "Gateway returned no models. Hit 'Refresh models' on the Server tab.",
            )
            return
        try:
            limits, counts = self._async.run(fetch_sidecars)
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

    def _on_page_error(self, event: object) -> None:
        text = str(getattr(event, "data", "") or event)
        LOG.error("unhandled UI error: %s", text)
        self._notify_error(f"UI error: {text[:180]}")

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

        def work() -> None:
            try:
                result = self._async.run(
                    model_bench.test_model, http, base, model_id, endpoint
                )
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
        # Generation counter: sweep A stopping late must not clear sweep B's
        # flags if the user restarts quickly ( A's finally would clobber B ).
        self._retest_gen += 1
        gen = self._retest_gen
        state.retesting = True
        state.retest_progress = (0, len(rows))
        stop = threading.Event()
        self._retest_stop = stop
        base = state.gateway_base_url
        http = self.services.http

        def on_row(result: dict, done: int, total: int) -> None:
            self._in_flet_ctx(self._retest_row)(result, done, total)

        def work() -> None:
            try:
                self._async.run(model_bench.retest, http, base, rows, on_row, stop)
            except Exception as exc:
                LOG.warning("retest sweep failed: %s", exc)
                self._in_flet_ctx(self._notify_error)(f"Retest stopped: {str(exc)[:120]}")
            finally:
                self._in_flet_ctx(lambda: self._finish_retest(gen))()

        self.page.run_thread(work)

    def _retest_row(self, result: dict, done: int, total: int) -> None:
        self._apply_test_result(result)
        state.retest_progress = (done, total)

    def _finish_retest(self, gen: int | None = None) -> None:
        # A stale sweep's finally must not clear a newer sweep's flags.
        if gen is not None and gen != self._retest_gen:
            return
        state.retesting = False
        state.retest_progress = (0, 0)
        state.model_testing = set()
        self._retest_stop = None

    def _stop_retest(self) -> None:
        if self._retest_stop is not None:
            self._retest_stop.set()
        self._notify_info("Stopping the retest sweep after the current model…")

    # ── sharing the gateway with someone else ───────────────────────────
    #
    # The gateway itself has no auth (the router console says api_key="any"),
    # so an optional key is enforced by our own stdlib proxy in front of it.
    # Tunneling uses the localtunnel protocol: pure stdlib, works on a phone.

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
                    # NOTE: the tunnel object keeps retrying (it may recover);
                    # the banner says "reconnecting", not "closed", and the
                    # URL stays until an explicit Stop so the UI never claims
                    # sharing ended while the relay is still serving.
                    LOG.warning("share tunnel error: %s", reason)

                    def _apply() -> None:
                        if state.share_url:
                            state.share_error = f"Tunnel reconnecting: {reason}"
                        else:
                            state.share_error = f"Tunnel error: {reason}"

                    self._in_flet_ctx(_apply)()

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

    def _save_settings(self, data: dict) -> None:
        allowed = {
            "theme",
            "gateway_port",
            "gateway_autostart",
            "share_enabled",
            "require_share_key",
            "share_key",
            "interstitial_every",
            "update_check",
        }
        updates = {k: v for k, v in data.items() if k in allowed}
        if not updates:
            return
        previous = {k: getattr(self.settings, k) for k in updates}
        try:
            for key, value in updates.items():
                setattr(self.settings, key, value)
        except ValidationError as exc:
            msg = str(exc)
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
        if self._share is not None and {"require_share_key", "share_key"} & set(updates):
            # The proxy captured its key at bind time; rebind live (owner
            # decision: touching the toggle/key mid-share restarts sharing
            # and the card immediately shows the new details).
            self._refresh_share_key()

    def _copy_text(self, text: str) -> None:
        # One shared Clipboard for the app's lifetime: per-call appends grew
        # page.services without bound, and the raw show_dialog(SnackBar) below
        # raised "Dialog is already opened" whenever copy ran from inside
        # another dialog (e.g. the log terminal) — the copy succeeded but the
        # user was told it failed.
        try:
            if self._clipboard is None:
                self._clipboard = ft.Clipboard()
                self.page.services.append(self._clipboard)
            clipboard = self._clipboard

            async def _copy() -> None:
                await clipboard.set(text)
                show_snack(self.page, "Copied to clipboard")

            self.page.run_task(_copy)
        except Exception as exc:
            LOG.warning("clipboard copy failed: %s", exc)
            self._notify_error(f"Copy failed: {str(exc)[:150]}")

    def _copy_logs(self) -> None:
        """Copy the rolling log file — the terminal is not always visible."""
        self._copy_text(log_tail())

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

    def _open_ad_privacy_options(self) -> None:
        """GDPR entry point: the UMP privacy-options form when required."""
        ads = self.ads
        if ads is None:
            self._notify_info("Ads are not available in this build.")
            return

        async def _show() -> None:
            outcome = await ads.show_privacy_options()
            if outcome == "not_required":
                self._notify_info("No additional ad privacy choices are required.")
            elif outcome == "no_manager":
                self._notify_info("Consent manager is not ready yet.")
            elif outcome != "form_shown":
                self._notify_error(f"Privacy options unavailable: {outcome[:120]}")

        try:
            self.page.run_task(_show)
        except Exception as exc:
            LOG.warning("privacy options failed: %s", exc)

    def _open_update_dialog(self) -> None:
        if not state.update_info:
            LOG.info("no update to show")
            return
        dialog = build_update_dialog(
            self.page,
            state.update_info,
            self._url_launcher,
            on_close=self._dismiss_update,
            theme_mode=state.theme_mode,
        )
        # Sherlock pattern: pop the stale dialog first — a second tap on the
        # sticky chip while open raised RuntimeError from show_dialog.
        with contextlib.suppress(Exception):
            self.page.pop_dialog()
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

    def _finish_onboarding(self) -> None:
        self.settings.onboarding_done = True
        self.settings.terms_accepted = True
        self.settings.save()
        state.onboarding_done = True
        state.terms_accepted = True
        state.selected_tab = 0  # land on the Server homepage after onboarding
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
        state remounts — global AppState survives.
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

    def _on_view_pop(self, e: ft.ViewPopEvent) -> None:
        """Owner rule (Sherlock): system back must never tear down the shell
        — it maps to in-app navigation, and at the root tab it just stays."""
        try:
            popped = getattr(e, "view", None)
            if state.selected_tab != 0:
                state.selected_tab = 0
                self._restore_shell(popped)
                return
            self._restore_shell(popped)
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
                # Release the wake lock when backgrounded: the FGS keeps the
                # process alive; the screen is free to sleep.
                if self._notify is not None:
                    await self._notify.wakelock_set(False)
                return
            if e.state in (ft.AppLifecycleState.RESUME, ft.AppLifecycleState.SHOW):
                await recheck_connectivity(self.page)
                # Re-acquire the wake lock only if the gateway is still up.
                if self._notify is not None:
                    await self._notify.wakelock_set(state.gateway_running)
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

    def _quit_app(self, e: object = None) -> None:
        # Idempotent: on_close, the Quit button and the back-at-root path can
        # race. Gateway shutdown joins up to ~7s — keep it off the UI thread
        # (forensics R8); window teardown runs in the Flet context. Ordered so
        # every resource dies before the loop that owns it. The event arg is
        # accepted (defaulting to None) because page.on_close invokes the
        # handler WITH a payload — a zero-arg method raises TypeError on
        # exactly the framework-initiated close path.
        if self._quitting:
            return
        self._quitting = True

        def work() -> None:
            # The shared httpx client is bound to the runner loop; close it
            # ON that loop before stopping the runner.
            try:
                self._async.run(self.services.http.aclose, timeout=10)
            except Exception as exc:
                LOG.debug("http client close: %s", exc)
            try:
                # Share proxy + tunnel outlived quit: daemon threads die with
                # the process, but the session must be marked stopped first.
                # Stop the share BEFORE the engine: the proxy fronts the
                # gateway, and stopping the engine first leaves a window
                # where the tunnel serves 502s with no UI left to stop it.
                self._in_flet_ctx(self._stop_share)()
            except Exception as exc:
                LOG.debug("share shutdown: %s", exc)
            try:
                self.services.engine.stop()
            except Exception as exc:
                LOG.warning("gateway shutdown: %s", exc)
            # Stop the foreground service so the notification clears on exit.
            if self._notify is not None:
                try:
                    self.page.run_task(self._notify.stop)
                except Exception as exc:
                    LOG.debug("notify stop on quit: %s", exc)
            self._async.stop()
            self._in_flet_ctx(self._finish_quit)()

        self.page.run_thread(work)

    def _finish_quit(self) -> None:
        """Flet-context step: release the ads, THEN destroy the window — one
        run_task chain so window teardown can never race the ad release."""

        async def _close_then_destroy() -> None:
            ads = self.ads
            if ads is not None:
                try:
                    # Bounded: a hung ad SDK must never wedge quit with the
                    # _quitting latch already set (no retry possible).
                    await asyncio.wait_for(ads.close(), timeout=5)
                except Exception as exc:
                    LOG.debug("ad close: %s", exc)
            self._destroy_window()

        # Closed-loop guard: during teardown run_task itself raises
        # ("Event loop is closed") and a synchronous fallback would schedule
        # ANOTHER run_task on the dead loop, producing the "coroutine was
        # never awaited" cascade (ads.close, window.destroy).
        try:
            loop = self._ui_loop
            if loop is None or loop.is_closed():
                LOG.debug("quit: UI loop already closed; skipping window teardown")
                return
            self.page.run_task(_close_then_destroy)
        except Exception as exc:
            LOG.warning("quit failed: %s", exc)
            # Last resort: the run_task dispatch itself failed with the
            # latch already set, so still attempt window teardown directly.
            try:
                loop = self._ui_loop
                if loop is None or loop.is_closed():
                    return
                self._destroy_window()
            except Exception as destroy_exc:
                LOG.warning("quit teardown failed: %s", destroy_exc)

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
