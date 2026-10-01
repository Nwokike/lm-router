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

    def _confirm(title: str, message: str, confirm_label: str, action) -> None:
        # Irreversible deletes go behind an AlertDialog: one misclick used to
        # wipe a conversation (or ALL of them) with no undo.
        page = getattr(ft.context, "page", None)
        if page is None:
            action()
            return

        def _do(_=None) -> None:
            page.pop_dialog()
            action()

        page.show_dialog(
            ft.AlertDialog(
                modal=True,
                title=ft.Text(title),
                content=ft.Text(message),
                actions=[
                    ft.TextButton("Cancel", on_click=lambda _: page.pop_dialog()),
                    ft.TextButton(confirm_label, on_click=_do),
                ],
            )
        )

    rows: list[ft.Control] = []
    for conversation in filtered_conversations:
        cid = str(conversation.get("id") or "")
        if not cid:
            continue
        title = conversation.get("title") or "Untitled"
        rel = conversation.get("relative", "")
        updated = conversation.get("updated", "")
        time_display = f"{rel} · {updated}" if rel and updated else (rel or updated)
        is_active = cid == state.active_conversation

        body: list[ft.Control] = [
            ft.Text(
                title,
                size=tokens.FONT_MD,
                max_lines=1,
                overflow=ft.TextOverflow.ELLIPSIS,
            ),
        ]
        if time_display:
            body.append(
                ft.Text(
                    time_display,
                    size=tokens.FONT_XS,
                    color=ft.Colors.ON_SURFACE_VARIANT,
                ),
            )
        rows.append(
            ft.Container(
                padding=tokens.SPACE_MD,
                border_radius=tokens.RADIUS_MD,
                bgcolor=ft.Colors.SURFACE_CONTAINER,
                # Active-conversation affordance: the list used to look
                # unchanged after Open.
                border=ft.Border.all(1, ft.Colors.PRIMARY) if is_active else None,
                # Whole-card tap opens: only the "Open" text used to work.
                ink=True,
                on_click=lambda e, c=cid: methods.open_conversation(c),
                content=ft.Row(
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        ft.Column(
                            spacing=tokens.SPACE_XXS,
                            expand=True,
                            controls=body,
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
                                    on_click=lambda e, c=cid, t=title: _confirm(
                                        "Delete conversation?",
                                        f'"{t}" will be permanently deleted.',
                                        "Delete",
                                        lambda: methods.delete_conversation(c),
                                    ),
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
                                    on_click=lambda _: _confirm(
                                        "Clear all conversations?",
                                        f"{len(filtered_conversations)} conversation(s) "
                                        "will be permanently deleted.",
                                        "Clear all",
                                        methods.clear_history,
                                    ),
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
