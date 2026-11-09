"""Collapsible "Thinking" disclosure block.

Reasoning collapses itself: while a probe is waiting on a thinking model the
block reads "Thinking…"; once the answer lands it shows "Reasoning" collapsed
and stays re-expandable. Reasoning arrives inside the completion's JSON
(`message.reasoning_content` on DeepSeek-style providers) — no streaming,
no kani needed.
"""

from __future__ import annotations

import flet as ft

from core import theme, tokens


def _caret(open_: bool):
    return ft.Icons.EXPAND_LESS_ROUNDED if open_ else ft.Icons.EXPAND_MORE_ROUNDED


@ft.component
def ThinkingBlock(
    *,
    reasoning: str,
    streaming: bool = False,
    is_dark: bool = True,
):
    """The model's reasoning: open while waiting, collapsed once answered."""
    open_, set_open = ft.use_state(streaming)

    # Auto-collapse the instant the answer starts, but never fight the user:
    # only force a close on the streaming -> finished transition.
    # flet's use_effect calls `setup()` with NO arguments, so the previous
    # value is tracked in state rather than passed in.
    was_streaming, set_was_streaming = ft.use_state(streaming)

    def _sync() -> None:
        if was_streaming and not streaming:
            set_open(False)
        set_was_streaming(streaming)

    ft.use_effect(_sync, [streaming])

    if not reasoning:
        return ft.Container()

    header = ft.Container(
        # Only Container carries an on_click in flet 1.0; Row/Column do not.
        ink=True,
        border_radius=tokens.RADIUS_SM,
        padding=ft.Padding.symmetric(
            horizontal=tokens.SPACE_XS,
            vertical=tokens.SPACE_XXS,
        ),
        on_click=lambda _: set_open(not open_),
        content=ft.Row(
            spacing=tokens.SPACE_TIGHT,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Icon(_caret(open_), size=tokens.ICON_XS, color=theme.dim(is_dark)),
                ft.Icon(
                    ft.Icons.PSYCHOLOGY_ROUNDED,
                    size=tokens.ICON_XS,
                    color=theme.PRIMARY if streaming else theme.dim(is_dark),
                ),
                ft.Text(
                    "Thinking…" if streaming else "Reasoning",
                    size=tokens.FONT_XS,
                    weight=ft.FontWeight.W_600,
                    color=theme.dim(is_dark),
                ),
            ],
        ),
    )

    body: list[ft.Control] = [header]
    if open_:
        body.append(
            ft.Container(
                padding=ft.Padding.only(
                    left=tokens.SPACE_SNUG,
                    top=tokens.SPACE_XS,
                    bottom=tokens.SPACE_XS,
                ),
                margin=ft.Margin.only(top=tokens.SPACE_XXS),
                border=ft.Border.only(
                    left=ft.BorderSide(
                        2,
                        ft.Colors.with_opacity(tokens.OPACITY_EMPHASIS, theme.PRIMARY),
                    ),
                ),
                content=theme.markdown(reasoning, is_dark=is_dark),
            ),
        )

    return ft.Container(
        padding=ft.Padding.symmetric(
            horizontal=tokens.SPACE_XXS,
            vertical=tokens.SPACE_XXS,
        ),
        bgcolor=theme.glass(is_dark, tokens.OPACITY_SUBTLE),
        border_radius=tokens.RADIUS_MD,
        on_click=None,
        content=ft.Column(spacing=tokens.SPACE_XXS, tight=True, controls=body),
    )
