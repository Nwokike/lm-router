"""About dialog: version, build, engine provenance and licences.

Opened by tapping the version chip in the header, which previously did
nothing at all. Also reachable from Settings > About.
"""

from __future__ import annotations

import flet as ft

from core import constants, theme, tokens


def _row(label: str, value: str, is_dark: bool) -> ft.Control:
    return ft.Row(
        spacing=tokens.SPACE_SM,
        controls=[
            ft.Text(label, size=tokens.FONT_XS, color=theme.dim(is_dark), expand=True),
            ft.Text(
                value,
                size=tokens.FONT_BODY_SM,
                color=theme.text_color(is_dark),
                selectable=True,
            ),
        ],
    )


def build_about_dialog(
    page: ft.Page,
    state,
    methods,
) -> ft.AlertDialog:
    """The app's identity card. Everything here is locally verifiable."""
    is_dark = theme.is_dark_mode(page, state.theme_mode)
    engine_source = state.gateway_source or "not started"
    gateway = "running" if state.gateway_running else "stopped"

    licences = "\n".join(
        [
            "Flet · flet-ads · flet-cli (Apache-2.0)",
            "kani (MIT)",
            "mcp Python SDK (MIT)",
            "Kiri Router gateway — router.kiri.ng (stdlib only)",
        ],
    )

    def _open(event: object) -> None:
        methods.open_url(constants.GITHUB_RELEASE_URL)

    def _check(event: object) -> None:
        methods.check_update()

    return ft.AlertDialog(
        modal=True,
        title=ft.Row(
            spacing=tokens.SPACE_SM,
            controls=[
                ft.Icon(ft.Icons.ROUTER_ROUNDED, color=theme.PRIMARY),
                ft.Text(
                    f"{constants.APP_NAME} {constants.APP_VERSION}",
                    weight=ft.FontWeight.BOLD,
                ),
            ],
        ),
        content=ft.Container(
            width=tokens.DIALOG_WIDTH_MD,
            content=ft.Column(
                spacing=tokens.SPACE_SM,
                tight=True,
                controls=[
                    _row("Version", constants.APP_VERSION, is_dark),
                    _row("Build", str(constants.BUILD_NUMBER), is_dark),
                    _row("Gateway", f"{gateway} · {engine_source}", is_dark),
                    ft.Divider(height=1, color=theme.border(is_dark)),
                    theme.markdown(licences, is_dark=is_dark, selectable=True),
                ],
            ),
        ),
        actions=[
            ft.TextButton("Releases", on_click=_open),
            ft.TextButton("Check for updates", on_click=_check),
            ft.FilledButton("Close", on_click=lambda _e: page.pop_dialog()),
        ],
    )
