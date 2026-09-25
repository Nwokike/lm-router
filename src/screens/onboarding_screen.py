"""Onboarding: 3 swipeable slides, first slide shows the app icon (SVG),
terms gate on the last slide. Structure follows Sherlock's gesture pager;
slide one uses the use_app_icon tint rule Sherlock uses for dark mode.
"""

import asyncio
import contextlib

import flet as ft
from flet import Control

from core import theme, tokens
from core.state import AppStateCtx
from state.controller_ctx import ControllerMethodsCtx

_PRIVACY_URL = "https://kiri.ng/privacy"
_TERMS_URL = "https://kiri.ng/terms"

# The tinted ring around the slide icon. No token at this width yet
# (missing: ICON_BACKDROP_RING), named once here instead of thrice inline.
_ICON_BACKDROP_RING = 54

# Page-indicator dots: the active pill is four dot-widths long, the inactive
# dot is dot-height. No token at 4 for the corner radius (DOT_RADIUS is 3).
_ACTIVE_DOT_WIDTH = tokens.DOT_SIZE * 4
_DOT_HEIGHT = tokens.SPACE_SM
_DOT_RADIUS = 4

# Swipe velocity (logical px/s) that counts as a deliberate flick.
_SWIPE_VELOCITY = 200

# Strong references so haptic asyncio tasks are never garbage-collected
# mid-flight (RUF006): fire-and-forget tasks must outlive their frame.
_background_tasks: set[asyncio.Task] = set()

_SLIDES = [
    {
        "use_app_icon": True,
        "icon": None,
        "color": theme.PRIMARY,
        "title": "Free models,\nright here",
        "body": (
            "Chat with free models through your own local gateway. "
            "No account, no API key, no credits."
        ),
    },
    {
        "icon": ft.Icons.CHAT_ROUNDED,
        "color": theme.PRIMARY,
        "title": "Any tool,\nany model",
        "body": (
            "OpenAI compatible: use Cursor, Cline, Claude Code or any SDK. "
            "Streaming replies, web search, stop anytime, token counts."
        ),
    },
    {
        "icon": ft.Icons.PRIVACY_TIP_ROUNDED,
        "color": theme.PRIMARY,
        "title": "Your data,\nyour device",
        "body": (
            "Your chat history and any provider keys you add stay on this "
            "device, in this app's own storage. Prompts are sent to free "
            "third-party model providers, which may log them. See our "
            "Privacy Policy."
        ),
    },
]


def _build_slide(s: dict) -> ft.Column:
    from core.state import state as app_state

    page = getattr(ft.context, "page", None)
    is_dark = theme.is_dark_mode(page, app_state.theme_mode)
    if s.get("use_app_icon"):
        icon_content = ft.Image(
            src="/icon.svg",
            width=tokens.ICON_FEATURE,
            height=tokens.ICON_FEATURE,
            color=ft.Colors.WHITE if is_dark else None,
        )
        bg_color = ft.Colors.with_opacity(
            tokens.OPACITY_TINT,
            ft.Colors.WHITE if is_dark else theme.PRIMARY,
        )
    else:
        icon_content = ft.Icon(s["icon"], size=tokens.ICON_FEATURE, color=s["color"])
        bg_color = ft.Colors.with_opacity(tokens.OPACITY_LIGHT, s["color"])
    return ft.Column(
        [
            ft.Container(
                content=icon_content,
                width=tokens.ICON_FEATURE + _ICON_BACKDROP_RING,
                height=tokens.ICON_FEATURE + _ICON_BACKDROP_RING,
                border_radius=(tokens.ICON_FEATURE + _ICON_BACKDROP_RING) // 2,
                bgcolor=bg_color,
                alignment=ft.Alignment.CENTER,
            ),
            ft.Container(height=tokens.SPACE_XL),
            ft.Text(
                s["title"],
                size=tokens.FONT_XXL,
                weight=ft.FontWeight.W_800,
                text_align=ft.TextAlign.CENTER,
                font_family=theme.FONT,
            ),
            ft.Container(height=tokens.SPACE_MD),
            ft.Text(
                s["body"],
                size=tokens.FONT_MD,
                color=ft.Colors.ON_SURFACE_VARIANT,
                text_align=ft.TextAlign.CENTER,
                font_family=theme.FONT,
            ),
        ],
        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        alignment=ft.MainAxisAlignment.CENTER,
        spacing=0,
    )


