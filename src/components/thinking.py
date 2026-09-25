"""Collapsible "Thinking" and tool-call disclosure blocks.

Two rules the chat UI depends on:

* **Reasoning collapses itself.** While a model is still thinking the block is
  open so you can watch it; the moment the answer starts it closes, and it stays
  re-expandable for the rest of the conversation.
* **Tool calls are quiet by default.** A one-line row (icon, name, status,
  duration) with the raw arguments/result hidden behind an expander, so a
  search returning 8k characters never floods the transcript.
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
    """The model's reasoning: open while streaming, collapsed once answered."""
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


@ft.component
def ToolCallBlock(
    *,
    name: str,
    content: str,
    is_error: bool = False,
    is_dark: bool = True,
):
    """One compact row per tool call; the payload is hidden until expanded."""
    open_, set_open = ft.use_state(False)
    status_color = theme.ERROR if is_error else theme.SUCCESS

    header = ft.Container(
        ink=True,
        border_radius=tokens.RADIUS_SM,
        padding=ft.Padding.symmetric(
            horizontal=tokens.SPACE_XXS,
            vertical=tokens.SPACE_XXS,
        ),
        on_click=lambda _: set_open(not open_),
        content=ft.Row(
            spacing=tokens.SPACE_TIGHT,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Icon(_caret(open_), size=tokens.ICON_XS, color=theme.dim(is_dark)),
                ft.Icon(
                    ft.Icons.SEARCH_ROUNDED if "search" in name.lower() else ft.Icons.BUILD_ROUNDED,
                    size=tokens.ICON_XS,
                    color=status_color,
                ),
                ft.Text(
                    name,
                    size=tokens.FONT_XS,
                    weight=ft.FontWeight.W_600,
                    color=status_color,
                    expand=True,
                    no_wrap=True,
                    overflow=ft.TextOverflow.ELLIPSIS,
                ),
                ft.Text(
                    "error" if is_error else "done",
                    size=tokens.FONT_2XS,
                    color=theme.dim(is_dark),
                ),
            ],
        ),
    )

    body: list[ft.Control] = [header]
    if open_:
        body.append(
            ft.Container(
                padding=ft.Margin.only(top=tokens.SPACE_XS),
                content=theme.markdown(content or "_No output._", is_dark=is_dark),
            ),
        )

    return ft.Container(
        padding=ft.Padding.symmetric(
            horizontal=tokens.SPACE_SNUG,
            vertical=tokens.SPACE_SM,
        ),
        bgcolor=theme.surface_2(is_dark),
        border_radius=tokens.RADIUS_MD,
        border=ft.Border.all(1, theme.border(is_dark)),
        content=ft.Column(spacing=0, tight=True, controls=body),
    )
