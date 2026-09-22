"""History screen: saved conversations (open / delete / clear)."""

import flet as ft

from components.banner_ad import build_banner_ad
from core import tokens
from core.state import AppStateCtx
from state.controller_ctx import ControllerMethodsCtx


@ft.component
def HistoryScreen():
    state = ft.use_context(AppStateCtx)
    methods = ft.use_context(ControllerMethodsCtx)

    rows: list = []
    for conversation in state.conversations:
        rows.append(
            ft.Container(
                padding=12,
                border_radius=tokens.RADIUS_MD,
                bgcolor=ft.Colors.SURFACE_CONTAINER,
                content=ft.Row(
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        ft.Column(
                            spacing=2,
                            tight=True,
                            controls=[
                                ft.Text(
                                    conversation.get("title", "Untitled"),
                                    size=14,
                                    max_lines=1,
                                    overflow=ft.TextOverflow.ELLIPSIS,
                                ),
                                ft.Text(
                                    conversation.get("updated", ""),
                                    size=11,
                                    color=ft.Colors.ON_SURFACE_VARIANT,
                                ),
                            ],
                        ),
                        ft.Row(
                            spacing=4,
                            controls=[
                                ft.TextButton(
                                    "Open",
                                    on_click=lambda e, cid=conversation["id"]: (
                                        methods.open_conversation(cid)
                                    ),
                                ),
                                ft.IconButton(
                                    ft.Icons.DELETE_OUTLINE,
                                    icon_size=18,
                                    tooltip="Delete",
                                    on_click=lambda e, cid=conversation["id"]: (
                                        methods.delete_conversation(cid)
                                    ),
                                ),
                            ],
                        ),
                    ],
                ),
            )
        )

    if not rows:
        rows.append(
            ft.Container(
                alignment=ft.Alignment.CENTER,
                padding=24,
                content=ft.Column(
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    spacing=8,
                    controls=[
                        ft.Icon(
                            ft.Icons.HISTORY_ROUNDED,
                            size=40,
                            color=ft.Colors.ON_SURFACE_VARIANT,
                        ),
                        ft.Text(
                            "No saved conversations yet.",
                            size=14,
                            color=ft.Colors.ON_SURFACE_VARIANT,
                        ),
                    ],
                ),
            )
        )

    return ft.Container(
        expand=True,
        scroll=ft.ScrollMode.AUTO,
        padding=16,
        content=ft.Column(
            spacing=12,
            controls=[
                ft.Row(
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        ft.Text("History", size=24, weight=ft.FontWeight.W_600),
                        ft.Row(
                            spacing=4,
                            controls=[
                                ft.TextButton(
                                    "New chat", on_click=lambda _: methods.new_conversation()
                                ),
                                ft.TextButton(
                                    "Clear all",
                                    style=ft.ButtonStyle(color=ft.Colors.ERROR),
                                    on_click=lambda _: methods.clear_history(),
                                ),
                            ],
                        ),
                    ],
                ),
                *rows,
                build_banner_ad(),
            ],
        ),
    )
