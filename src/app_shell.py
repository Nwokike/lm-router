"""App shell: onboarding gate, offline banner, AppHeader + bottom navigation.

History is a sub-view, not a fourth tab: the NavigationBar carries three
destinations (Chat/Server/Settings) and opening History keeps the bar's
highlight on the tab the user came from instead of lying about location.
"""

import contextlib

import flet as ft

from components.app_header import AppHeader
from core import tokens
from core.notify import show_snack
from core.state import AppStateCtx
from screens.chat_screen import ChatScreen
from screens.history_screen import HistoryScreen
from screens.onboarding_screen import OnboardingScreen
from screens.server_screen import ServerScreen
from screens.settings_screen import SettingsScreen
from state.controller_ctx import ControllerMethodsCtx


def _sync_navigation_bar(page, state, methods) -> None:
    """Attach the shell's NavigationBar to the TOP view (KTV's View-chrome
    pattern: Material owns the bar's height and system insets).

    Module-level so it is testable without the effect machinery: flet only
    flushes use_effect callbacks against a live session, which the render
    harness has none of. Targets views[-1] — the Android back underlay sits
    at views[0] once installed and is never visible; attaching the bar there
    hid the bottom navigation entirely (owner device regression).
    """
    if not getattr(page, "views", None):
        return
    view = page.views[-1]
    if not state.onboarding_done:
        if view.navigation_bar is not None:
            view.navigation_bar = None
            with contextlib.suppress(Exception):
                page.update()
        return
    index = state.selected_tab if 0 <= state.selected_tab < 4 else 0
    view.navigation_bar = ft.NavigationBar(
        # History (3) is a sub-view: keep the highlight on the last real tab
        # so the bar never claims the user is on Chat while reading History.
        selected_index=state.last_nav_tab
        if 0 <= state.last_nav_tab < 3
        else (index if index < 3 else 0),
        on_change=lambda e: methods.set_tab(int(e.control.selected_index)),
        destinations=[
            ft.NavigationBarDestination(
                icon=ft.Icons.CHAT_BUBBLE_OUTLINE,
                selected_icon=ft.Icons.CHAT_BUBBLE,
                label="Chat",
            ),
            ft.NavigationBarDestination(
                icon=ft.Icons.DNS_OUTLINED,
                selected_icon=ft.Icons.DNS,
                label="Server",
            ),
            ft.NavigationBarDestination(
                icon=ft.Icons.SETTINGS_OUTLINED,
                selected_icon=ft.Icons.SETTINGS,
                label="Settings",
            ),
        ],
    )
    with contextlib.suppress(Exception):
        page.update()


@ft.component
def AppShell():
    state = ft.use_context(AppStateCtx)
    methods = ft.use_context(ControllerMethodsCtx)

    def _sync_nav_effect() -> None:
        page = getattr(ft.context, "page", None)
        if page is None:
            return
        _sync_navigation_bar(page, state, methods)

    # Hooks run before any early return so the effect survives the
    # onboarding -> dashboard transition (rules of hooks).
    ft.use_effect(
        _sync_nav_effect,
        [state.selected_tab, state.onboarding_done],
    )

    def _surface_boot_notice() -> None:
        # state.notice carries one-time boot warnings (dropped provider
        # keys, malformed stored items). The old top-of-header banner is
        # gone: notices surface as SnackBars (scroll-independent,
        # Sherlock's pattern), shown once after the first frame, then
        # cleared so the effect never loops.
        if not state.notice:
            return
        page = getattr(ft.context, "page", None)
        show_snack(page, state.notice, duration=10000)
        methods.dismiss_notice()

    ft.use_effect(_surface_boot_notice, [state.notice])

    if not state.onboarding_done:
        return ft.SafeArea(
            expand=True,
            content=OnboardingScreen(key=ft.ValueKey("view-onboarding")),
        )

    # Solid error background + on-error foreground: the old tinted wash with
    # ERROR-on-tint text was low-contrast in dark mode. Omitted entirely when
    # online (no dead layout node).
    offline_banner = (
        ft.Container(
            padding=ft.Padding(tokens.SPACE_LG, tokens.SPACE_SM, tokens.SPACE_LG, tokens.SPACE_SM),
            bgcolor=ft.Colors.ERROR,
            content=ft.Row(
                spacing=tokens.SPACE_SM,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Icon(
                        ft.Icons.WIFI_OFF_ROUNDED,
                        size=tokens.ICON_SM,
                        color=ft.Colors.ON_ERROR,
                    ),
                    ft.Text(
                        "You're offline. Gateway and search may be unavailable.",
                        size=tokens.FONT_MD,
                        weight=ft.FontWeight.BOLD,
                        color=ft.Colors.ON_ERROR,
                    ),
                ],
            ),
        )
        if state.offline
        else None
    )

    def _build_view(index: int):
        # Lazy: only the visible screen pays construction cost each render.
        # The old 4-element list rebuilt Chat+Server+Settings+History (catalog
        # closures, tier lookups, pill builders) on every log flush at ~2Hz.
        if index == 1:
            return ft.Column(
                expand=True,
                spacing=0,
                controls=[
                    AppHeader(
                        title="Server",
                        show_quit=True,
                        extra_actions=[
                            ft.IconButton(
                                ft.Icons.TERMINAL_ROUNDED,
                                icon_size=tokens.ICON_MD,
                                tooltip="Activity log",
                                on_click=lambda _: methods.open_log_terminal(),
                            ),
                        ],
                    ),
                    ServerScreen(key=ft.ValueKey("view-server")),
                ],
            )
        if index == 2:
            return ft.Column(
                expand=True,
                spacing=0,
                controls=[
                    AppHeader(title="Settings", show_quit=True),
                    SettingsScreen(key=ft.ValueKey("view-settings")),
                ],
            )
        if index == 3:
            return ft.Column(
                expand=True,
                spacing=0,
                controls=[
                    AppHeader(title="History", show_quit=True),
                    HistoryScreen(key=ft.ValueKey("view-history")),
                ],
            )
        return ft.Column(
            expand=True,
            spacing=0,
            controls=[
                AppHeader(
                    title="Chat",
                    extra_actions=[
                        ft.IconButton(
                            ft.Icons.HISTORY_ROUNDED,
                            icon_size=tokens.ICON_MD,
                            tooltip="History",
                            on_click=lambda _: methods.set_tab(3),
                        ),
                    ],
                ),
                ChatScreen(key=ft.ValueKey("view-chat")),
            ],
        )

    index = state.selected_tab if 0 <= state.selected_tab < 4 else 0

    # SafeArea keeps the header clear of the Android status bar and the
    # content clear of the gesture bar; the nav itself is View chrome and
    # handles its own inset (see _sync_navigation_bar).
    controls: list = []
    if offline_banner is not None:
        controls.append(offline_banner)
    controls.append(ft.Container(expand=True, content=_build_view(index)))
    return ft.SafeArea(
        expand=True,
        content=ft.Column(
            expand=True,
            spacing=0,
            controls=controls,
        ),
    )
