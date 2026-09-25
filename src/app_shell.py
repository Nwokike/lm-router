"""App shell: onboarding gate, offline banner, AppHeader + four tabs."""

import contextlib

import flet as ft

from components.app_header import AppHeader
from core import tokens
from core.state import AppStateCtx
from screens.chat_screen import ChatScreen
from screens.history_screen import HistoryScreen
from screens.onboarding_screen import OnboardingScreen
from screens.server_screen import ServerScreen
from screens.settings_screen import SettingsScreen
from state.controller_ctx import ControllerMethodsCtx


@ft.component
def AppShell():
    state = ft.use_context(AppStateCtx)
    methods = ft.use_context(ControllerMethodsCtx)

    def _sync_navigation_bar() -> None:
        # KTV Player's pattern: the NavigationBar is View chrome, not a child
        # of the screen Column, so Material owns its height and system insets.
        # page.render() only replaces views[0].controls, so what we set here
        # survives re-renders; the effect re-runs on tab/onboarding changes.
        page = getattr(ft.context, "page", None)
        if page is None or not getattr(page, "views", None):
            return
        view = page.views[0]
        if not state.onboarding_done:
            if view.navigation_bar is not None:
                view.navigation_bar = None
                with contextlib.suppress(Exception):
                    page.update()
            return
        index = state.selected_tab if 0 <= state.selected_tab < 4 else 0
        view.navigation_bar = ft.NavigationBar(
            selected_index=index if index < 3 else 0,
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

    # Hooks run before any early return so the effect survives the
    # onboarding -> dashboard transition (rules of hooks).
    ft.use_effect(
        _sync_navigation_bar,
        [state.selected_tab, state.onboarding_done],
    )

    if not state.onboarding_done:
        return ft.SafeArea(
            expand=True,
            content=OnboardingScreen(key=ft.ValueKey("view-onboarding")),
        )

    offline_banner = (
        ft.Container(
            padding=ft.Padding(tokens.SPACE_LG, tokens.SPACE_SM, tokens.SPACE_LG, tokens.SPACE_SM),
            bgcolor=ft.Colors.with_opacity(tokens.OPACITY_STRONG, ft.Colors.ERROR),
            content=ft.Row(
                spacing=tokens.SPACE_SM,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Icon(
                        ft.Icons.WIFI_OFF_ROUNDED,
                        size=tokens.ICON_XS,
                        color=ft.Colors.ERROR,
                    ),
                    ft.Text(
                        "You're offline. Gateway and search may be unavailable.",
                        size=tokens.FONT_SM,
                        color=ft.Colors.ERROR,
                    ),
                ],
            ),
        )
        if state.offline
        else ft.Container(height=0)
    )

    notice_banner = (
        ft.Container(
            padding=ft.Padding(tokens.SPACE_LG, tokens.SPACE_SM, tokens.SPACE_LG, tokens.SPACE_SM),
            bgcolor=ft.Colors.with_opacity(tokens.OPACITY_STRONG, ft.Colors.ERROR),
            content=ft.Row(
                spacing=tokens.SPACE_SM,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Icon(
                        ft.Icons.ERROR_OUTLINE_ROUNDED,
                        size=tokens.ICON_XS,
                        color=ft.Colors.ERROR,
                    ),
                    ft.Text(
                        state.notice,
                        size=tokens.FONT_SM,
                        color=ft.Colors.ERROR,
                        max_lines=3,
                        overflow=ft.TextOverflow.ELLIPSIS,
                        expand=True,
                    ),
                    ft.IconButton(
                        ft.Icons.CLOSE,
                        icon_size=tokens.ICON_XS,
                        tooltip="Dismiss",
                        on_click=lambda _: methods.dismiss_notice(),
                    ),
                ],
            ),
        )
        if state.notice
        else ft.Container(height=0)
    )

    views = [
        ft.Column(
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
        ),
        ft.Column(
            expand=True,
            spacing=0,
            controls=[
                AppHeader(title="Server", show_quit=True),
                ServerScreen(key=ft.ValueKey("view-server")),
            ],
        ),
        ft.Column(
            expand=True,
            spacing=0,
            controls=[
                AppHeader(title="Settings", show_quit=True),
                SettingsScreen(key=ft.ValueKey("view-settings")),
            ],
        ),
        ft.Column(
            expand=True,
            spacing=0,
            controls=[
                AppHeader(title="History", show_quit=True),
                HistoryScreen(key=ft.ValueKey("view-history")),
            ],
        ),
    ]
    index = state.selected_tab if 0 <= state.selected_tab < len(views) else 0

    # SafeArea keeps the header clear of the Android status bar and the
    # content clear of the gesture bar; the nav itself is View chrome and
    # handles its own inset (see _sync_navigation_bar).
    return ft.SafeArea(
        expand=True,
        content=ft.Column(
            expand=True,
            spacing=0,
            controls=[
                offline_banner,
                notice_banner,
                ft.Container(expand=True, content=views[index]),
            ],
        ),
    )
