"""LM Router entry point."""

import asyncio
import contextvars
import os
import time
from collections.abc import Callable
from typing import Any

import flet as ft
from pydantic import ValidationError

from app_shell import AppShell
from components.about_dialog import build_about_dialog
from components.connectivity_monitor import start_connectivity_monitor
from components.update_dialog import build_update_dialog
from core import constants, theme
from core.catalog import AUTO_MODEL_ID, chat_models
from core.logging import LOG
from core.logging import tail as log_tail
from core.settings import AppSettings, MCPServerConfig, ProviderConfig, pop_load_warnings
from core.state import state
from services import history
from services.ad_service import AdService
from services.agent import AgentService
from services.clock import build_time_tool, with_clock
from services.engine import EngineService
from services.file_save import save_text_file
from services.http import HttpService
from services.mcp import MCPHub
from services.reasoning import final_reasoning
from services.search import build_search_tool
from services.tokenizer import prewarm_tokenizers
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
        self._mcp_future: Any = None
        # flet page context captured at init: chat callbacks run on the anyio
        # portal thread, which has no context of its own (kani thread study).
        self._flet_ctx = contextvars.copy_context()

    def init(self) -> None:
        page = self.page
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

        # Surface anything load() had to skip/drop (dropped provider keys,
        # malformed stored items) — never silently lose user data state.
        load_warnings = pop_load_warnings()
        if load_warnings:
            state.notice = "; ".join(load_warnings)
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

        # conversation catalog on boot, fresh id for new chats
        state.conversations = history.list_conversations()
        if not state.active_conversation:
            history.start_conversation()

        # connectivity: OS events + HTTP confirm, banner rendered by the shell
        try:
            page.services.append(ft.Connectivity())
        except Exception as exc:
            LOG.info("connectivity service unavailable: %s", exc)
        page.run_task(start_connectivity_monitor, page)

        # ads: UMP consent then preload (gates live inside AdService)
        self.ads = AdService(page)
        page.run_task(self._boot_ads)
        page.run_thread(prewarm_tokenizers)

        # MCP owner task: ONE long-lived portal task owns the session
        # context (enter+exit in the same task). Config changes just signal
        # it; it never shares a cancel scope across portal tasks.
        if settings.mcp_servers:
            try:
                self._mcp_future = agent.spawn(hub.serve)
            except Exception as exc:
                LOG.warning("mcp owner task not started: %s", exc)
                self._notify_error(f"MCP subsystem unavailable: {str(exc)[:150]}")

        # controllers wired for this step (later steps extend the same object)
        m = self.methods
        m.set_tab = self._set_tab
        m.set_theme = self._set_theme
        m.start_gateway = self._start_gateway
        m.stop_gateway = self._stop_gateway
        m.refresh_models = self._refresh_models
        m.open_gateway_console = self._open_console
        m.quit_app = self._quit_app
        m.send_message = self._send_message
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
        direct call when the loop cannot be determined (tests/headless).
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

            try:
                return asyncio.run_coroutine_threadsafe(_call(), loop).result(timeout=30)
            except TimeoutError:
                LOG.error("UI callback timed out: %s", getattr(fn, "__name__", fn))
                return None

        return run

    def _notify_error(self, message: str) -> None:
        """Surface a failure in the shell banner + log. Never swallow."""

        def _apply() -> None:
            state.notice = message
            state.notice_id += 1

        LOG.warning("notice: %s", message)
        self._run_on_ui(_apply)()

    def _dismiss_notice(self) -> None:
        state.notice = ""

    def _notify_info(self, message: str) -> None:
        """Non-error feedback (floating SnackBar); falls back to the banner."""
        LOG.info("notice: %s", message)

        def _apply() -> None:
            try:
                self.page.show_dialog(
                    ft.SnackBar(
                        content=ft.Text(message),
                        behavior=ft.SnackBarBehavior.FLOATING,
                        duration=2500,
                    ),
                )
            except Exception:
                state.notice = message

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
        try:
            state.log_version += 1
        except RuntimeError:
            # logged from a non-flet thread with no page context; the UI
            # catches the next in-context bump
            return

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

    def _start_gateway_quiet(self) -> None:
        try:
            self.services.engine.start()
        except Exception as exc:
            LOG.error("gateway autostart failed: %s", exc)
            self._notify_error(f"Gateway failed to start: {str(exc)[:200]}")
            return
        self._fetch_models(refresh=False)

    def _start_gateway(self) -> None:
        self.page.run_thread(self._start_gateway_quiet)

    def _stop_gateway(self) -> None:
        def work() -> None:
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
                "No active chat models right now — try Refresh models on the Server tab.",
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

    def _send_failed(self, content: str) -> None:
        state.busy = False
        state.messages.append({"role": "error", "content": content})

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
                {"role": "error", "content": "Gateway is not running. Start it on the Server tab."},
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
                f"Tools unavailable — running without search/MCP ({tool_err[:100]}).",
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
                state.session_total_tokens += total
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
                state.messages.append({"role": "error", "content": content})
                LOG.warning("turn error (%s): %s", kind, content)
            else:
                state.messages[index] = {"role": "error", "content": content}
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
            self._notify_error("Conversation not saved — check disk space (see logs).")
            return
        state.conversations = items
        if not ok:
            self._notify_error("Conversation not saved — check disk space (see logs).")

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

    def _reapply_mcp(self) -> None:
        """Signal the MCP owner task (thread-safe, instant).

        The owner task owns the session context for the app's lifetime: a
        per-call apply/close from different portal tasks poisons the anyio
        task group ("cancel scope in a different task") and permanently
        bricks the agent portal — chat then never runs again (boot-diff
        reproduction: an enabled unreachable server killed every send).
        """
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
                f"An MCP server named '{name}' already exists (names must be unique).",
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
        self._reapply_mcp()

    def _remove_mcp_server(self, server_id: str) -> None:
        self.settings.mcp_servers = [s for s in self.settings.mcp_servers if s.id != server_id]
        self.settings.save()
        self._reapply_mcp()

    def _toggle_mcp_server(self, server_id: str) -> None:
        for server in self.settings.mcp_servers:
            if server.id == server_id:
                server.enabled = not server.enabled
        self.settings.save()
        self._reapply_mcp()

    def _toggle_mcp_tool(self, server_id: str, tool_name: str) -> None:
        for server in self.settings.mcp_servers:
            if server.id == server_id:
                if tool_name in server.disabled_tools:
                    server.disabled_tools.remove(tool_name)
                else:
                    server.disabled_tools.append(tool_name)
        self.settings.save()
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
                deliver(("err", "Timed out after 20s — check the URL/command."))
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

    def _clear_history(self) -> None:
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
            self._notify_error("Could not load that conversation (see logs).")
            return
        state.active_conversation = conversation_id
        state.messages = history.messages_from_history(agent)
        self._set_tab(0)

    def _delete_conversation(self, conversation_id: str) -> None:
        def work() -> None:
            ok = history.delete_conversation(conversation_id)
            items = history.list_conversations()
            self._in_flet_ctx(self._apply_history_change)(items, 0 if ok else 1)

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
                f"Could not delete {failures} conversation file(s) — see logs.",
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
        content, filename = history.export_conversation_markdown(conversation_id)
        if not content:
            self._notify_error("Export failed: conversation not readable.")
            return

        async def _write() -> None:
            path = await save_text_file(
                self.page,
                content,
                filename,
                dialog_title=f"Export {filename}",
            )
            if path:
                LOG.info("exported conversation %s to %s", conversation_id, path)
                self._in_flet_ctx(self._notify_info)(f"Saved to {path}")
            else:
                self._in_flet_ctx(self._notify_error)(
                    "Could not save the file. Check that the folder is writable.",
                )

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
                self._notify_info(f"You're up to date (build {constants.BUILD_NUMBER}).")
            return
        state.update_info = data
        if not silent:
            self._open_update_dialog()

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
        dialog = build_update_dialog(self.page, state.update_info, self._url_launcher)
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

    def _quit_app(self) -> None:
        # Gateway shutdown joins up to ~7s — keep it off the UI thread
        # (forensics R8); window teardown runs in the Flet context.
        def work() -> None:
            try:
                self.services.mcp.request_stop()
            except Exception as exc:
                LOG.debug("mcp stop signal: %s", exc)
            try:
                self.services.agent.stop()
            except Exception as exc:
                LOG.warning("agent shutdown: %s", exc)
            try:
                self.services.engine.stop()
            except Exception as exc:
                LOG.warning("gateway shutdown: %s", exc)
            self._in_flet_ctx(self._destroy_window)()

        self.page.run_thread(work)

    def _destroy_window(self) -> None:
        # Window.destroy is a coroutine in Flet 1.0: calling it bare returns an
        # un-awaited coroutine and the window never closes. Re-enable the close
        # guard synchronously, then hand the destroy to the Flet event loop.
        self.page.window.prevent_close = False
        try:
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


if __name__ == "__main__":
    # Flet resolves a relative assets_dir against THIS SCRIPT's directory, so
    # the fallback must be "assets" — "src/assets" resolved to src/src/assets
    # and silently dropped the icon and fonts.
    ft.run(main, assets_dir=os.environ.get("FLET_ASSETS_DIR") or "assets")
