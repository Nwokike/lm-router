"""App shell: onboarding gate, then AppHeader + screens over a NavigationBar."""

import flet as ft

from components.app_header import AppHeader
from core.state import AppStateCtx
from screens.chat_screen import ChatScreen
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

    views = [
        ft.Column(
            expand=True,
            spacing=0,
            controls=[
                AppHeader(title="Chat"),
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
    ]
    index = state.selected_tab if 0 <= state.selected_tab < len(views) else 0

    return ft.Column(
        expand=True,
        spacing=0,
        controls=[
            ft.Container(expand=True, content=views[index]),
            ft.NavigationBar(
                selected_index=index,
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
