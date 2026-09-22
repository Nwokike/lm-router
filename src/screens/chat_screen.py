"""Chat screen. Minimal in this step; replaced by the full composer in step 4."""

import flet as ft


@ft.component
def ChatScreen():
    return ft.Container(
        expand=True,
        alignment=ft.Alignment.CENTER,
        content=ft.Column(
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            spacing=10,
            controls=[
                ft.Icon(ft.Icons.CHAT_BUBBLE_OUTLINE, size=48, color=ft.Colors.ON_SURFACE_VARIANT),
                ft.Text("Chat arrives in the next step", weight=ft.FontWeight.W_600),
                ft.Text(
                    "Streaming replies, web search and history land here.",
                    size=13,
                    color=ft.Colors.ON_SURFACE_VARIANT,
                ),
            ],
        ),
    )
