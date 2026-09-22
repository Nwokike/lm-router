"""AppHeader: unified screen header (Sherlock pattern).

Top-left: app icon.svg (tinted white in dark mode) + screen title.
Top-right: version chip + 3-state theme cycle.
"""

import flet as ft

from core import constants, theme, tokens
from core.state import AppStateCtx
from state.controller_ctx import ControllerMethodsCtx


def _theme_icon(mode: str) -> object:
    if mode == "dark":
        return ft.Icons.DARK_MODE_ROUNDED
    if mode == "light":
        return ft.Icons.LIGHT_MODE_ROUNDED
    return ft.Icons.SETTINGS_SYSTEM_DAYDREAM_ROUNDED


def AppHeader(
    *,
    title: str | None = None,
    subtitle: str | None = None,
    show_settings: bool = False,
    on_settings: object | None = None,
    extra_actions: list | None = None,
) -> ft.Container:
    page = getattr(ft.context, "page", None)
    state = ft.use_context(AppStateCtx)
    methods = ft.use_context(ControllerMethodsCtx)

    def _cycle_theme(_: object) -> None:
        order = {"dark": "light", "light": "system", "system": "dark"}
        methods.set_theme(order.get(state.theme_mode, "system"))

    is_dark = theme.is_dark_mode(page, state.theme_mode)
    left: list = [
        ft.Container(
            content=ft.Image(
                src="/icon.svg",
                width=32,
                height=32,
                color=ft.Colors.WHITE if is_dark else None,
                fit=ft.BoxFit.CONTAIN,
            ),
            width=32,
            height=32,
            alignment=ft.Alignment.CENTER,
        ),
    ]
    if title:
        title_controls: list = [
            ft.Text(title, size=tokens.FONT_LG, weight=ft.FontWeight.BOLD, font_family="Outfit")
        ]
        if subtitle:
            title_controls.append(
                ft.Text(
                    subtitle,
                    size=tokens.FONT_XS,
                    color=ft.Colors.ON_SURFACE_VARIANT,
                    font_family="Outfit",
                )
            )
        left.append(ft.Column(controls=title_controls, spacing=tokens.SPACE_XXS, tight=True))

    right: list = list(extra_actions or [])
    right.append(
        ft.Container(
            content=ft.Text(
                f"v{constants.APP_VERSION}",
                size=11,
                weight=ft.FontWeight.BOLD,
                color=ft.Colors.ON_SURFACE_VARIANT,
                no_wrap=True,
            ),
            padding=ft.Padding(10, 4, 10, 4),
            border_radius=10,
            bgcolor=ft.Colors.with_opacity(0.08, ft.Colors.ON_SURFACE_VARIANT),
            tooltip=f"LM Router {constants.APP_VERSION}",
        )
    )
    right.append(
        ft.IconButton(
            icon=_theme_icon(state.theme_mode),
            icon_size=tokens.ICON_SM + 2,
            on_click=_cycle_theme,
            tooltip="Theme: light / dark / system",
        )
    )
    if show_settings and on_settings is not None:
        right.append(
            ft.IconButton(
                icon=ft.Icons.SETTINGS_ROUNDED,
                icon_size=tokens.ICON_SM + 2,
                on_click=on_settings,
                tooltip="Settings",
            )
        )

    return ft.Container(
        padding=ft.Padding(tokens.SPACE_XL, tokens.SPACE_SM, tokens.SPACE_XL, tokens.SPACE_SM),
        content=ft.Row(
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Row(
                    left,
                    spacing=tokens.SPACE_SM,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                ft.Row(
                    right,
                    spacing=tokens.SPACE_XS,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
            ],
        ),
    )
