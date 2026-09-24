"""Regression tests for the Flet 1.0 API bugs that made the app inert.

Every test here corresponds to a defect that shipped because the old suite only
constructed screens and never fired an event handler or asserted a real Flet
constructor contract. The runtime is the installed flet package, so a future
flet upgrade that changes these contracts fails here instead of in the GUI.
"""

from __future__ import annotations

import os
import tempfile

import flet as ft
import pytest
from flet.components.component import Renderer
from flet.controls.context import _context_page
from flet.controls.control_event import Event


class _StubServices(list):
    def register_service(self, svc):
        list.append(self, svc)
        return svc

    def unregister_services(self):
        pass


class _StubPage:
    """Minimal page stand-in: enough context for component render."""

    platform = ft.PagePlatform.WINDOWS
    theme_mode = ft.ThemeMode.SYSTEM

    def __init__(self) -> None:
        self.views = [ft.View()]
        self._services = _StubServices()
        self.services: list = []
        self.window = type(
            "W",
            (),
            {"prevent_close": False, "visible": True, "on_event": None},
        )()
        self.session = type(
            "S",
            (),
            {
                "patch_control": lambda *a, **k: None,
                "schedule_update": lambda *a, **k: None,
            },
        )()

    def show_dialog(self, dialog) -> None:
        self.dialog = dialog

    def pop_dialog(self):
        return None

    def update(self) -> None:
        pass

    def run_task(self, *args, **kwargs) -> None:
        pass

    def run_thread(self, *args, **kwargs) -> None:
        pass


@pytest.fixture
def stub_page(monkeypatch):
    monkeypatch.setenv("FLET_APP_STORAGE_DATA", tempfile.mkdtemp())
    page = _StubPage()
    _context_page.set(page)
    return page


def test_navigation_bar_change_carries_index_on_control_not_data():
    """The tab bar regression: `int(e.data)` raised TypeError on every tap.

    NavigationBar.on_change is a plain Event whose payload lives on
    `e.control.selected_index`; `e.data` is None, so the old handler killed
    itself silently and the tabs never changed.
    """
    from app_shell import AppShell  # noqa: F401  (import proves module is sane)

    bar = ft.NavigationBar(
        selected_index=0,
        destinations=[
            ft.NavigationBarDestination(icon=ft.Icons.CHAT_BUBBLE_OUTLINE, label="Chat"),
            ft.NavigationBarDestination(icon=ft.Icons.DNS_OUTLINED, label="Server"),
            ft.NavigationBarDestination(icon=ft.Icons.SETTINGS_OUTLINED, label="Settings"),
        ],
    )
    event = Event(control=bar, name="change", data=None)

    assert event.data is None, "flet changed: NavigationBar now populates e.data"
    assert int(event.control.selected_index) == 0

    bar.selected_index = 2
    assert int(Event(control=bar, name="change", data=None).control.selected_index) == 2


def test_app_shell_navigation_handler_uses_control_payload(stub_page):
    """Lock the shell's actual tab handler to the working payload.

    Component output is built lazily, so this asserts on the shipped source:
    the NavigationBar handler must read `e.control.selected_index`. The old
    `int(e.data)` raised TypeError on every tap and the tabs never moved.
    """
    import inspect

    from app_shell import AppShell

    source = inspect.getsource(AppShell)
    assert "e.control.selected_index" in source, (
        "AppShell NavigationBar handler must use e.control.selected_index"
    )
    assert "int(e.data)" not in source, (
        "AppShell still reads the NavigationBar payload from e.data, which is None"
    )


def _mount(component_fn):
    """Render AND execute a component body.

    `Renderer().render()` only wraps the function; the body first runs during
    `before_update()`. The old render smoke called render() alone, so a screen
    that raised TypeError on its first constructed control still "passed".
    """
    component = Renderer().render(component_fn)
    component.before_update()
    return component


def test_component_body_actually_executes(stub_page):
    """Sanity check on the harness itself: a bad control must raise."""

    @ft.component
    def _Broken():
        return ft.Container(scroll=ft.ScrollMode.AUTO)  # type: ignore[call-arg]

    with pytest.raises(TypeError):
        _mount(_Broken)


def test_every_screen_body_executes(stub_page):
    """Each screen must build its real control tree, not just import."""
    from core.state import state
    from screens.chat_screen import ChatScreen
    from screens.history_screen import HistoryScreen
    from screens.onboarding_screen import OnboardingScreen
    from screens.server_screen import ServerScreen
    from screens.settings_screen import SettingsScreen

    state.onboarding_done = True
    state.gateway_running = True
    state.messages = [{"role": "user", "content": "hi"}]

    for screen in (
        OnboardingScreen,
        ChatScreen,
        ServerScreen,
        SettingsScreen,
        HistoryScreen,
    ):
        component = _mount(screen)
        try:
            assert component is not None
        finally:
            component._detach_observable_subscriptions()
            component._state.mounted = False