@ft.component
def OnboardingScreen() -> Control:
    controller = ft.use_context(ControllerMethodsCtx)
    # Subscribe this screen too: like Sherlock/DDGS/ktv-player, flip the
    # observable HERE (in the tap) so the context-owned shell re-renders;
    # the controller then persists it.
    app_state = ft.use_context(AppStateCtx)
    page_idx, set_page_idx = ft.use_state(0)
    agreed, set_agreed = ft.use_state(False)
    show_hint, set_show_hint = ft.use_state(False)

    is_last = page_idx == len(_SLIDES) - 1

    def _haptic() -> None:
        with contextlib.suppress(Exception):
            task = asyncio.create_task(ft.HapticFeedback().light_impact())
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)

    def _finish() -> None:
        # Flip the observable HERE (in the tap) so this screen's context
        # notifies the shell to re-render, then the controller persists it
        # and forces page.update (ffmpeg pattern).
        app_state.onboarding_done = True
        app_state.terms_accepted = True
        controller.finish_onboarding()

    def _on_next(e: object = None) -> None:
        _haptic()
        if is_last:
            if not agreed:
                set_show_hint(True)
                return
            _finish()
            return
        set_page_idx(page_idx + 1)

    def _on_skip(e: object) -> None:
        _haptic()
        set_show_hint(True)  # skipping still requires agreeing on last slide
        if agreed:
            _finish()
        else:
            set_page_idx(len(_SLIDES) - 1)

    def _on_swipe(e: ft.DragEndEvent) -> None:
        velocity = getattr(e, "primary_velocity", None)
        if velocity is None:
            return
        if velocity < -_SWIPE_VELOCITY and not is_last:
            _haptic()
            set_page_idx(min(page_idx + 1, len(_SLIDES) - 1))
        elif velocity > _SWIPE_VELOCITY and page_idx > 0:
            _haptic()
            set_page_idx(page_idx - 1)

    def _on_dot_click(idx: int) -> None:
        _haptic()
        set_page_idx(idx)

    dots = []
    for i in range(len(_SLIDES)):
        active = i == page_idx
        dot = ft.Container(
            width=_ACTIVE_DOT_WIDTH if active else _DOT_HEIGHT,
            height=_DOT_HEIGHT,
            border_radius=_DOT_RADIUS,
            bgcolor=ft.Colors.PRIMARY
            if active
            else ft.Colors.with_opacity(tokens.OPACITY_LIGHT, ft.Colors.ON_SURFACE),
            animate=ft.Animation(tokens.ANIM_SLOW, "easeOut"),
        )
        dots.append(
            ft.GestureDetector(
                content=dot,
                on_tap=lambda e, idx=i: _on_dot_click(idx),
            ),
        )

    terms_row = ft.Row(
        spacing=tokens.SPACE_XS,
        alignment=ft.MainAxisAlignment.CENTER,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
        controls=[
            ft.Checkbox(value=agreed, on_change=lambda e: set_agreed(bool(e.control.value))),
            ft.Text(
                "I agree to the ",
                size=tokens.FONT_BODY_SM,
                spans=[
                    ft.TextSpan(
                        "Privacy Policy",
                        style=ft.TextStyle(color=theme.PRIMARY, weight=ft.FontWeight.W_600),
                        on_click=lambda e: controller.open_url(_PRIVACY_URL),
                    ),
                    ft.TextSpan(" & "),
                    ft.TextSpan(
                        "Terms of Service",
                        style=ft.TextStyle(color=theme.PRIMARY, weight=ft.FontWeight.W_600),
                        on_click=lambda e: controller.open_url(_TERMS_URL),
                    ),
                ],
            ),
        ],
    )

    hint = ft.Text(
        "Accept the Privacy Policy and Terms to continue.",
        size=tokens.FONT_XS,
        color=ft.Colors.ERROR,
        text_align=ft.TextAlign.CENTER,
        visible=show_hint and not agreed,
    )

    return ft.Container(
        expand=True,
        gradient=ft.LinearGradient(
            begin=ft.Alignment.TOP_CENTER,
            end=ft.Alignment.BOTTOM_CENTER,
            colors=[
                ft.Colors.SURFACE,
                ft.Colors.with_opacity(tokens.OPACITY_FAINT, theme.PRIMARY),
            ],
        ),
        content=ft.Column(
            expand=True,
            spacing=0,
            controls=[
                ft.Container(
                    padding=ft.Padding(tokens.SPACE_LG, tokens.SPACE_MD, tokens.SPACE_LG, 0),
                    content=ft.Row(
                        alignment=ft.MainAxisAlignment.END,
                        controls=[
                            ft.TextButton(
                                "Skip",
                                visible=not is_last,
                                on_click=_on_skip,
                                style=ft.ButtonStyle(color=ft.Colors.ON_SURFACE_VARIANT),
                            ),
                        ],
                    ),
                ),
                ft.Container(
                    expand=True,
                    alignment=ft.Alignment.CENTER,
                    content=ft.GestureDetector(
                        content=ft.Container(
                            content=_build_slide(_SLIDES[page_idx]),
                            alignment=ft.Alignment.CENTER,
                            padding=ft.Padding(tokens.SPACE_XL, 0, tokens.SPACE_XL, 0),
                        ),
                        on_horizontal_drag_end=_on_swipe,
                    ),
                ),
                ft.Container(
                    padding=ft.Padding(tokens.SPACE_LG, 0, tokens.SPACE_LG, tokens.SPACE_LG),
                    content=ft.Column(
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                        spacing=tokens.SPACE_MD,
                        controls=[
                            ft.Row(
                                controls=dots,
                                alignment=ft.MainAxisAlignment.CENTER,
                                spacing=tokens.SPACE_SM,
                            ),
                            terms_row if is_last else ft.Container(),
                            hint,
                            ft.FilledButton(
                                content=ft.Text(
                                    "Get Started" if is_last else "Next",
                                    size=tokens.FONT_MD,
                                    weight=ft.FontWeight.W_600,
                                    font_family=theme.FONT,
                                    color=ft.Colors.WHITE,
                                ),
                                icon=ft.Icons.CHECK_ROUNDED
                                if is_last
                                else ft.Icons.ARROW_FORWARD_ROUNDED,
                                on_click=_on_next,
                                width=tokens.CTA_BUTTON_WIDTH,
                                height=tokens.CTA_BUTTON_HEIGHT,
                                style=ft.ButtonStyle(
                                    shape=ft.RoundedRectangleBorder(radius=tokens.RADIUS_XL),
                                ),
                            ),
                        ],
                    ),
                ),
            ],
        ),
    )
