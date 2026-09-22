"""App shell: onboarding gate, offline banner, AppHeader + four tabs."""

import flet as ft

from components.app_header import AppHeader
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

    if not state.onboarding_done:
        return OnboardingScreen(key=ft.ValueKey("view-onboarding"))

    offline_banner = (
        ft.Container(
            padding=ft.Padding(16, 8, 16, 8),
            bgcolor=ft.Colors.with_opacity(0.15, ft.Colors.ERROR),
            content=ft.Row(
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Icon(ft.Icons.WIFI_OFF_ROUNDED, size=16, color=ft.Colors.ERROR),
                    ft.Text(
                        "You're offline. Gateway and search may be unavailable.",
                        size=12,
                        color=ft.Colors.ERROR,
                    ),
                ],
            ),
        )
        if state.offline
        else ft.SizedBox(height=0)
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
                            icon_size=22,
                            tooltip="History",
                            on_click=lambda _: methods.set_tab(3),
                        )
                    ],
                ),
                ChatScreen(key=ft.ValueKey("view-chat")),
            ],
        ),
        ft.Column(
            expand=True,
            spacing=0,
            controls=[
                AppHeader(title="Server"),
                ServerScreen(key=ft.ValueKey("view-server")),
            ],
        ),
        ft.Column(
            expand=True,
            spacing=0,
            controls=[
                AppHeader(title="Settings"),
                SettingsScreen(key=ft.ValueKey("view-settings")),
            ],
        ),
        ft.Column(
            expand=True,
            spacing=0,
            controls=[
                AppHeader(title="History"),
                HistoryScreen(key=ft.ValueKey("view-history")),
            ],
        ),
    ]
    index = state.selected_tab if 0 <= state.selected_tab < len(views) else 0

    return ft.Column(
        expand=True,
        spacing=0,
        controls=[
            offline_banner,
            ft.Container(expand=True, content=views[index]),
            ft.NavigationBar(
                selected_index=index if index < 3 else 0,
                on_change=lambda e: methods.set_tab(int(e.data)),
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
            ),
        ],
    )