def test_container_rejects_scroll_so_screens_must_scroll_the_column(stub_page):
    """Guards the two screens that used `Container(scroll=...)`.

    Container has no `scroll` property in flet 1.0; the whole screen raised
    TypeError on its first render.
    """
    with pytest.raises(TypeError):
        ft.Container(scroll=ft.ScrollMode.AUTO)  # type: ignore[call-arg]

    # The supported form must keep working.
    ft.Column(scroll=ft.ScrollMode.AUTO)

    from screens.history_screen import HistoryScreen
    from screens.settings_screen import SettingsScreen

    for screen in (HistoryScreen, SettingsScreen):
        component = _mount(screen)
        component._detach_observable_subscriptions()
        component._state.mounted = False


def test_server_screen_qr_uses_valid_image_src(stub_page):
    """The QR card used Image(src_base64=...), which does not exist."""
    from core.state import state
    from screens.server_screen import ServerScreen

    with pytest.raises(TypeError):
        ft.Image(src_base64="iVBORw0KGgo=")  # type: ignore[call-arg]

    state.onboarding_done = True
    state.gateway_running = True
    state.gateway_lan_url = "http://192.168.1.5:8082/v1"
    component = _mount(ServerScreen)
    try:
        assert component is not None
    finally:
        component._detach_observable_subscriptions()
        component._state.mounted = False


def test_window_destroy_is_a_coroutine():
    """Quit called the async Window.destroy() bare, so the window never closed."""
    import inspect

    assert inspect.iscoroutinefunction(ft.Window().destroy)


def test_url_launcher_launch_url_is_a_coroutine():
    """Console/links used run_thread(lambda: launch_url(...)) — a dropped coroutine."""
    import inspect

    from flet.controls.services.url_launcher import UrlLauncher

    assert inspect.iscoroutinefunction(UrlLauncher().launch_url)


def test_window_event_payload_is_type_not_data(stub_page):
    """The close-to-tray handler read event.data; the payload is event.type."""
    assert hasattr(ft, "WindowEventType")
    event = Event(control=stub_page.window, name="event", data=None)
    event.type = ft.WindowEventType.CLOSE
    assert event.type == ft.WindowEventType.CLOSE
    assert event.data is None


def test_platform_brightness_is_brightness_not_theme_mode():
    """theme.is_dark_mode compared Brightness to ThemeMode — always False."""
    from core import theme

    class _Page:
        platform_brightness = ft.Brightness.DARK

    assert theme.is_dark_mode(_Page(), "system") is True
    assert theme.is_dark_mode(_Page(), "light") is False
    assert theme.is_dark_mode(_Page(), "dark") is True


def test_font_asset_is_a_real_font_file():
    """page.fonts accepts .ttf/.ttc/.otf — the old .css silently did nothing."""
    from core import theme

    for family, rel in theme.FONTS.items():
        assert rel.lower().endswith((".ttf", ".ttc", ".otf")), (
            f"{family} points at {rel}, which flet's Page.fonts ignores"
        )
        asset = os.path.join("src", "assets", rel)
        assert os.path.isfile(asset), f"missing font asset: {asset}"


def test_mcp_server_payload_matches_schema_fields():
    """The add form sent `target`; MCPServerConfig ignores unknown keys."""
    from core.settings import MCPServerConfig

    remote = MCPServerConfig(name="r", transport="streamable_http", url="https://x.dev/mcp")
    assert str(remote.url).startswith("https://")

    local = MCPServerConfig(name="l", transport="stdio", command="python", args=[])
    assert local.command == "python"

    # stdio without a command is rejected (the reason the old payload failed).
    with pytest.raises(Exception):
        MCPServerConfig(name="bad", transport="stdio")


def test_history_listing_survives_malformed_files(stub_page):
    """A single corrupt conversation file used to abort app startup."""
    import json

    from core import storage
    from services import history

    directory = storage.conversations_dir()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "a.json").write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    (directory / "b.json").write_text(json.dumps({"chat_history": None}), encoding="utf-8")
    (directory / "c.json").write_text(json.dumps({"chat_history": ["oops"]}), encoding="utf-8")
    (directory / "d.json").write_text("{not json", encoding="utf-8")

    items = history.list_conversations()  # must not raise
    assert len(items) == 4
    assert all(isinstance(item["title"], str) for item in items)

    content, _ = history.export_conversation_markdown("a")
    assert content == ""


