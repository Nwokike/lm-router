"""LM Router entry point."""

import os
import time
from typing import Any

import flet as ft

from app_shell import AppShell
from core import constants, theme
from core.logging import LOG
from core.settings import AppSettings
from core.state import state
from services.agent import AgentService
from services.engine import EngineService
from services.http import HttpService
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

    def init(self) -> None:
        page = self.page
        settings = self.settings

        state.theme_mode = settings.theme
        state.gateway_port = settings.gateway_port
        state.onboarding_done = settings.onboarding_done
        state.terms_accepted = settings.terms_accepted
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

        # services
        http = HttpService()
        engine = EngineService(settings)
        agent = AgentService(settings)
        self.services.settings = settings
        self.services.http = http
        self.services.engine = engine
        self.services.agent = agent
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

        import core.logging as applog

        applog.on_record = self._bump_logs
        LOG.info("LM Router %s starting", constants.APP_VERSION)

        if settings.gateway_autostart:
            page.run_thread(self._start_gateway_quiet)

    # controller implementations

    def _bump_logs(self) -> None:
        state.log_version += 1

    def _on_window_event(self, event: object) -> None:
        name = getattr(event, "data", "") or ""
        if "CLOSE" in str(name).upper():
            self.page.window.visible = False

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
            return
        self._fetch_models(refresh=False)

    def _start_gateway(self) -> None:
        self.page.run_thread(self._start_gateway_quiet)

    def _stop_gateway(self) -> None:
        self.page.run_thread(self.services.engine.stop)

    def _refresh_models(self) -> None:
        # refresh=true makes the engine force a fresh upstream probe server-side
        self.page.run_thread(lambda: self._fetch_models(refresh=True))

    def _fetch_models(self, refresh: bool) -> None:
        agent = self.services.agent
        if agent.portal is None:
            LOG.warning("cannot fetch models: agent portal down")
            return

        async def fetch() -> list:
            suffix = "?refresh=true" if refresh else ""
            resp = await self.services.http.get(f"{state.gateway_base_url}/v1/models{suffix}")
            if resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code}")
            payload = resp.json()
            return list(payload.get("data", []))

        try:
            data = agent.call(fetch)
        except Exception as exc:
            LOG.warning("model catalog fetch failed: %s", exc)
            return
        self._apply_models(data)

    def _apply_models(self, data: list) -> None:
        ids = [m.get("id") for m in data if isinstance(m, dict) and m.get("id")]
        state.models = data
        if state.model not in ids:
            preferred = (
                self.settings.last_model
                if self.settings.last_model in ids
                else (ids[0] if ids else "")
            )
            if preferred:
                state.model = preferred
                if self.settings.last_model != preferred:
                    self.settings.last_model = preferred
                    self.settings.save()
        LOG.info("model catalog: %d models", len(ids))

    def _set_model(self, model: str) -> None:
        if not model or model == state.model:
            return
        state.model = model
        self.settings.last_model = model
        self.settings.save()
        LOG.info("model selected: %s", model)

    def _new_conversation(self) -> None:
        state.messages = []
        self.services.agent.reset_conversation()

    def _stop_generation(self) -> None:
        self.services.agent.stop_turn()

    def _send_message(self, text: str) -> None:
        text = (text or "").strip()
        if not text or state.busy:
            return
        if not state.gateway_running:
            state.messages.append(
                {"role": "error", "content": "Gateway is not running. Start it on the Server tab."}
            )
            return
        agent = self.services.agent
        kani = agent.ensure_kani(state.model, self.settings.system_prompt)
        if kani is None:
            state.messages.append({"role": "error", "content": "No model selected yet."})
            return

        state.messages.append({"role": "user", "content": text})
        state.messages.append({"role": "assistant", "content": ""})
        index = len(state.messages) - 1
        state.busy = True
        buffer = {"text": "", "last": 0.0}

        def flush() -> None:
            state.messages[index] = {"role": "assistant", "content": buffer["text"]}

        def on_delta(chunk: str) -> None:
            buffer["text"] += chunk
            now = time.monotonic()
            if now - buffer["last"] >= 0.08:
                buffer["last"] = now
                flush()

        def on_done(message: Any, usage: dict | None) -> None:
            final = buffer["text"] or str(getattr(message, "text", "") or "")
            entry: dict = {"role": "assistant", "content": final}
            if usage:
                entry["usage"] = usage
            state.messages[index] = entry
            state.busy = False
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

        started = agent.start_turn(text, on_delta, on_done, on_error)
        if not started:
            tail = state.messages[-1]
            if tail.get("role") == "assistant" and tail.get("content") == "":
                state.messages.pop()
            state.busy = False

    def _open_console(self) -> None:
        url = f"http://127.0.0.1:{self.services.engine.port}/"
        launcher = self._url_launcher
        if launcher is None:
            LOG.warning("cannot open console: url launcher missing")
            return
        try:
            self.page.run_thread(lambda: launcher.launch_url(url))
        except Exception as exc:
            LOG.warning("open console failed: %s", exc)

    def _finish_onboarding(self) -> None:
        self.settings.onboarding_done = True
        self.settings.terms_accepted = True
        self.settings.save()
        state.onboarding_done = True
        state.terms_accepted = True
        LOG.info("onboarding complete")

    def _open_url(self, url: str) -> None:
        launcher = self._url_launcher
        if launcher is None:
            LOG.warning("cannot open %s: url launcher missing", url)
            return
        try:
            self.page.run_thread(lambda u=url: launcher.launch_url(u))
        except Exception as exc:
            LOG.warning("open url failed: %s", exc)

    def _quit_app(self) -> None:
        try:
            self.services.agent.stop()
        except Exception as exc:
            LOG.warning("agent shutdown: %s", exc)
        try:
            self.services.engine.stop()
        except Exception as exc:
            LOG.warning("gateway shutdown: %s", exc)
        try:
            self.page.window.prevent_close = False
            self.page.window.destroy()
        except Exception as exc:
            LOG.warning("quit failed: %s", exc)


def main(page: ft.Page) -> None:
    controller = AppController(page)
    controller.init()
    services = controller.services
    methods = controller.methods
    page.render(
        lambda: ServiceCtx(services, lambda: ControllerMethodsCtx(methods, lambda: AppShell()))
    )


if __name__ == "__main__":
    ft.run(main, assets_dir=os.environ.get("FLET_ASSETS_DIR") or "src/assets")
