"""Live render smoke using voicelm's fake-page harness.

Every screen body executes through the REAL flet Renderer (not just
imports), and mounted components are unmounted afterwards so global
observable subscriptions cannot schedule updates against the fake page
from later tests.
"""

from __future__ import annotations

import flet as ft
import pytest
from flet.components.component import Renderer
from flet.controls.context import _context_page


class _FakeServices(list):
    def register_service(self, svc):
        try:
            list.append(self, svc)
        except Exception:
            pass
        return svc

    def unregister_services(self):
        pass


class _FakeSession:
    def patch_control(self, prev_control, control, parent, path, frozen):
        pass

    def schedule_update(self, component):
        pass


class _FakePage:
    def __init__(self) -> None:
        self._services = _FakeServices()
        self.services: list = []
        self.platform = ft.PagePlatform.ANDROID
        self.theme_mode = ft.ThemeMode.DARK
        self.views = [ft.View()]
        self.dialog = None
        self.session = _FakeSession()

    def show_dialog(self, d) -> None:
        self.dialog = d

    def pop_dialog(self) -> None:
        self.dialog = None

    def update(self) -> None:
        pass

    def run_task(self, *args, **kwargs) -> None:
        pass

    def run_thread(self, *args, **kwargs) -> None:
        pass


@pytest.fixture
def _renderer_page():
    page = _FakePage()
    _context_page.set(page)
    return page


def _render(component_fn):
    return Renderer().render(component_fn)


def test_all_screens_render(_renderer_page):
    from app_shell import AppShell
    from screens.chat_screen import ChatScreen
    from screens.history_screen import HistoryScreen
    from screens.onboarding_screen import OnboardingScreen
    from screens.server_screen import ServerScreen
    from screens.settings_screen import SettingsScreen
    from state.controller_ctx import ControllerMethods, ControllerMethodsCtx

    roots = []
    try:
        for component in (
            OnboardingScreen,
            ChatScreen,
            ServerScreen,
            SettingsScreen,
            HistoryScreen,
        ):
            roots.append(_render(component))
        shell_root = Renderer().render(
            lambda: ControllerMethodsCtx(ControllerMethods(), lambda: AppShell())
        )
        roots.append(shell_root)
        assert shell_root is not None
    finally:
        for root in roots:
            try:
                root._detach_observable_subscriptions()
                root._state.mounted = False
            except Exception:
                pass
