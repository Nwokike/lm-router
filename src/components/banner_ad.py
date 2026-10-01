"""BannerAd builder (Sherlock port): glass card, mobile only, full width."""

import contextlib

import flet as ft
from flet import Control

from core import constants, theme, tokens
from core.logging import LOG
from core.state import state

try:
    import flet_ads as fta

    _HAS_ADS = True
except ImportError:
    _HAS_ADS = False


def build_banner_ad(page: ft.Page | None = None) -> Control:
    """320x50 banner inside a full-width card; zero-size off mobile.

    The inner ad stays at its native AdSize; the outer Row stretches the
    glass card edge to edge on every screen (Sherlock's sizing note: never
    set alignment on a wide Container).
    """
    if page is None:
        try:
            from flet import context

            page = context.page
        except Exception:
            return ft.Container(width=0, height=0)

    if not page or not hasattr(page, "platform"):
        return ft.Container(width=0, height=0)
    try:
        if not page.platform.is_mobile():
            return ft.Container(width=0, height=0)
    except Exception:
        return ft.Container(width=0, height=0)

    is_dark = theme.is_dark_mode(page, state.theme_mode)

    if not _HAS_ADS:
        return ft.Container(width=0, height=0)

    # Single construction (a leftover duplicate built the ad twice — double
    # request, double platform view). The error handler hides the WHOLE
    # glass card, not just the creative: hiding only the inner holder left
    # the empty rounded-rectangle chrome in the thread.
    outer: ft.Row | None = None

    def _on_ad_error(e) -> None:
        LOG.warning("ads: banner load error: %s", getattr(e, "data", e))
        if outer is not None:
            outer.visible = False
            with contextlib.suppress(Exception):
                outer.update()

    try:
        ad = fta.BannerAd(
            unit_id=constants.AD_BANNER_UNIT_ID_ANDROID,
            width=320,
            height=50,
            on_load=lambda e: LOG.debug("ads: banner loaded"),
            on_error=_on_ad_error,
        )
    except Exception as exc:
        LOG.warning("ads: banner construction failed: %s", exc)
        return ft.Container(width=0, height=0)

    # Sherlock styling: an adaptive glass card (low-alpha overlay + hairline
    # border) rather than a flat tonal surface, so it reads as chrome on both
    # the slate dark background and the light one.
    holder = ft.Container(content=ad, width=320, height=50)
    glass = ft.Container(
        expand=True,
        padding=tokens.SPACE_SM,
        border_radius=tokens.RADIUS_LG,
        bgcolor=theme.glass(is_dark),
        border=ft.Border.all(1, theme.border(is_dark)),
        content=ft.Column(
            [holder],
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            spacing=tokens.SPACE_XS,
            tight=True,
        ),
    )
    outer = ft.Row(controls=[glass], alignment=ft.MainAxisAlignment.CENTER)
    return outer
