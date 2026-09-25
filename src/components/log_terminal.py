"""Live activity log terminal.

Modelled on Sherlock's terminal dialog. The log used to live inline in the
Server page inside its own ListView, which was two bugs at once: a nested
scrollable swallowed the page's scroll gestures (so the sections below it were
unreachable), and each line was `max_lines=1, overflow=CLIP`, so the text was
visually truncated and selecting it copied fragments.

A dialog fixes both: one scrollable region, one selectable Text that wraps, and
the page behind it scrolls normally again.
"""

from __future__ import annotations

import flet as ft

from core import theme, tokens
from core.logging import records

_DIALOG_WIDTH = 560
_DIALOG_HEIGHT = 420
_MAX_LINES = 500


def build_log_terminal(
    page: ft.Page,
    state,
    methods,
) -> ft.AlertDialog:
    """Read-only terminal: refresh, copy all, close."""
    lines = [f"{r['ts']}  {r.get('level', 'INFO'):<7}  {r['msg']}" for r in records()[-_MAX_LINES:]]
    body = "\n".join(lines) or "No log output yet."

    return ft.AlertDialog(
        modal=False,
        title=ft.Row(
            spacing=tokens.SPACE_SM,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Icon(
                    ft.Icons.TERMINAL_ROUNDED,
                    color=theme.PRIMARY,
                    size=tokens.ICON_MD,
                ),
                ft.Text(
                    "Activity log",
                    size=tokens.FONT_MD,
                    weight=ft.FontWeight.BOLD,
                    color=theme.PRIMARY,
                    expand=True,
                ),
                ft.IconButton(
                    icon=ft.Icons.COPY_ROUNDED,
                    tooltip="Copy the whole log",
                    icon_size=18,
                    icon_color=theme.PRIMARY,
                    on_click=lambda _e: methods.copy_logs(),
                ),
            ],
        ),
        content=ft.Container(
            width=_DIALOG_WIDTH,
            height=_DIALOG_HEIGHT,
            bgcolor="#101418",
            border=ft.Border.all(
                1,
                ft.Colors.with_opacity(0.12, ft.Colors.WHITE),
            ),
            border_radius=tokens.RADIUS_MD,
            padding=tokens.SPACE_MD,
            # ONE scrollable. A single selectable Text wraps long lines, so
            # copying gets the whole log rather than clipped fragments.
            content=ft.Column(
                [
                    ft.Text(
                        body,
                        selectable=True,
                        font_family="monospace",
                        size=12,
                        color="#D7DEE7",
                    ),
                ],
                scroll=ft.ScrollMode.AUTO,
                spacing=0,
            ),
        ),
        actions=[
            ft.TextButton("Close", on_click=lambda _e: page.pop_dialog()),
        ],
        actions_alignment=ft.MainAxisAlignment.END,
    )
