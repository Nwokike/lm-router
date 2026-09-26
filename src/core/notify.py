"""Scroll-independent message surface (port of Sherlock's core/notify.py).

Notices are SnackBars only. A banner rendered inside the shell is only
readable where the shell shows it: mid-conversation, during onboarding, or
on another screen it can be entirely off-screen, and the old top-of-header
notice bar duplicated every message (banner plus snack) until manually
dismissed. An overlay is visible no matter where the user is.

`page.snack_bar` does not exist in Flet 1.0 (verified against the installed
package). The supported path is `page.show_dialog(...)`, and `ft.SnackBar` is
a `DialogControl` (`.venv/Lib/site-packages/flet/controls/material/snack_bar.py`),
so it can be shown that way.

One edge case matters: `show_dialog` raises `RuntimeError("Dialog is already
opened")` when a dialog is still on the stack. The runtime here pops the
lingering SnackBar and re-shows, but never closes a real (non-snack) dialog.
Everything is best-effort: a failed toast must never take the app down.
"""

from __future__ import annotations

import flet as ft

from .logging import LOG

# One text color for every toast; the background carries the severity.
_TEXT_COLOR = ft.Colors.WHITE
_DEFAULT_BG = "#1A1D22"  # theme.DARK_SURFACE: readable in both page themes


def show_snack(
    page: ft.Page,
    message: str,
    bgcolor: str | None = None,
    duration: int = 4000,
    floating: bool = False,
) -> None:
    """Best-effort snackbar. Logs failures, never raises."""
    if page is None or not message:
        return
    try:
        snack = ft.SnackBar(
            content=ft.Text(message, color=_TEXT_COLOR),
            bgcolor=bgcolor or _DEFAULT_BG,
            duration=duration,
            behavior=ft.SnackBarBehavior.FLOATING if floating else None,
        )
        try:
            page.show_dialog(snack)
        except RuntimeError:
            # Something is already on the dialog stack. Only a lingering
            # SnackBar may be replaced; a real dialog (About, update, logs)
            # belongs to the user and is left alone.
            popped = page.pop_dialog()
            if popped is None or isinstance(popped, ft.SnackBar):
                page.show_dialog(snack)
    except Exception as exc:
        LOG.warning("show_snack failed: %s", exc)
