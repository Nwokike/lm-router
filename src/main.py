"""LM Router entry point."""

import os

import flet as ft

from app_shell import AppShell
from core import constants, theme
from core.logging import LOG
from core.settings import AppSettings
from core.state import state
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
        self.services.settings = settings
        self.services.http = http
        self.services.engine = engine
        try:
            self._url_launcher = ft.UrlLauncher()
            page.services.append(self._url_launcher)
        except Exception as exc:
            LOG.info("url launcher unavailable: %s", exc)

        # controllers wired for this step (later steps extend the same object)
        m = self.methods
        m.set_tab = self._set_tab
        m.set_theme = self._set_theme
        m.start_gateway = self._start_gateway
        m.stop_gateway = self._stop_gateway
        m.refresh_models = self._refresh_models
        m.open_gateway_console = self._open_console
        m.quit_app = self._quit_app

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

    def _start_gateway(self) -> None:
        self.page.run_thread(self._start_gateway_quiet)

    def _stop_gateway(self) -> None:
        self.page.run_thread(self.services.engine.stop)

    def _refresh_models(self) -> None:
        self.services.engine.refresh_models()

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

    def _quit_app(self) -> None:
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
