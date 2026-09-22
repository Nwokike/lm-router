"""App shell: NavigationBar over the three screens (voicelm pattern)."""

import flet as ft

from core.state import AppStateCtx
from screens.chat_screen import ChatScreen
from screens.server_screen import ServerScreen
from screens.settings_screen import SettingsScreen
from state.controller_ctx import ControllerMethodsCtx


@ft.component
def AppShell():
    state = ft.use_context(AppStateCtx)
    methods = ft.use_context(ControllerMethodsCtx)

    views = [
        ChatScreen(key=ft.ValueKey("view-chat")),
        ServerScreen(key=ft.ValueKey("view-server")),
        SettingsScreen(key=ft.ValueKey("view-settings")),
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
