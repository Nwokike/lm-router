"""Settings screen. Minimal in this step; filled in step 6."""

import flet as ft


@ft.component
def SettingsScreen():
    return ft.Container(
        expand=True,
        alignment=ft.Alignment.CENTER,
        content=ft.Column(
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            spacing=10,
            controls=[
                ft.Icon(ft.Icons.SETTINGS, size=48, color=ft.Colors.ON_SURFACE_VARIANT),
                ft.Text("Settings arrive in step 6", weight=ft.FontWeight.W_600),
                ft.Text(
                    "Theme, gateway, providers, MCP servers and about.",
                    size=13,
                    color=ft.Colors.ON_SURFACE_VARIANT,
                ),
            ],
        ),
    )
