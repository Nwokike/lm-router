"""Uppercase section label above each settings card.

An exact port of Sherlock's section_header: 12px, bold, the accent colour, one
pixel of letter spacing, padded to line up with the card's own left edge.
"""

from __future__ import annotations

import flet as ft

from core import tokens


def section_header(text: str) -> ft.Container:
    return ft.Container(
        content=ft.Text(
            text,
            size=tokens.FONT_SM,
            weight=ft.FontWeight.W_700,
            color=ft.Colors.PRIMARY,
            style=ft.TextStyle(letter_spacing=1),
        ),
        padding=ft.Padding(
            left=tokens.SPACE_LG,
            right=tokens.SPACE_LG,
            top=tokens.SPACE_MD,
            bottom=tokens.SPACE_XS,
        ),
    )
