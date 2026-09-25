"""Settings card and row helpers, ported from Sherlock's settings screen.

These two components are what make that screen read as designed rather than as
a stack of default Material widgets: an outlined card with hairline dividers
between rows, and a row with a circular icon backdrop, a title/subtitle block,
and the trailing control — which drops to its own line below 600px so nothing
overflows on a phone.
"""

from __future__ import annotations

from collections.abc import Callable

import flet as ft

from core import tokens


def settings_card(controls: list[ft.Control]) -> ft.Container:
    """A bordered group of rows with hairline separators, Sherlock's `_settings_card`."""
    rows: list[ft.Control] = []
    for index, control in enumerate(controls):
        if index:
            rows.append(
                ft.Divider(
                    height=1,
                    color=ft.Colors.with_opacity(tokens.OPACITY_SUBTLE, ft.Colors.OUTLINE),
                ),
            )
        rows.append(control)
    return ft.Container(
        # No `tight`: a tight Column hugs its children's intrinsic width and
        # the card renders narrow. Sherlock's `_settings_card` lets the Column
        # stretch so the card fills the available width.
        content=ft.Column(controls=rows, spacing=0),
        margin=ft.Margin(tokens.SPACE_XL, 0, tokens.SPACE_XL, tokens.SPACE_SM),
        border_radius=tokens.RADIUS_LG,
        bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
        border=ft.Border.all(
            1,
            ft.Colors.with_opacity(tokens.OPACITY_SUBTLE, ft.Colors.OUTLINE),
        ),
        clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
    )


def setting_row(
    *,
    icon: str,
    title: str,
    subtitle: str = "",
    trailing: ft.Control | None = None,
    stacked: bool = False,
    on_tap: Callable[[], None] | None = None,
) -> ft.Container:
    """One settings row, Sherlock's `_setting_row`.

    On a narrow screen (`stacked=True`) the trailing control moves to a second
    line, indented to line up under the text, instead of squeezing the row.
    """
    icon_box = ft.Container(
        content=ft.Icon(icon, size=tokens.ICON_MD, color=ft.Colors.ON_SURFACE_VARIANT),
        width=tokens.ICON_BACKDROP,
        height=tokens.ICON_BACKDROP,
        border_radius=tokens.ICON_BACKDROP_RADIUS,
        bgcolor=ft.Colors.with_opacity(tokens.OPACITY_LIGHT, ft.Colors.ON_SURFACE),
        alignment=ft.Alignment.CENTER,
    )
    text_controls: list[ft.Control] = [
        ft.Text(title, size=tokens.FONT_MD, weight=ft.FontWeight.W_500),
    ]
    if subtitle:
        text_controls.append(
            ft.Text(
                subtitle,
                size=tokens.FONT_XS,
                color=ft.Colors.with_opacity(tokens.OPACITY_DIM, ft.Colors.ON_SURFACE),
            ),
        )
    text_col = ft.Column(
        controls=text_controls,
        spacing=tokens.SPACE_XXS,
        tight=True,
        expand=True,
    )

    if stacked and trailing is not None:
        content = ft.Column(
            controls=[
                ft.Row(
                    controls=[icon_box, text_col],
                    spacing=tokens.SPACE_MD,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                ft.Row(
                    controls=[
                        ft.Container(width=tokens.ICON_BACKDROP + tokens.SPACE_MD),
                        trailing,
                    ],
                    spacing=tokens.SPACE_XS,
                ),
            ],
            spacing=tokens.SPACE_XS,
        )
    else:
        controls: list[ft.Control] = [icon_box, text_col]
        if trailing is not None:
            controls.append(trailing)
        content = ft.Row(
            controls=controls,
            spacing=tokens.SPACE_MD,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

    return ft.Container(
        content=content,
        padding=ft.Padding(
            left=tokens.SPACE_LG,
            right=tokens.SPACE_LG,
            top=tokens.SPACE_MD,
            bottom=tokens.SPACE_MD,
        ),
        on_click=on_tap,
        ink=on_tap is not None,
    )
