"""Update / release notes dialog (FFmpeg port): GITHUB_WEB markdown body."""

import flet as ft

from core import constants, theme, tokens
from core.logging import LOG

_RELEASE_NOTES_FALLBACK = "See the GitHub releases page for the change log."


def build_update_dialog(
    page: ft.Page,
    update_data: dict | None = None,
    url_launcher: object | None = None,
) -> ft.AlertDialog:
    is_update = update_data is not None
    title = (update_data or {}).get("title") or f"{constants.APP_NAME} {constants.APP_VERSION}"
    notes = (update_data or {}).get("release_notes") or _RELEASE_NOTES_FALLBACK
    is_mandatory = bool((update_data or {}).get("mandatory", False))

    def _launch(url: str) -> None:
        # Flet1: URLs open via the registered UrlLauncher service (async).
        if url_launcher is None:
            LOG.warning("url launcher unavailable; cannot open %s", url)
            return
        page.run_task(url_launcher.launch_url, url)

    def _open_download(_: object) -> None:
        url = (
            constants.PLAYSTORE_URL
            if page.platform and page.platform.is_mobile() and constants.PLAYSTORE_URL
            else constants.GITHUB_RELEASE_URL
        )
        _launch(url)

    actions = [
        ft.FilledButton(
            "Download Update" if is_update else "View on GitHub",
            on_click=_open_download,
        ),
    ]
    if not is_mandatory:
        actions.append(ft.TextButton("Close", on_click=lambda _: page.pop_dialog()))

    return ft.AlertDialog(
        modal=is_mandatory,
        title=ft.Text(title, weight=ft.FontWeight.BOLD),
        content=ft.Container(
            width=tokens.DIALOG_WIDTH_MD,
            height=tokens.DIALOG_HEIGHT_SM,
            content=ft.Column(
                scroll=ft.ScrollMode.AUTO,
                controls=[
                    theme.markdown(
                        notes,
                        is_dark=theme.is_dark_mode(page, "system"),
                        on_tap_link=lambda e: _launch(e.data),
                    ),
                ],
            ),
        ),
        actions=actions,
        actions_alignment=ft.MainAxisAlignment.END,
        shape=ft.RoundedRectangleBorder(radius=tokens.RADIUS_LG),
    )