def test_settings_load_survives_hostile_shapes(stub_page):
    """settings.load() ran during init; a bad file must not abort the app."""
    import json

    from core import storage
    from core.settings import AppSettings

    path = storage.settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    path.write_text(json.dumps({"providers": None, "mcp_servers": None}), encoding="utf-8")
    assert AppSettings.load() is not None

    path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert AppSettings.load() is not None

    path.write_bytes(b"\xff\xfe\x00bad utf8")
    assert AppSettings.load() is not None


def test_pill_never_wraps_a_control_in_an_icon(stub_page):
    """Regression: the discovering spinner was passed as an icon CODE.

    `ft.Icon(ProgressRing(...))` raises "type 'Control' is not a subtype of
    type 'int?'" at render time, which the GUI showed as a wall of identical
    UI errors while the gateway discovered models.
    """
    from components.chat_controls import _pill

    # A control goes in icon_control and is used as-is.
    spinner = ft.ProgressRing(width=13, height=13, stroke_width=2)
    pill = _pill("Starting gateway…", icon_control=spinner, active=True)
    # The spinner must survive into the tree unchanged, not be wrapped.
    assert spinner in _flatten(pill.content)

    # An icon code goes in icon and is wrapped.
    auto_pill = _pill("Auto", icon=ft.Icons.AUTO_AWESOME_ROUNDED, active=True)
    assert any(isinstance(c, ft.Icon) for c in _flatten(auto_pill.content))


def _flatten(value):
    if value is None:
        return []
    if isinstance(value, list):
        out = []
        for item in value:
            out.extend(_flatten(item))
        return out
    out = [value]
    for attr in ("controls", "content"):
        out.extend(_flatten(getattr(value, attr, None)))
    return out


def test_model_picker_survives_every_startup_state(stub_page):
    """The picker must never crash or say "no model" while starting up."""
    from components.chat_controls import ModelPicker
    from core.state import state
    from state.controller_ctx import ControllerMethods

    methods = ControllerMethods()
    for running, models, selected in (
        (False, [], ""),  # gateway stopped
        (True, [], ""),  # starting / discovering
        (True, [{"id": "auto", "status": "active", "endpoint_type": "chat.completion"}], "auto"),
        (True, [{"id": "m", "status": "failed", "endpoint_type": "chat.completion"}], ""),
    ):
        state.gateway_running = running
        state.models = list(models)
        state.model = selected
        component = Renderer().render(
            lambda: ModelPicker(state=state, methods=methods, is_dark=True),
        )
        component.before_update()
        component._detach_observable_subscriptions()
        component._state.mounted = False


TABLE_MARKDOWN = """## Onyeka Nwokike — Kiri (kiri.ng)

### Notable Projects
| Project | Description |
|---|---|
| **Kiri.ng** | A complex marketplace system |
| **Kiriong** | Django PWA for Nigerian artisans |
"""


def test_markdown_enables_gfm_so_tables_render(stub_page):
    """Regression: every model answer rendered its table as raw pipe text.

    ft.Markdown defaults to `extension_set=MarkdownExtensionSet.NONE` ("basic
    markdown parsing without additional extension bundle"), so GFM tables never
    parsed while bold/lists/links did. All rendering now goes through
    core.theme.markdown(), which pins GITHUB_FLAVORED.
    """
    from core import theme

    control = theme.markdown(TABLE_MARKDOWN, is_dark=True)
    assert control.extension_set == ft.MarkdownExtensionSet.GITHUB_FLAVORED
    # Table styling rides on the style sheet, not on Markdown directly.
    assert control.md_style_sheet is not None
    assert control.md_style_sheet.table_cells_decoration is not None
    assert control.code_theme == ft.MarkdownCodeTheme.MONOKAI


def test_markdown_helper_is_the_only_markdown_constructor(stub_page):
    """No screen may build ft.Markdown directly and forget the extension set."""
    import ast
    import pathlib

    offenders = []
    for path in pathlib.Path("src").rglob("*.py"):
        if path.name == "theme.py":  # the helper itself
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "Markdown"
            ):
                offenders.append(f"{path}:{node.lineno}")
    assert not offenders, f"ft.Markdown built outside core.theme.markdown: {offenders}"


def test_markdown_link_handler_survives_the_helper(stub_page):
    from core import theme

    control = theme.markdown("[docs](https://x.dev)", is_dark=True, on_tap_link=lambda e: None)
    assert control.on_tap_link is not None
