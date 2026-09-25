"""History screen: saved conversations with search, relative timestamps, and export."""

import flet as ft

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
                padding=tokens.SPACE_MD,
                border_radius=tokens.RADIUS_MD,
                bgcolor=ft.Colors.SURFACE_CONTAINER,
                content=ft.Row(
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        ft.Column(
                            spacing=tokens.SPACE_XXS,
                            tight=True,
                            expand=True,
                            controls=[
                                ft.Text(
                                    conversation.get("title", "Untitled"),
                                    size=tokens.FONT_MD,
                                    max_lines=1,
                                    overflow=ft.TextOverflow.ELLIPSIS,
                                ),
                                ft.Text(
                                    time_display,
                                    size=tokens.FONT_XS,
                                    color=ft.Colors.ON_SURFACE_VARIANT,
                                ),
                            ],
                        ),
                        ft.Row(
                            spacing=tokens.SPACE_XS,
                            controls=[
                                ft.TextButton(
                                    "Open",
                                    on_click=lambda e, c=cid: methods.open_conversation(c),
                                ),
                                ft.IconButton(
                                    ft.Icons.IOS_SHARE,
                                    icon_size=tokens.ICON_SM,
                                    tooltip="Export Markdown",
                                    on_click=lambda e, c=cid: methods.export_conversation(c),
                                ),
                                ft.IconButton(
                                    ft.Icons.DELETE_OUTLINE,
                                    icon_size=tokens.ICON_SM,
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
                padding=tokens.SPACE_XL,
                content=ft.Column(
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    spacing=tokens.SPACE_SM,
                    controls=[
                        ft.Icon(
                            ft.Icons.HISTORY_ROUNDED,
                            size=tokens.ICON_XL,
                            color=ft.Colors.ON_SURFACE_VARIANT,
                        ),
                        ft.Text(
                            empty_msg,
                            size=tokens.FONT_MD,
                            color=ft.Colors.ON_SURFACE_VARIANT,
                        ),
                    ],
                ),
            ),
        )

    return ft.Container(
        expand=True,
        padding=ft.Padding.symmetric(
            horizontal=tokens.SPACE_MD,
            vertical=tokens.SPACE_SM,
        ),
        content=ft.Column(
            spacing=tokens.SPACE_MD,
            scroll=ft.ScrollMode.AUTO,
            controls=[
                ft.Row(
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        # 24px sits between FONT_LG (20) and FONT_XXL (28);
                        # no token matches, so the literal stays.
                        ft.Text("History", size=24, weight=ft.FontWeight.W_600),
                        ft.Row(
                            spacing=tokens.SPACE_XS,
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
                    text_size=tokens.FONT_BODY_SM,
                    on_change=lambda e: set_query(str(e.control.value or "")),
                ),
                *rows,
                # No banner at the floor: the owner banned them there.
            ],
        ),
    )
