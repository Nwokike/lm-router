"""History screen: saved conversations with search, relative timestamps, and export."""

import flet as ft

from components.banner_ad import build_banner_ad
from core import tokens
from core.state import AppStateCtx
from state.controller_ctx import ControllerMethodsCtx


@ft.component
def HistoryScreen():
    state = ft.use_context(AppStateCtx)
    methods = ft.use_context(ControllerMethodsCtx)
    query, set_query = ft.use_state("")

    # Filter conversations by search term
    q = query.strip().lower()
    filtered_conversations = [
        c for c in state.conversations if not q or q in c.get("title", "").lower()
    ]

    rows: list[ft.Control] = []
    for conversation in filtered_conversations:
        cid = conversation["id"]
        rel = conversation.get("relative", "")
        updated = conversation.get("updated", "")
        time_display = f"{rel} · {updated}" if rel and updated else (rel or updated)

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
                            expand=True,
                            controls=[
                                ft.Text(
                                    conversation.get("title", "Untitled"),
                                    size=14,
                                    max_lines=1,
                                    overflow=ft.TextOverflow.ELLIPSIS,
                                ),
                                ft.Text(
                                    time_display,
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
                                    on_click=lambda e, c=cid: methods.open_conversation(c),
                                ),
                                ft.IconButton(
                                    ft.Icons.IOS_SHARE,
                                    icon_size=18,
                                    tooltip="Export Markdown",
                                    on_click=lambda e, c=cid: methods.export_conversation(c),
                                ),
                                ft.IconButton(
                                    ft.Icons.DELETE_OUTLINE,
                                    icon_size=18,
                                    tooltip="Delete",
                                    on_click=lambda e, c=cid: methods.delete_conversation(c),
                                ),
                            ],
                        ),
                    ],
                ),
            ),
        )

    if not rows:
        empty_msg = (
            f"No conversations matching '{query}'."
            if query.strip()
            else "No saved conversations yet."
        )
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
                            empty_msg,
                            size=14,
                            color=ft.Colors.ON_SURFACE_VARIANT,
                        ),
                    ],
                ),
            ),
        )

    return ft.Container(
        expand=True,
        padding=16,
        content=ft.Column(
            spacing=12,
            scroll=ft.ScrollMode.AUTO,
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
                                    "New chat",
                                    on_click=lambda _: methods.new_conversation(),
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
                ft.TextField(
                    value=query,
                    hint_text="Search conversations…",
                    prefix_icon=ft.Icons.SEARCH,
                    text_size=13,
                    on_change=lambda e: set_query(str(e.control.value or "")),
                ),
                *rows,
                build_banner_ad(),
            ],
        ),
    )
