"""Live render smoke using voicelm's fake-page harness.

Every screen body executes through the REAL flet Renderer (not just
imports), and mounted components are unmounted afterwards so global
observable subscriptions cannot schedule updates against the fake page
from later tests.
"""

from __future__ import annotations

import contextlib

import flet as ft
import pytest
from flet.components.component import Component, Renderer
from flet.controls.context import _context_page


class _FakeServices(list):
    def register_service(self, svc):
        with contextlib.suppress(Exception):
            list.append(self, svc)
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
    """Render a component AND execute its body, recursively.

    `Renderer().render()` only wraps the callable; the body first runs during
    `before_update()`. Calling render() alone let screens that raise on their
    first constructed control (e.g. Container(scroll=...)) report as passing.

    `before_update()` on a Component only renders ITS own body. Nested
    components returned by it (Composer, SessionBar, ThinkingBlock...) build
    later, during the real page patch — which is how `Chip(avatar=...)` (not
    a flet 1.0 property) reached production unnoticed. `_deep_update` walks
    the whole tree so nested components are executed here too.
    """
    component = Renderer().render(component_fn)
    _deep_update(component)
    return component


def _deep_update(control, depth: int = 0) -> None:
    """Execute before_update on `control` and every nested Component."""
    if control is None or depth > 30:
        return
    if isinstance(control, Component):
        control.before_update()
        for child in _flatten(getattr(control, "_b", None)):
            _deep_update(child, depth + 1)
        return
    # flet_ads controls resolve self.page by walking a real _parent chain to a
    # live Page; the synthetic test tree has none, so they raise here even
    # though they are fine in the app. Skip them rather than fake a parent.
    if type(control).__module__.startswith("flet_ads"):
        return
    # Plain controls: run their own validators, then recurse.
    control.before_update()
    for child in _children(control):
        _deep_update(child, depth + 1)


def _flatten(value):
    if value is None:
        return []
    if isinstance(value, list):
        return [v for v in value if v is not None]
    return [value]


def _children(control):
    """Best-effort child extraction across flet's control shapes."""
    out = []
    for attr in ("controls", "content", "leading", "trailing", "items", "views"):
        value = getattr(control, attr, None)
        if isinstance(value, list):
            out.extend(v for v in value if v is not None)
        elif value is not None and hasattr(value, "__dict__"):
            out.append(value)
    return out


def test_all_screens_render(_renderer_page):
    from app_shell import AppShell
    from core.state import state
    from screens.chat_screen import ChatScreen
    from screens.history_screen import HistoryScreen
    from screens.onboarding_screen import OnboardingScreen
    from screens.server_screen import ServerScreen
    from screens.settings_screen import SettingsScreen
    from state.controller_ctx import ControllerMethods, ControllerMethodsCtx

    # Exercise the populated branches too, not only the empty states.
    state.onboarding_done = True
    state.gateway_running = True
    state.gateway_lan_url = "http://192.168.1.5:8082/v1"
    state.messages = [{"role": "user", "content": "hello"}]
    state.conversations = [
        {"id": "abc", "title": "Test chat", "relative": "now", "updated": "2026-01-01"}
    ]

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
        shell_root = _render(
            lambda: ControllerMethodsCtx(ControllerMethods(), lambda: AppShell()),
        )
        roots.append(shell_root)
        assert shell_root is not None
    finally:
        for root in roots:
            with contextlib.suppress(Exception):
                root._detach_observable_subscriptions()
                root._state.mounted = False
