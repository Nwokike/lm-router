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
    show_quit: bool = False,
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
                width=tokens.LOGO_SIZE,
                height=tokens.LOGO_SIZE,
                color=ft.Colors.WHITE if is_dark else None,
                fit=ft.BoxFit.CONTAIN,
            ),
            width=tokens.LOGO_SIZE,
            height=tokens.LOGO_SIZE,
            alignment=ft.Alignment.CENTER,
        ),
    ]
    if title:
        title_controls: list = [
            ft.Text(
                title,
                size=tokens.FONT_LG,
                weight=ft.FontWeight.BOLD,
                font_family=theme.FONT,
            ),
        ]
        if subtitle:
            title_controls.append(
                ft.Text(
                    subtitle,
                    size=tokens.FONT_XS,
                    color=ft.Colors.ON_SURFACE_VARIANT,
                    font_family=theme.FONT,
                ),
            )
        left.append(ft.Column(controls=title_controls, spacing=tokens.SPACE_XXS, tight=True))

    right: list = list(extra_actions or [])
    if state.update_info:
        update_version = str(state.update_info.get("version", "new"))
        right.append(
            ft.Container(
                content=ft.Row(
                    spacing=tokens.SPACE_TIGHT,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        ft.Text(
                            f"Update: {update_version}",
                            size=tokens.FONT_XS,
                            weight=ft.FontWeight.BOLD,
                            color=ft.Colors.PRIMARY,
                            no_wrap=True,
                        ),
                        ft.Container(
                            width=tokens.DOT_SIZE,
                            height=tokens.DOT_SIZE,
                            border_radius=tokens.DOT_RADIUS,
                            bgcolor=ft.Colors.PRIMARY,
                        ),
                    ],
                ),
                padding=ft.Padding(
                    tokens.SPACE_SNUG,
                    tokens.SPACE_XS,
                    tokens.SPACE_SNUG,
                    tokens.SPACE_XS,
                ),
                border_radius=tokens.RADIUS_CHIP,
                bgcolor=ft.Colors.with_opacity(tokens.OPACITY_STRONG, ft.Colors.PRIMARY),
                border=ft.Border.all(1.5, ft.Colors.PRIMARY),
                ink=True,
                tooltip="New update available",
                on_click=lambda _: methods.open_update_dialog(),
            ),
        )
    else:
        right.append(
            ft.Container(
                content=ft.Text(
                    f"v{constants.APP_VERSION}",
                    size=tokens.FONT_XS,
                    weight=ft.FontWeight.BOLD,
                    color=ft.Colors.ON_SURFACE_VARIANT,
                    no_wrap=True,
                ),
                padding=ft.Padding(
                    tokens.SPACE_SNUG,
                    tokens.SPACE_XS,
                    tokens.SPACE_SNUG,
                    tokens.SPACE_XS,
                ),
                border_radius=tokens.RADIUS_CHIP,
                bgcolor=ft.Colors.with_opacity(tokens.OPACITY_LIGHT, ft.Colors.ON_SURFACE_VARIANT),
                tooltip=f"{constants.APP_NAME} {constants.APP_VERSION} — tap for details",
                # Only Container is tappable in flet 1.0; this chip used to be
                # inert, so tapping the version did nothing at all.
                ink=True,
                on_click=lambda _: methods.open_about(),
            ),
        )
    right.append(
        ft.IconButton(
            icon=_theme_icon(state.theme_mode),
            icon_size=tokens.ICON_SM + 2,
            on_click=_cycle_theme,
            tooltip="Theme: light / dark / system",
        ),
    )
    if show_settings and on_settings is not None:
        right.append(
            ft.IconButton(
                icon=ft.Icons.SETTINGS_ROUNDED,
                icon_size=tokens.ICON_SM + 2,
                on_click=on_settings,
                tooltip="Settings",
            ),
        )
    if show_quit:
        # Always-present escape hatch: without it, "keep running when closed"
        # means the app looks like it refuses to quit.
        right.append(
            ft.IconButton(
                icon=ft.Icons.POWER_SETTINGS_NEW_ROUNDED,
                icon_size=tokens.ICON_SM + 2,
                # IconButton takes `icon_color`; `color` is not a flet 1.0 property.
                icon_color=ft.Colors.ERROR,
                on_click=lambda _: methods.quit_app(),
                tooltip="Quit LM Router",
            ),
        )

    return ft.Container(
        padding=ft.Padding(
            tokens.SPACE_LG,
            tokens.SPACE_SM,
            tokens.SPACE_LG,
            tokens.SPACE_SM,
        ),
        content=ft.Row(
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Row(
                    left,
                    spacing=tokens.SPACE_XS,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                ft.Row(
                    right,
                    spacing=tokens.SPACE_XXS,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
            ],
            # Owner: header must scroll horizontally when extra action
            # buttons make it overflow narrow screens (instead of clipping).
            scroll=ft.ScrollMode.AUTO,
        ),
    )
